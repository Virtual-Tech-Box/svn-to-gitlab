"""Discovery and capability probing of the external tools we drive.

The tool must work against *any* SVN deployment, which in practice means it has to
find a Subversion client wherever the site happens to keep one. On the client's
Windows Server 2019 box that is VisualSVN Server's own `bin`; elsewhere it may be
SlikSVN, TortoiseSVN's command-line component, CollabNet, Cygwin, or a package
manager install.

`git svn` deserves special attention: it is a Perl script, and the *MinGit* build
that ships inside some installers omits Perl entirely. We probe for it explicitly
rather than discovering the problem three hours into a fetch.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .errors import ToolMissingError
from .logging_setup import get_logger
from .procs import IS_WINDOWS, bundle_dir, run, which

log = get_logger("tools")

# Directories searched in addition to PATH, most-likely first.
_WINDOWS_HINTS = [
    r"C:\Program Files\VisualSVN Server\bin",
    r"C:\Program Files (x86)\VisualSVN Server\bin",
    r"C:\Program Files\SlikSvn\bin",
    r"C:\Program Files (x86)\SlikSvn\bin",
    r"C:\Program Files\TortoiseSVN\bin",
    r"C:\Program Files (x86)\TortoiseSVN\bin",
    r"C:\Program Files\CollabNet\Subversion Client",
    r"C:\Program Files\Git\cmd",
    r"C:\Program Files\Git\bin",
    r"C:\Program Files\Git\mingw64\bin",
    r"C:\Program Files (x86)\Git\cmd",
    r"C:\cygwin64\bin",
]

_POSIX_HINTS = [
    "/usr/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/opt/local/bin",
    "/usr/local/git/bin",
]

# Minimums below which we know behaviour we depend on is missing.
MIN_GIT = (2, 20)      # `git svn` reset/--include-paths behaviour, worktree stability
MIN_SVN = (1, 7)       # single .svn dir, `svnrdump`
MIN_GIT_LFS = (2, 9)   # `git lfs migrate import --everything`


@dataclass
class ToolInfo:
    name: str
    path: Optional[str] = None
    version: Optional[str] = None
    version_tuple: tuple = ()
    available: bool = False
    detail: str = ""

    def to_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d["version_tuple"] = list(self.version_tuple)
        return d


@dataclass
class ToolSet:
    git: ToolInfo = field(default_factory=lambda: ToolInfo("git"))
    git_svn: ToolInfo = field(default_factory=lambda: ToolInfo("git-svn"))
    git_lfs: ToolInfo = field(default_factory=lambda: ToolInfo("git-lfs"))
    svn: ToolInfo = field(default_factory=lambda: ToolInfo("svn"))
    svnadmin: ToolInfo = field(default_factory=lambda: ToolInfo("svnadmin"))
    svnrdump: ToolInfo = field(default_factory=lambda: ToolInfo("svnrdump"))
    svnlook: ToolInfo = field(default_factory=lambda: ToolInfo("svnlook"))
    svnsync: ToolInfo = field(default_factory=lambda: ToolInfo("svnsync"))

    def all(self) -> List[ToolInfo]:
        return [self.git, self.git_svn, self.git_lfs, self.svn,
                self.svnadmin, self.svnrdump, self.svnlook, self.svnsync]

    def to_dict(self) -> Dict[str, object]:
        return {t.name: t.to_dict() for t in self.all()}

    # -- requirement helpers -------------------------------------------------

    def require(self, *names: str) -> None:
        """Raise a single actionable error naming everything that is missing."""
        missing = []
        for name in names:
            info = getattr(self, name.replace("-", "_"), None)
            if info is None or not info.available:
                missing.append(name)
        if missing:
            raise ToolMissingError(
                "required tool(s) not available: " + ", ".join(missing),
                _install_hint(missing),
            )

    def git_env(self) -> Dict[str, str]:
        """Environment additions so `git` can always find its helpers."""
        env: Dict[str, str] = {}
        if self.git.path:
            git_dir = str(Path(self.git.path).parent)
            env["PATH"] = git_dir + os.pathsep + os.environ.get("PATH", "")
        return env


def _install_hint(missing: Sequence[str]) -> str:
    hints = []
    for name in missing:
        if name in ("git", "git-svn"):
            hints.append(
                "Install Git for Windows (full installer, not MinGit) from https://git-scm.com/download/win "
                "- the standard installer includes Perl, which `git svn` requires."
                if IS_WINDOWS else
                "Install git and the git-svn package (Debian/Ubuntu: apt install git git-svn; "
                "RHEL: dnf install git git-svn; macOS: git-svn ships with Xcode CLT or `brew install git`)."
            )
        elif name == "git-lfs":
            hints.append("Install Git LFS from https://git-lfs.com (or disable `lfs.enabled` in the config).")
        elif name.startswith("svn"):
            hints.append(
                "A Subversion command-line client is required. On a VisualSVN Server host it lives in "
                r"'C:\Program Files\VisualSVN Server\bin'; otherwise install SlikSVN or TortoiseSVN with "
                "the command-line tools component enabled."
                if IS_WINDOWS else
                "Install Subversion (apt install subversion / dnf install subversion / brew install subversion)."
            )
    seen, unique = set(), []
    for h in hints:
        if h not in seen:
            seen.add(h)
            unique.append(h)
    return " ".join(unique)


def _search_dirs(extra: Sequence[str] = ()) -> List[str]:
    dirs: List[str] = [str(d) for d in extra]
    bundled = bundle_dir()
    if bundled:
        # Tools shipped alongside the frozen exe by the installer.
        dirs += [str(bundled / "tools" / sub) for sub in ("git/cmd", "git/bin", "svn/bin")]
    dirs += _WINDOWS_HINTS if IS_WINDOWS else _POSIX_HINTS
    return [d for d in dirs if d]


def _parse_version(text: str) -> tuple:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return ()
    return tuple(int(g) for g in match.groups() if g is not None)


def _probe(name: str, version_args: Sequence[str], extra_dirs: Sequence[str]) -> ToolInfo:
    info = ToolInfo(name)
    path = which(name, extra_dirs)
    if not path:
        info.detail = "not found on PATH or in any known install location"
        return info
    info.path = path
    try:
        result = run([path, *version_args], check=False, timeout=60, log_command=False)
        text = (result.stdout or result.stderr).strip()
        if result.returncode != 0:
            info.detail = f"found at {path} but `{name} {' '.join(version_args)}` exited {result.returncode}"
            return info
        info.version = text.splitlines()[0] if text else "unknown"
        info.version_tuple = _parse_version(info.version)
        info.available = True
    except Exception as exc:
        info.detail = f"found at {path} but could not be executed: {exc}"
    return info


def _probe_git_svn(git: ToolInfo) -> ToolInfo:
    """`git svn` is a subcommand, not a binary; probe it through git itself."""
    info = ToolInfo("git-svn")
    if not git.available or not git.path:
        info.detail = "git itself is unavailable"
        return info
    result = run([git.path, "svn", "--version"], check=False, timeout=120, log_command=False)
    text = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        lowered = text.lower()
        if "perl" in lowered or "can't locate" in lowered:
            info.detail = ("git-svn is present but its Perl runtime is broken or missing "
                           "(MinGit and some minimal packages omit Perl)")
        elif "not a git command" in lowered:
            info.detail = "this git build does not include the svn subcommand"
        else:
            info.detail = text.splitlines()[0] if text else f"exited {result.returncode}"
        return info
    info.path = f"{git.path} svn"
    for line in text.splitlines():
        if "git-svn version" in line:
            info.version = line.strip()
            break
    else:
        info.version = text.splitlines()[0] if text else "unknown"
    info.version_tuple = _parse_version(info.version)
    info.available = True
    return info


def detect_tools(extra_dirs: Sequence[str] = (), refresh: bool = False) -> ToolSet:
    """Locate every external tool. Result is cached unless `refresh` is set."""
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE

    dirs = _search_dirs(extra_dirs)
    log.debug("searching for tools in %d extra directories", len(dirs))

    ts = ToolSet()
    ts.git = _probe("git", ["--version"], dirs)
    ts.git_svn = _probe_git_svn(ts.git)
    ts.git_lfs = _probe("git-lfs", ["version"], dirs)
    ts.svn = _probe("svn", ["--version", "--quiet"], dirs)
    ts.svnadmin = _probe("svnadmin", ["--version", "--quiet"], dirs)
    ts.svnrdump = _probe("svnrdump", ["--version", "--quiet"], dirs)
    ts.svnlook = _probe("svnlook", ["--version", "--quiet"], dirs)
    ts.svnsync = _probe("svnsync", ["--version", "--quiet"], dirs)

    for info, minimum in ((ts.git, MIN_GIT), (ts.svn, MIN_SVN), (ts.git_lfs, MIN_GIT_LFS)):
        if info.available and info.version_tuple and info.version_tuple < minimum:
            info.detail = (f"version {'.'.join(map(str, info.version_tuple))} is older than the "
                           f"required {'.'.join(map(str, minimum))}")
            info.available = False

    for info in ts.all():
        if info.available:
            log.debug("found %-9s %s (%s)", info.name, info.path, info.version)
        else:
            log.debug("missing %-8s %s", info.name, info.detail)

    _CACHE = ts
    return ts


_CACHE: Optional[ToolSet] = None


def free_disk_bytes(path: Path) -> int:
    """Free space on the volume holding `path` (walking up to the first existing parent)."""
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(str(probe)).free
    except Exception:
        return 0


def human_bytes(count: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(count) < 1024 or unit == "TiB":
            return f"{count:.1f} {unit}" if unit != "B" else f"{int(count)} B"
        count /= 1024
    return f"{count:.1f} TiB"
