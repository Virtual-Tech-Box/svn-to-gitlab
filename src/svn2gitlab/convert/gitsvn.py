"""The git-svn mirror: the one repository that keeps its Subversion identity.

The mirror is never pushed to GitLab. It holds `refs/remotes/svn/*` and the
`git-svn-id` metadata that makes `git svn fetch` resumable and incremental, which is
what lets us keep syncing a live SVN repository right up to the cutover. Everything
published to GitLab is derived from it (see `export.py`), so rewriting the published
history never damages our ability to fetch the next revision.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Dict, List, Optional, Sequence

from ..config import ConvertConfig, LayoutMode, SourceConfig
from ..errors import ConversionError
from ..logging_setup import get_logger
from ..svn.analyze import DetectedLayout
from ..tools import ToolSet
from .git import Git

log = get_logger("gitsvn")

SVN_PREFIX = "svn/"
# git-svn fetches revision metadata in windows; the default of 100 is agonising on a
# large repository, and 10k is where the memory/latency trade-off settles.
LOG_WINDOW_SIZE = 10000

_REV_LINE = re.compile(r"^r(?P<rev>\d+) = (?P<sha>[0-9a-f]{40})(?: \((?P<ref>[^)]+)\))?")
_AUTHOR_MISSING = re.compile(r"Author: (?P<user>.+?) not defined in (?P<file>.+?) file", re.I)
_CHECKED_THROUGH = re.compile(r"Checked through r(\d+)")


@dataclass
class FetchResult:
    revisions_fetched: int = 0
    last_revision: int = 0
    head_sha: str = ""
    duration: float = 0.0
    refs: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "revisions_fetched": self.revisions_fetched,
            "last_revision": self.last_revision,
            "head_sha": self.head_sha,
            "duration": round(self.duration, 1),
            "ref_count": len(self.refs),
        }


class GitSvnMirror:
    """Wraps `git svn` for one repository."""

    def __init__(
        self,
        tools: ToolSet,
        repo_dir: Path,
        source_url: str,
        layout: DetectedLayout,
        convert: ConvertConfig,
        source: SourceConfig,
        authors_file: Optional[Path] = None,
        cancel: Optional[Event] = None,
    ) -> None:
        tools.require("git", "git-svn")
        self.tools = tools
        self.git = Git(tools, repo_dir, cancel=cancel)
        self.repo_dir = Path(repo_dir)
        self.source_url = source_url.rstrip("/")
        self.layout = layout
        self.convert = convert
        self.source = source
        self.authors_file = authors_file
        self.cancel = cancel

    # -- setup ---------------------------------------------------------------

    @property
    def initialised(self) -> bool:
        return self.git.exists and bool(self.git.config_get("svn-remote.svn.url"))

    def layout_args(self) -> List[str]:
        """Translate the detected layout into git-svn's -T/-b/-t options."""
        args: List[str] = []
        trunk = self.layout.trunk
        if self.layout.mode == LayoutMode.FLAT or trunk in (None, ".", ""):
            # No layout options: git-svn tracks the URL itself as `refs/remotes/svn/git-svn`.
            return args
        args += ["--trunk", trunk]
        for branch in self.layout.branches:
            args += ["--branches", branch]
        for tag in self.layout.tags:
            args += ["--tags", tag]
        return args

    @property
    def trunk_ref(self) -> str:
        if self.layout.mode == LayoutMode.FLAT or self.layout.trunk in (None, ".", ""):
            return f"refs/remotes/{SVN_PREFIX}git-svn"
        return f"refs/remotes/{SVN_PREFIX}trunk"

    def init(self, force: bool = False) -> None:
        if force and self.repo_dir.exists():
            log.warning("discarding existing mirror at %s", self.repo_dir)
            shutil.rmtree(self.repo_dir, ignore_errors=True)

        if self.initialised:
            self._apply_config()
            existing = self.git.config_get("svn-remote.svn.url")
            if existing.rstrip("/") != self.source_url:
                log.info("source URL changed (%s -> %s); rewriting svn-remote", existing, self.source_url)
                self.git.config_set("svn-remote.svn.url", self.source_url)
            return

        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.git.init(bare=False, initial_branch=self.convert.default_branch)
        args = ["svn", "init", self.source_url, "--prefix", SVN_PREFIX, *self.layout_args()]
        self.git.run(args, timeout=600,
                     remedy="`git svn init` failed. Verify the URL is reachable and that the "
                            "trunk/branches/tags paths exist in the repository.")
        self._apply_config()
        log.info("initialised git-svn mirror for %s (layout=%s)", self.source_url,
                 self.layout.mode.value)

    def _apply_config(self) -> None:
        self.git.apply_base_config()
        if self.authors_file and Path(self.authors_file).is_file():
            self.git.config_set("svn.authorsfile", str(Path(self.authors_file).resolve()))
        # Unmapped authors must not silently become "user <user@localhost>".
        self.git.config_set("svn.authorsProg", "")
        self.git.config_unset_all("svn.authorsProg")

        layout_cfg = self.source.layout
        if layout_cfg.ignore_paths:
            self.git.config_set("svn-remote.svn.ignore-paths", layout_cfg.ignore_paths)
        else:
            self.git.config_unset_all("svn-remote.svn.ignore-paths")
        if layout_cfg.include_paths:
            self.git.config_set("svn-remote.svn.include-paths", layout_cfg.include_paths)
        else:
            self.git.config_unset_all("svn-remote.svn.include-paths")

        if self.source.username:
            self.git.config_set("svn.username", self.source.username)
        self.git.config_set("svn-remote.svn.rewriteRoot", self.source.url or self.source_url)

    # -- fetching ------------------------------------------------------------

    def last_fetched_revision(self) -> int:
        """Highest SVN revision already present in the mirror."""
        best = 0
        for ref in self.git.list_refs(f"refs/remotes/{SVN_PREFIX}"):
            meta = self.git.commit_meta(ref.sha)
            rev = extract_svn_revision(meta.get("message", ""))
            if rev and rev > best:
                best = rev
        return best

    def fetch(
        self,
        revision_start: int = 1,
        revision_end: Optional[int] = None,
        head_revision: int = 0,
        on_progress: Optional[Callable[[float, str], None]] = None,
        retries: int = 3,
    ) -> FetchResult:
        """Fetch revisions into the mirror. Safe to call repeatedly; it resumes.

        Network conversions fail intermittently by nature, so a failed fetch is
        retried with backoff before it becomes a job failure. Every retry resumes at
        the revision that broke rather than starting over.
        """
        started = time.time()
        result = FetchResult()
        seen_revisions: List[int] = []
        last_reported = [0.0]

        def watch(line: str) -> None:
            match = _REV_LINE.match(line.strip())
            if match:
                rev = int(match.group("rev"))
                seen_revisions.append(rev)
                result.last_revision = max(result.last_revision, rev)
                now = time.time()
                if on_progress and head_revision and (now - last_reported[0] > 0.5):
                    last_reported[0] = now
                    ref = match.group("ref") or ""
                    suffix = f" [{ref.split('/')[-1]}]" if ref else ""
                    on_progress(min(0.99, rev / head_revision),
                                f"converted r{rev:,} of r{head_revision:,}{suffix}")
                return
            checked = _CHECKED_THROUGH.search(line)
            if checked:
                rev = int(checked.group(1))
                result.last_revision = max(result.last_revision, rev)

        args = ["svn", "fetch", f"--log-window-size={LOG_WINDOW_SIZE}"]
        if revision_start > 1 or revision_end:
            args += ["-r", f"{revision_start}:{revision_end or 'HEAD'}"]
        if self.authors_file and Path(self.authors_file).is_file():
            args += ["--authors-file", str(Path(self.authors_file).resolve())]

        attempt = 0
        while True:
            attempt += 1
            proc = self.git.run(
                args, check=False, timeout=None,
                on_stdout=watch, on_stderr=watch, collect_stdout=False,
            )
            if proc.ok:
                break
            failure = self._diagnose(proc.stderr or proc.stdout)
            if failure or attempt > retries:
                raise failure or ConversionError(
                    f"`git svn fetch` failed after {attempt} attempt(s) (exit {proc.returncode})",
                    "Re-running resumes from the last converted revision. If it fails at the "
                    "same revision every time, that revision likely contains a path git-svn "
                    "cannot represent - use `source.layout.ignore_paths` to exclude it.",
                )
            backoff = min(60, 5 * attempt)
            log.warning("git svn fetch failed (attempt %d/%d); retrying in %ds",
                        attempt, retries, backoff)
            self._recover_stale_lock()
            if self.cancel and self.cancel.wait(backoff):
                raise ConversionError("cancelled while waiting to retry the fetch")

        result.revisions_fetched = len(set(seen_revisions))
        if not result.last_revision:
            result.last_revision = self.last_fetched_revision()
        result.refs = {r.name: r.sha for r in self.git.list_refs(f"refs/remotes/{SVN_PREFIX}")}
        head = self.git.rev_parse(self.trunk_ref)
        result.head_sha = head or ""
        result.duration = time.time() - started
        log.info("fetched %d revision(s) up to r%d in %.0fs (%d refs)",
                 result.revisions_fetched, result.last_revision, result.duration, len(result.refs))
        if on_progress:
            on_progress(1.0, f"converted through r{result.last_revision:,}")
        return result

    def _diagnose(self, stderr: str) -> Optional[ConversionError]:
        """Recognise failures that retrying will never fix."""
        text = stderr or ""
        author = _AUTHOR_MISSING.search(text)
        if author:
            return ConversionError(
                f"SVN author {author.group('user')!r} is not present in the author map",
                "Run `svn2gitlab authors --write` to regenerate authors.txt with every author "
                "in the repository, review the addresses, then re-run the migration. The fetch "
                "resumes where it stopped.",
            )
        lowered = text.lower()
        if "filesystem has no item" in lowered and "e160013" in lowered:
            return ConversionError(
                "git-svn asked for a path that does not exist at that revision",
                "This usually means the configured trunk/branches/tags paths do not match the "
                "repository. Run `svn2gitlab analyze` and compare the detected layout.",
            )
        if "unable to determine upstream svn information" in lowered:
            return ConversionError(
                "the mirror's git-svn metadata is inconsistent",
                "Re-run with `--restart-from convert` to rebuild the mirror from scratch.",
            )
        if "couldn't open a repository" in lowered or "is not a working copy" in lowered:
            return ConversionError(
                "the mirror working directory is damaged",
                "Re-run with `--restart-from convert` to rebuild it.",
            )
        return None

    def _recover_stale_lock(self) -> None:
        """A killed fetch leaves index.lock behind and blocks every later attempt."""
        for lock in (self.repo_dir / ".git" / "index.lock",
                     self.repo_dir / ".git" / "svn" / ".metadata.lock"):
            if lock.exists():
                log.info("removing stale lock %s", lock)
                try:
                    lock.unlink()
                except OSError as exc:
                    log.warning("could not remove %s: %s", lock, exc)

    # -- inspection ----------------------------------------------------------

    def remote_refs(self) -> Dict[str, str]:
        return {r.name: r.sha for r in self.git.list_refs(f"refs/remotes/{SVN_PREFIX}")}

    def branch_refs(self) -> Dict[str, str]:
        """Branch name -> sha, excluding trunk and anything under a tags path."""
        out: Dict[str, str] = {}
        tag_prefixes = tuple(f"refs/remotes/{SVN_PREFIX}{t.rstrip('/*')}/" for t in self.layout.tags)
        trunk = self.trunk_ref
        for name, sha in self.remote_refs().items():
            if name == trunk or name.endswith("@"):  # peg-revision refs are noise
                continue
            if tag_prefixes and name.startswith(tag_prefixes):
                continue
            short = name[len(f"refs/remotes/{SVN_PREFIX}"):]
            if short in ("git-svn", "trunk"):
                continue
            out[short] = sha
        return out

    def tag_refs(self) -> Dict[str, str]:
        """Tag name -> sha for every ref living under a configured tags path."""
        out: Dict[str, str] = {}
        for tags_path in self.layout.tags:
            prefix = f"refs/remotes/{SVN_PREFIX}{tags_path.rstrip('/*')}/"
            for name, sha in self.remote_refs().items():
                if name.startswith(prefix) and not name.endswith("@"):
                    out[name[len(prefix):]] = sha
        if not self.layout.tags:
            # git-svn also stores tags flat when `--tags` was given without a subdirectory.
            prefix = f"refs/remotes/{SVN_PREFIX}tags/"
            for name, sha in self.remote_refs().items():
                if name.startswith(prefix) and not name.endswith("@"):
                    out[name[len(prefix):]] = sha
        return out

    def svn_ignore_content(self, ref: Optional[str] = None) -> str:
        """`git svn show-ignore` rendered as .gitignore text."""
        args = ["svn", "show-ignore"]
        if ref:
            args += ["-i", ref]
        result = self.git.run(args, check=False, timeout=3600)
        return result.stdout if result.ok else ""

    def revision_of(self, ref: str) -> int:
        meta = self.git.commit_meta(ref)
        return extract_svn_revision(meta.get("message", "")) or 0


_SVN_ID = re.compile(r"^git-svn-id:\s*(?P<url>\S+)@(?P<rev>\d+)\s+(?P<uuid>\S+)\s*$", re.M)


def extract_svn_revision(message: str) -> Optional[int]:
    match = _SVN_ID.search(message or "")
    return int(match.group("rev")) if match else None


def extract_svn_id(message: str) -> Optional[Dict[str, str]]:
    match = _SVN_ID.search(message or "")
    if not match:
        return None
    return {"url": match.group("url"), "rev": match.group("rev"), "uuid": match.group("uuid")}
