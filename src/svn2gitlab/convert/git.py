"""A small Git façade.

Only the operations the migration actually needs, with the settings that keep a
converted repository honest: no autocrlf mangling, no gc racing the conversion, no
global user identity leaking into synthesised commits.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..errors import ConversionError
from ..logging_setup import get_logger
from ..procs import ProcResult, run
from ..tools import ToolSet

log = get_logger("git")

# Settings applied to every repository we create. autocrlf in particular will
# silently rewrite every text file if the machine's global config enables it.
REPO_CONFIG = {
    "core.autocrlf": "false",
    "core.safecrlf": "false",
    "core.ignorecase": "false",
    "core.longpaths": "true",
    "core.fsmonitor": "false",
    "gc.auto": "0",
    "gc.autoDetach": "false",
    "svn.authorsfile": None,       # set explicitly later
    "i18n.commitEncoding": "UTF-8",
    "i18n.logOutputEncoding": "UTF-8",
}


@dataclass
class RefInfo:
    name: str
    sha: str
    kind: str  # branch | tag | remote | other


class Git:
    """Bound to one repository directory."""

    def __init__(self, tools: ToolSet, repo: Path, cancel: Optional[Event] = None) -> None:
        tools.require("git")
        self.tools = tools
        self.repo = Path(repo)
        self.cancel = cancel
        self.exe = tools.git.path or "git"

    # -- plumbing ------------------------------------------------------------

    def _env(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = dict(self.tools.git_env())
        # Deterministic, non-interactive behaviour regardless of the operator's setup.
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        env.setdefault("GIT_ASKPASS", "echo")
        env.setdefault("GIT_CONFIG_NOSYSTEM", "1")
        env.setdefault("LC_ALL", "C.UTF-8" if os.name != "nt" else "C")
        if extra:
            env.update(extra)
        return env

    def run(
        self,
        args: Sequence[str],
        check: bool = True,
        timeout: Optional[float] = None,
        env: Optional[Dict[str, str]] = None,
        on_stdout: Optional[Callable[[str], None]] = None,
        on_stderr: Optional[Callable[[str], None]] = None,
        collect_stdout: bool = True,
        cwd: Optional[Path] = None,
        input_text: Optional[str] = None,
        secrets: Sequence[Optional[str]] = (),
        remedy: str = "",
    ) -> ProcResult:
        return run(
            [self.exe, *args],
            cwd=cwd or self.repo,
            env=self._env(env),
            check=check,
            timeout=timeout,
            cancel=self.cancel,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            collect_stdout=collect_stdout,
            input_text=input_text,
            secrets=secrets,
            remedy=remedy,
        )

    def out(self, args: Sequence[str], check: bool = True, **kwargs) -> str:
        return self.run(args, check=check, **kwargs).stdout.strip()

    # -- repository lifecycle ------------------------------------------------

    @property
    def exists(self) -> bool:
        return (self.repo / ".git").is_dir() or (self.repo / "HEAD").is_file()

    def init(self, bare: bool = False, initial_branch: str = "main") -> "Git":
        self.repo.mkdir(parents=True, exist_ok=True)
        if self.exists:
            return self
        args = ["init"]
        if bare:
            args.append("--bare")
        # -b landed in git 2.28; fall back to renaming afterwards.
        if (self.tools.git.version_tuple or (0,)) >= (2, 28):
            args += ["-b", initial_branch]
        args.append(str(self.repo))
        run([self.exe, *args], env=self._env(), check=True, timeout=120, cancel=self.cancel)
        if (self.tools.git.version_tuple or (0,)) < (2, 28):
            self.run(["symbolic-ref", "HEAD", f"refs/heads/{initial_branch}"], check=False)
        self.apply_base_config()
        log.info("initialised git repository %s", self.repo)
        return self

    def apply_base_config(self) -> None:
        for key, value in REPO_CONFIG.items():
            if value is None:
                continue
            self.config_set(key, value)

    def config_set(self, key: str, value: str) -> None:
        self.run(["config", key, value], check=False)

    def config_get(self, key: str) -> str:
        result = self.run(["config", "--get", key], check=False)
        return result.stdout.strip() if result.ok else ""

    def config_unset_all(self, key: str) -> None:
        self.run(["config", "--unset-all", key], check=False)

    # -- refs ----------------------------------------------------------------

    def rev_parse(self, ref: str) -> Optional[str]:
        result = self.run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], check=False)
        sha = result.stdout.strip()
        return sha or None

    def ref_exists(self, ref: str) -> bool:
        return self.run(["show-ref", "--verify", "--quiet", ref], check=False).ok

    def list_refs(self, pattern: str = "") -> List[RefInfo]:
        args = ["for-each-ref", "--format=%(objectname) %(refname)"]
        if pattern:
            args.append(pattern)
        out = self.run(args, check=False).stdout
        refs: List[RefInfo] = []
        for line in out.splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) != 2:
                continue
            sha, name = parts
            if name.startswith("refs/heads/"):
                kind = "branch"
            elif name.startswith("refs/tags/"):
                kind = "tag"
            elif name.startswith("refs/remotes/"):
                kind = "remote"
            else:
                kind = "other"
            refs.append(RefInfo(name=name, sha=sha, kind=kind))
        return refs

    def update_ref(self, ref: str, sha: str) -> None:
        self.run(["update-ref", ref, sha])

    def delete_ref(self, ref: str) -> None:
        self.run(["update-ref", "-d", ref], check=False)

    def symbolic_head(self, branch: str) -> None:
        self.run(["symbolic-ref", "HEAD", f"refs/heads/{branch}"])

    def branch_exists(self, name: str) -> bool:
        return self.ref_exists(f"refs/heads/{name}")

    def current_branch(self) -> str:
        result = self.run(["symbolic-ref", "--short", "-q", "HEAD"], check=False)
        return result.stdout.strip()

    def checkout(self, ref: str, force: bool = False) -> None:
        args = ["checkout"]
        if force:
            args.append("--force")
        args.append(ref)
        self.run(args, timeout=None)

    # -- commits & objects ---------------------------------------------------

    def commit_count(self, ref: str = "--all") -> int:
        result = self.run(["rev-list", "--count", ref], check=False)
        try:
            return int(result.stdout.strip() or 0)
        except ValueError:
            return 0

    def commit_meta(self, ref: str) -> Dict[str, str]:
        fmt = "%H%n%an%n%ae%n%aI%n%cn%n%ce%n%cI%n%B"
        result = self.run(["log", "-1", f"--format={fmt}", ref], check=False)
        if not result.ok:
            return {}
        lines = result.stdout.split("\n")
        if len(lines) < 7:
            return {}
        return {
            "sha": lines[0], "author_name": lines[1], "author_email": lines[2],
            "author_date": lines[3], "committer_name": lines[4], "committer_email": lines[5],
            "committer_date": lines[6], "message": "\n".join(lines[7:]).strip(),
        }

    def cat_file(self, sha: str) -> bytes:
        """Raw bytes of a blob. Binary-safe, unlike the text-mode helpers."""
        import subprocess
        result = subprocess.run(
            [self.exe, "-C", str(self.repo), "cat-file", "blob", sha],
            capture_output=True, env=self._env(),
        )
        if result.returncode != 0:
            raise ConversionError(
                f"could not read blob {sha[:12]}: "
                f"{result.stderr.decode('utf-8', 'replace').strip()}")
        return result.stdout

    def is_empty_commit(self, sha: str) -> bool:
        """True when the commit's tree is identical to its first parent's."""
        parent = self.run(["rev-parse", "--verify", "--quiet", f"{sha}^1"], check=False).stdout.strip()
        if not parent:
            return False
        result = self.run(["diff-tree", "--quiet", parent, sha], check=False)
        return result.returncode == 0

    def first_parent(self, sha: str) -> Optional[str]:
        result = self.run(["rev-parse", "--verify", "--quiet", f"{sha}^1"], check=False)
        return result.stdout.strip() or None

    def repo_size_bytes(self) -> int:
        result = self.run(["count-objects", "-v"], check=False)
        total = 0
        for line in result.stdout.splitlines():
            if line.startswith(("size:", "size-pack:")):
                match = re.search(r"(\d+)", line)
                if match:
                    total += int(match.group(1)) * 1024  # count-objects reports KiB
        return total

    def largest_blobs(self, limit: int = 200, min_bytes: int = 1) -> List[Tuple[str, int, str]]:
        """Every blob in the whole history, largest first: (sha, size, path).

        This is the authoritative input for LFS planning - unlike an SVN HEAD listing
        it sees every historical revision of every file, which is exactly where the
        150 MB build artefact that was deleted in 2014 is hiding.
        """
        listing = self.run(["rev-list", "--objects", "--all"], check=False, timeout=None)
        if not listing.ok:
            return []
        paths: Dict[str, str] = {}
        for line in listing.stdout.splitlines():
            sha, _, path = line.partition(" ")
            if path:
                paths[sha] = path
        if not paths:
            return []
        batch_input = "\n".join(paths.keys()) + "\n"
        check = self.run(["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                         check=False, input_text=batch_input, timeout=None)
        out: List[Tuple[str, int, str]] = []
        for line in check.stdout.splitlines():
            parts = line.split()
            if len(parts) != 3 or parts[1] != "blob":
                continue
            size = int(parts[2])
            if size >= min_bytes:
                out.append((parts[0], size, paths.get(parts[0], "")))
        out.sort(key=lambda item: item[1], reverse=True)
        return out[:limit]

    # -- maintenance ---------------------------------------------------------

    def gc(self, aggressive: bool = False) -> None:
        args = ["gc", "--prune=now"]
        if aggressive:
            args.append("--aggressive")
        self.run(args, check=False, timeout=None)

    def repack(self) -> None:
        self.run(["repack", "-adf", "--depth=50", "--window=50"], check=False, timeout=None)

    def fsck(self) -> ProcResult:
        return self.run(["fsck", "--no-dangling"], check=False, timeout=None)

    # -- worktree ------------------------------------------------------------

    def add_all_and_commit(self, message: str, author: str, date: str,
                           paths: Optional[Sequence[str]] = None,
                           allow_empty: bool = False) -> Optional[str]:
        """Commit with a fully pinned identity so the resulting SHA is reproducible."""
        self.run(["add", "--", *(paths or ["."])], check=False)
        status = self.run(["status", "--porcelain"], check=False).stdout.strip()
        if not status and not allow_empty:
            return None
        env = {
            "GIT_AUTHOR_NAME": _identity_name(author),
            "GIT_AUTHOR_EMAIL": _identity_email(author),
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_NAME": _identity_name(author),
            "GIT_COMMITTER_EMAIL": _identity_email(author),
            "GIT_COMMITTER_DATE": date,
        }
        args = ["commit", "-m", message, "--no-verify", "--no-gpg-sign"]
        if allow_empty:
            args.append("--allow-empty")
        self.run(args, env=env, timeout=600)
        return self.rev_parse("HEAD")

    def create_annotated_tag(self, name: str, target: str, message: str,
                             tagger_name: str, tagger_email: str, tagger_date: str,
                             force: bool = True) -> None:
        env = {
            "GIT_COMMITTER_NAME": tagger_name,
            "GIT_COMMITTER_EMAIL": tagger_email,
            "GIT_COMMITTER_DATE": tagger_date,
        }
        args = ["tag", "-a", "-m", message or name]
        if force:
            args.append("-f")
        args += [name, target]
        self.run(args, env=env, remedy=f"could not create tag {name!r}")

    def create_lightweight_tag(self, name: str, target: str) -> None:
        self.run(["tag", "-f", name, target])

    # -- remotes -------------------------------------------------------------

    def set_remote(self, name: str, url: str, secrets: Sequence[Optional[str]] = ()) -> None:
        existing = self.run(["remote", "get-url", name], check=False)
        if existing.ok:
            self.run(["remote", "set-url", name, url], secrets=secrets)
        else:
            self.run(["remote", "add", name, url], secrets=secrets)

    def remote_head_branch(self, remote: str) -> str:
        result = self.run(["remote", "show", remote], check=False, timeout=120)
        for line in result.stdout.splitlines():
            if "HEAD branch" in line:
                return line.split(":", 1)[-1].strip()
        return ""


def _identity_name(identity: str) -> str:
    match = re.match(r"^(.*?)\s*<", identity)
    return (match.group(1).strip() if match else identity) or "svn2gitlab"


def _identity_email(identity: str) -> str:
    match = re.search(r"<([^>]*)>", identity)
    return (match.group(1).strip() if match else "") or "svn2gitlab@localhost"


def ensure_supported(tools: ToolSet) -> None:
    if not tools.git.available:
        raise ConversionError("git is not available", "Install Git and re-run `svn2gitlab doctor`.")
