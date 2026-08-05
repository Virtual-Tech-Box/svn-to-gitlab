"""A thin, well-behaved Subversion client wrapper.

Design notes that matter in the field:

* Credentials are fed through `--password-from-stdin` when the client supports it
  (Subversion 1.10+), so passwords never appear in the process list of a shared
  Windows Server.
* Certificate trust flags changed shape in 1.9; we emit whichever form the installed
  client understands rather than assuming.
* `svn log` is paged rather than fetched in one shot. A 200k-revision VisualSVN
  repository produces hundreds of megabytes of XML, and the naive single call both
  exhausts memory and reports no progress for an hour.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlparse

from ..errors import SvnError
from ..logging_setup import get_logger
from ..procs import ProcResult, run
from ..tools import ToolSet

log = get_logger("svn")

# Subversion returns these on the stderr of an otherwise successful-looking call.
_AUTH_HINTS = ("authorization failed", "authentication failed", "e170001", "e215004",
               "no more credentials", "username or password")
_CERT_HINTS = ("server certificate verification failed", "e230001", "ssl handshake failed")
_MISSING_HINTS = ("non-existent in revision", "e160013", "path not found", "url .* doesn't exist")


@dataclass
class SvnInfo:
    url: str = ""
    repository_root: str = ""
    repository_uuid: str = ""
    revision: int = 0
    node_kind: str = ""
    last_changed_rev: int = 0
    last_changed_author: str = ""
    last_changed_date: Optional[datetime] = None

    @property
    def relative_path(self) -> str:
        """Path of `url` within the repository root, without leading slash."""
        if not self.repository_root or not self.url:
            return ""
        if self.url == self.repository_root:
            return ""
        if self.url.startswith(self.repository_root):
            return self.url[len(self.repository_root):].strip("/")
        return ""


@dataclass
class SvnEntry:
    path: str
    kind: str          # "file" | "dir"
    size: int = 0
    revision: int = 0
    author: str = ""
    date: Optional[datetime] = None

    @property
    def is_dir(self) -> bool:
        return self.kind == "dir"


@dataclass
class SvnLogEntry:
    revision: int
    author: str = ""
    date: Optional[datetime] = None
    message: str = ""
    paths: List[Tuple[str, str, Optional[str], Optional[int]]] = field(default_factory=list)
    # (action, path, copyfrom_path, copyfrom_rev)


def _parse_date(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    text = text.strip()
    # 2019-04-11T08:12:33.123456Z
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def url_join(base: str, *parts: str) -> str:
    """Join URL segments, percent-encoding each segment but keeping separators."""
    url = base.rstrip("/")
    for part in parts:
        for segment in str(part).strip("/").split("/"):
            if segment:
                url += "/" + quote(segment, safe="~!$&'()*+,;=:@")
    return url


class SvnClient:
    """All Subversion interaction funnels through this class."""

    def __init__(
        self,
        tools: ToolSet,
        username: Optional[str] = None,
        password: Optional[str] = None,
        trust_server_cert: bool = True,
        config_dir: Optional[str] = None,
        cancel: Optional[Event] = None,
    ) -> None:
        tools.require("svn")
        self.tools = tools
        self.username = username
        self.password = password
        self.trust_server_cert = trust_server_cert
        self.config_dir = config_dir
        self.cancel = cancel
        self._version = tools.svn.version_tuple or (1, 7)
        self._supports_stdin_password = self._version >= (1, 10)
        self._supports_cert_failures = self._version >= (1, 9)

    # -- command construction ------------------------------------------------

    def _auth_args(self) -> List[str]:
        args = ["--non-interactive"]
        if self.username:
            args += ["--username", self.username]
        if self.password and not self._supports_stdin_password:
            # Older clients leave us no choice; the value is redacted from all logs.
            args += ["--password", self.password]
        elif self.password:
            args += ["--password-from-stdin"]
        if self.trust_server_cert:
            if self._supports_cert_failures:
                args += ["--trust-server-cert-failures=unknown-ca,cn-mismatch,expired,"
                         "not-yet-valid,other"]
            else:
                args += ["--trust-server-cert"]
        if self.config_dir:
            args += ["--config-dir", str(self.config_dir)]
        return args

    def _run(
        self,
        subcommand: str,
        args: Sequence[str],
        binary: str = "svn",
        cwd: Optional[Path] = None,
        check: bool = True,
        timeout: Optional[float] = None,
        on_stdout: Optional[Callable[[str], None]] = None,
        collect_stdout: bool = True,
        auth: bool = True,
    ) -> ProcResult:
        exe = getattr(self.tools, binary.replace("-", "_")).path
        if not exe:
            raise SvnError(f"{binary} is not available")
        cmd = [exe, subcommand, *args]
        if auth:
            cmd += self._auth_args()
        stdin_text = self.password + "\n" if (auth and self.password and self._supports_stdin_password) else None
        result = run(
            cmd,
            cwd=cwd,
            check=False,
            timeout=timeout,
            input_text=stdin_text,
            cancel=self.cancel,
            secrets=[self.password],
            on_stdout=on_stdout,
            collect_stdout=collect_stdout,
        )
        if check and not result.ok:
            raise self._translate(result)
        return result

    def _translate(self, result: ProcResult) -> SvnError:
        text = (result.stderr or result.stdout or "").strip()
        lowered = text.lower()
        tail = "\n    ".join(text.splitlines()[-8:])
        if any(h in lowered for h in _AUTH_HINTS):
            return SvnError(
                f"Subversion rejected the credentials:\n    {tail}",
                "Check source.username/source.password. For VisualSVN with Windows "
                "authentication, use DOMAIN\\user, and confirm the account has read access "
                "to the whole repository - a partially readable repository cannot be migrated "
                "faithfully.",
            )
        if any(h in lowered for h in _CERT_HINTS):
            return SvnError(
                f"TLS certificate was not accepted:\n    {tail}",
                "VisualSVN often uses a self-signed certificate. Set "
                "`source.trust_server_cert: true`, or import the server certificate into the "
                "machine trust store.",
            )
        if any(re.search(h, lowered) for h in _MISSING_HINTS):
            return SvnError(
                f"Subversion path or revision does not exist:\n    {tail}",
                "Check source.url and the layout paths (trunk/branches/tags) against "
                "`svn ls` output for the repository.",
            )
        if "e000111" in lowered or "connection refused" in lowered or "could not connect" in lowered:
            return SvnError(
                f"Could not reach the Subversion server:\n    {tail}",
                "Verify the URL, that the VisualSVN Server service is running, and that "
                "port 443/8443 is reachable from this host.",
            )
        return SvnError(f"svn command failed (exit {result.returncode}):\n    {tail}")

    # -- queries -------------------------------------------------------------

    def info(self, target: str, revision: Optional[int] = None) -> SvnInfo:
        args = [target, "--xml"]
        if revision is not None:
            args += ["-r", str(revision)]
        result = self._run("info", args, timeout=300)
        root = ET.fromstring(result.stdout)
        entry = root.find("entry")
        if entry is None:
            raise SvnError(f"svn info returned no entry for {target}")
        commit = entry.find("commit")
        return SvnInfo(
            url=(entry.findtext("url") or "").rstrip("/"),
            repository_root=(entry.findtext("repository/root") or "").rstrip("/"),
            repository_uuid=entry.findtext("repository/uuid") or "",
            revision=int(entry.get("revision") or 0),
            node_kind=entry.get("kind") or "",
            last_changed_rev=int(commit.get("revision")) if commit is not None and commit.get("revision") else 0,
            last_changed_author=(commit.findtext("author") if commit is not None else "") or "",
            last_changed_date=_parse_date(commit.findtext("date")) if commit is not None else None,
        )

    def exists(self, target: str, revision: Optional[int] = None) -> bool:
        args = [target, "--xml"]
        if revision is not None:
            args += ["-r", str(revision)]
        return self._run("info", args, check=False, timeout=180).ok

    def head_revision(self, target: str) -> int:
        return self.info(target).revision

    def ls(self, target: str, revision: Optional[int] = None, recursive: bool = False,
           check: bool = True) -> List[SvnEntry]:
        args = [target, "--xml"]
        if recursive:
            args.append("--recursive")
        if revision is not None:
            args += ["-r", str(revision)]
        result = self._run("list", args, check=check, timeout=None)
        if not result.ok:
            return []
        return self._parse_list(result.stdout)

    @staticmethod
    def _parse_list(xml_text: str) -> List[SvnEntry]:
        entries: List[SvnEntry] = []
        if not xml_text.strip():
            return entries
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise SvnError(f"could not parse `svn list --xml` output: {exc}") from exc
        for node in root.iter("entry"):
            name = node.findtext("name") or ""
            if not name:
                continue
            commit = node.find("commit")
            entries.append(SvnEntry(
                path=name,
                kind=node.get("kind") or "",
                size=int(node.findtext("size") or 0),
                revision=int(commit.get("revision")) if commit is not None and commit.get("revision") else 0,
                author=(commit.findtext("author") if commit is not None else "") or "",
                date=_parse_date(commit.findtext("date")) if commit is not None else None,
            ))
        return entries

    def propget(self, prop: str, target: str, revision: Optional[int] = None,
                recursive: bool = False) -> Dict[str, str]:
        args = [prop, target, "--xml"]
        if recursive:
            args.append("--recursive")
        if revision is not None:
            args += ["-r", str(revision)]
        result = self._run("propget", args, check=False, timeout=600)
        if not result.ok or not result.stdout.strip():
            return {}
        out: Dict[str, str] = {}
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError:
            return {}
        for target_node in root.iter("target"):
            path = target_node.get("path") or ""
            value = target_node.findtext("property") or ""
            out[path] = value
        return out

    def cat(self, target: str, revision: Optional[int] = None) -> str:
        args = [target]
        if revision is not None:
            args += ["-r", str(revision)]
        return self._run("cat", args, timeout=600).stdout

    def export(self, target: str, dest: Path, revision: Optional[int] = None,
               ignore_keywords: bool = True, ignore_externals: bool = True) -> None:
        args = [target, str(dest), "--force"]
        if revision is not None:
            args += ["-r", str(revision)]
        if ignore_keywords:
            # Keeps $Id$/$Revision$ unexpanded so the bytes match what git stores.
            args.append("--ignore-keywords")
        if ignore_externals:
            args.append("--ignore-externals")
        self._run("export", args, timeout=None)

    # -- log paging ----------------------------------------------------------

    def iter_log(
        self,
        target: str,
        start: int = 1,
        end: Optional[int] = None,
        with_paths: bool = False,
        page_size: int = 5000,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> Iterator[SvnLogEntry]:
        """Yield log entries in ascending revision order, one page at a time.

        Paging keeps memory flat and lets the UI show "revision 41,300 of 212,884"
        instead of a spinner.
        """
        head = end if end is not None else self.head_revision(target)
        current = max(1, start)
        while current <= head:
            args = [target, "--xml", "-r", f"{current}:{head}", "--limit", str(page_size)]
            args.append("-v" if with_paths else "-q")
            result = self._run("log", args, timeout=None)
            page = list(self._parse_log(result.stdout))
            if not page:
                break
            for entry in page:
                yield entry
            last = page[-1].revision
            if on_progress:
                on_progress(last, head)
            if last >= head or len(page) < page_size:
                break
            current = last + 1

    @staticmethod
    def _parse_log(xml_text: str) -> Iterable[SvnLogEntry]:
        if not xml_text.strip():
            return []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise SvnError(f"could not parse `svn log --xml` output: {exc}") from exc
        out: List[SvnLogEntry] = []
        for node in root.iter("logentry"):
            paths = []
            paths_node = node.find("paths")
            if paths_node is not None:
                for p in paths_node.findall("path"):
                    copyfrom_rev = p.get("copyfrom-rev")
                    paths.append((
                        p.get("action") or "",
                        (p.text or "").strip(),
                        p.get("copyfrom-path"),
                        int(copyfrom_rev) if copyfrom_rev else None,
                    ))
            out.append(SvnLogEntry(
                revision=int(node.get("revision") or 0),
                author=node.findtext("author") or "",
                date=_parse_date(node.findtext("date")),
                message=node.findtext("msg") or "",
                paths=paths,
            ))
        return out

    def log_paths_for(self, target: str, limit: int = 200,
                      revision: Optional[int] = None) -> List[SvnLogEntry]:
        """Most recent `limit` entries with changed paths - used for branch/tag forensics."""
        args = [target, "--xml", "-v", "--limit", str(limit)]
        if revision is not None:
            args += ["-r", f"{revision}:0"]
        result = self._run("log", args, check=False, timeout=1800)
        if not result.ok:
            return []
        return list(self._parse_log(result.stdout))


def is_local_url(url: str) -> bool:
    return urlparse(url).scheme == "file"


def repo_path_from_file_url(url: str) -> Optional[Path]:
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    path = parsed.path
    # file:///C:/repo -> C:/repo on Windows
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    from urllib.parse import unquote
    return Path(unquote(path))
