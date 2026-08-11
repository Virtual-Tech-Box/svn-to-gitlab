"""Getting the history onto local disk before converting it.

`git svn` against a remote server issues one round trip per revision. Over a WAN
that is 200-500 ms each, so a 60,000-revision repository takes days and any network
blip restarts the current revision. Converting from a local copy removes the network
from the inner loop entirely and typically turns days into hours.

Three acquisition strategies, chosen automatically:

* **local**    - the repository is already on this machine; use `file://` directly.
                 Zero copy, zero downtime. FSFS is safe for concurrent readers.
* **hotcopy**  - `svnadmin hotcopy` makes an isolated, consistent copy. Costs disk
                 but insulates the conversion from ongoing commits.
* **mirror**   - `svnsync` (preferred) or `svnrdump` pulls a remote repository into
                 a local one. Both are resumable, which matters on a flaky VPN.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Optional

from ..config import FastPath, SourceConfig
from ..errors import SvnError
from ..logging_setup import get_logger
from ..procs import IS_WINDOWS, run
from ..tools import ToolSet, free_disk_bytes, human_bytes
from .client import SvnClient, url_join

log = get_logger("acquire")

# Below this many revisions, mirroring a remote repository costs more than it saves.
MIRROR_REVISION_THRESHOLD = 2000


@dataclass
class Acquisition:
    """The URL the converter should actually use, and how we got there."""

    url: str
    strategy: str
    local_repo: Optional[Path] = None
    revisions_mirrored: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "strategy": self.strategy,
            "local_repo": str(self.local_repo) if self.local_repo else None,
            "revisions_mirrored": self.revisions_mirrored,
            "note": self.note,
        }


def choose_strategy(source: SourceConfig, tools: ToolSet, head_revision: int,
                    require_local: bool = False) -> str:
    """Resolve `fast_path: auto` into a concrete strategy.

    `require_local` is set by the native conversion engine, which reads an
    `svnadmin dump` and therefore cannot work against a bare remote URL. In that
    case "talk to the live server" is not an option and we mirror instead.
    """
    requested = source.fast_path
    if requested == FastPath.NONE:
        if require_local and not source.local_path:
            raise SvnError(
                "conversion needs a local repository, but `fast_path: none` "
                "forbids making one",
                "Set `fast_path: auto` so the repository is mirrored locally first, "
                "or point `source.local_path` at the repository if this is the "
                "Subversion server.",
            )
        return "direct"
    if requested == FastPath.HOTCOPY:
        return "hotcopy"
    if requested == FastPath.RDUMP:
        return "mirror"

    if source.local_path:
        return "local"
    if (require_local or head_revision >= MIRROR_REVISION_THRESHOLD) \
            and (tools.svnsync.available or tools.svnrdump.available) \
            and tools.svnadmin.available:
        return "mirror"
    if require_local:
        raise SvnError(
            "conversion needs a local copy of the repository, but neither svnsync "
            "nor svnrdump is available to make one",
            "Install a full Subversion client (both ship with Subversion), or run "
            "this on the Subversion server with `source.local_path` set.",
        )
    return "direct"


def acquire(
    source: SourceConfig,
    tools: ToolSet,
    client: SvnClient,
    target_dir: Path,
    head_revision: int,
    estimated_bytes: int = 0,
    cancel: Optional[Event] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
    require_local: bool = False,
) -> Acquisition:
    """Make the history locally available and return the URL to convert from."""
    strategy = choose_strategy(source, tools, head_revision, require_local=require_local)
    log.info("acquisition strategy: %s", strategy)

    if strategy == "direct":
        url = source.url or source.effective_url()
        if source.subpath:
            url = url_join(url, source.subpath)
        return Acquisition(url=url, strategy="direct",
                           note="converting straight from the live server")

    if strategy == "local":
        repo = Path(source.local_path).resolve()  # type: ignore[arg-type]
        _assert_repository(repo, tools)
        url = repo.as_uri()
        if source.subpath:
            url = url_join(url, source.subpath)
        return Acquisition(url=url, strategy="local", local_repo=repo,
                           note="reading the repository directly from local disk")

    if strategy == "hotcopy":
        repo = _hotcopy(source, tools, target_dir, estimated_bytes, cancel, on_progress)
        url = repo.as_uri()
        if source.subpath:
            url = url_join(url, source.subpath)
        return Acquisition(url=url, strategy="hotcopy", local_repo=repo,
                           note="isolated copy taken with svnadmin hotcopy")

    repo, mirrored = _mirror(source, tools, client, target_dir, head_revision,
                             cancel, on_progress)
    url = repo.as_uri()
    if source.subpath:
        url = url_join(url, source.subpath)
    return Acquisition(url=url, strategy="mirror", local_repo=repo,
                       revisions_mirrored=mirrored,
                       note="remote repository mirrored to local disk (resumable)")


# --------------------------------------------------------------------------- #
# Local repository helpers
# --------------------------------------------------------------------------- #

def _assert_repository(path: Path, tools: ToolSet) -> None:
    if not path.is_dir():
        raise SvnError(f"source.local_path does not exist: {path}",
                       "Point it at the repository directory itself, e.g. "
                       r"C:\Repositories\myrepo (the folder containing 'format' and 'db').")
    if not (path / "format").is_file() or not (path / "db").is_dir():
        raise SvnError(f"{path} is not a Subversion repository",
                       "VisualSVN keeps repositories under its Repositories folder; pass the "
                       "individual repository directory, not the parent.")
    if tools.svnadmin.available:
        result = run([tools.svnadmin.path, "verify", "--quiet", "-r", "0:0", str(path)],
                     check=False, timeout=120)
        if not result.ok and "unrecognized" not in (result.stderr or "").lower():
            log.warning("svnadmin could not read %s cleanly: %s", path,
                        (result.stderr or "").strip().splitlines()[:3])


def youngest_revision(repo: Path, tools: ToolSet) -> int:
    """Highest revision present in a local repository (0 for a fresh one)."""
    if not tools.svnlook.available:
        raise SvnError("svnlook is required to inspect a local repository")
    result = run([tools.svnlook.path, "youngest", str(repo)], check=False, timeout=120)
    if not result.ok:
        return 0
    match = re.search(r"\d+", result.stdout)
    return int(match.group()) if match else 0


def create_repository(path: Path, tools: ToolSet, uuid: Optional[str] = None) -> Path:
    """Create an empty local repository with the revprop hook svnsync requires."""
    tools.require("svnadmin")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        run([tools.svnadmin.path, "create", str(path)], timeout=300,
            remedy="Check that the work directory is writable and not on a network share "
                   "that lacks byte-range locking.")
        log.info("created local repository %s", path)
    if uuid:
        run([tools.svnadmin.path, "setuuid", str(path), uuid], check=False, timeout=120)
    _install_revprop_hook(path)
    return path


def _install_revprop_hook(repo: Path) -> None:
    """svnsync refuses to run unless the mirror allows revision property changes."""
    hooks = repo / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        hook = hooks / "pre-revprop-change.bat"
        hook.write_text("@ECHO OFF\r\nREM installed by svn2gitlab - allow svnsync revprops\r\nEXIT /B 0\r\n",
                        encoding="ascii")
    else:
        hook = hooks / "pre-revprop-change"
        hook.write_text("#!/bin/sh\n# installed by svn2gitlab - allow svnsync revprops\nexit 0\n",
                        encoding="utf-8")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log.debug("installed %s", hook)


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #

def _hotcopy(source: SourceConfig, tools: ToolSet, target_dir: Path, estimated_bytes: int,
             cancel: Optional[Event], on_progress: Optional[Callable[[float, str], None]]) -> Path:
    tools.require("svnadmin")
    if not source.local_path:
        raise SvnError("fast_path: hotcopy requires source.local_path",
                       "Run the tool on the SVN server, or use fast_path: rdump for a remote "
                       "repository.")
    origin = Path(source.local_path).resolve()
    _assert_repository(origin, tools)
    dest = target_dir
    if dest.exists():
        log.info("removing previous hotcopy at %s", dest)
        shutil.rmtree(dest, onerror=_force_remove)

    needed = estimated_bytes or _dir_size(origin)
    free = free_disk_bytes(dest.parent)
    if free and needed and free < needed * 1.15:
        raise SvnError(
            f"not enough free space for a hotcopy: need about {human_bytes(needed * 1.15)}, "
            f"{human_bytes(free)} available on {dest.parent}",
            "Point `workdir` at a larger volume, or use `fast_path: none` to convert directly "
            "from the live repository.",
        )

    if on_progress:
        on_progress(0.05, f"hotcopying {human_bytes(needed)}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    run([tools.svnadmin.path, "hotcopy", str(origin), str(dest)],
        timeout=None, cancel=cancel,
        remedy="svnadmin hotcopy needs read access to the repository and write access to the "
               "destination. On Windows, run the tool as an account with those rights.")
    if on_progress:
        on_progress(1.0, "hotcopy complete")
    log.info("hotcopy complete: %s", dest)
    return dest


def _mirror(source: SourceConfig, tools: ToolSet, client: SvnClient, target_dir: Path,
            head_revision: int, cancel: Optional[Event],
            on_progress: Optional[Callable[[float, str], None]]) -> tuple:
    """Pull a remote repository into a local one, resuming if a copy already exists."""
    tools.require("svnadmin")
    remote_root = client.info(source.url or "").repository_root
    if not remote_root:
        raise SvnError("could not determine the repository root of the source URL")

    repo = create_repository(target_dir, tools)
    already = youngest_revision(repo, tools) if tools.svnlook.available else 0
    if already >= head_revision:
        log.info("local mirror already at r%d; nothing to fetch", already)
        return repo, 0

    if already:
        log.info("resuming mirror from r%d towards r%d", already, head_revision)

    if tools.svnsync.available:
        mirrored = _svnsync(source, tools, repo, remote_root, already, head_revision,
                            cancel, on_progress)
    elif tools.svnrdump.available:
        mirrored = _svnrdump(source, tools, repo, remote_root, already, head_revision,
                             cancel, on_progress)
    else:
        raise SvnError("neither svnsync nor svnrdump is available to mirror the repository",
                       "Install a full Subversion client, or set `fast_path: none` to convert "
                       "directly over the network.")
    return repo, mirrored


def _sync_auth_args(source: SourceConfig, prefix: str) -> list:
    """svnsync needs source/target credentials named separately."""
    args = ["--non-interactive"]
    if source.username:
        args += [f"--{prefix}-username", source.username]
    if source.password:
        args += [f"--{prefix}-password", source.password]
    if source.trust_server_cert:
        args.append("--trust-server-cert-failures=unknown-ca,cn-mismatch,expired,"
                    "not-yet-valid,other")
    return args


def _svnsync(source: SourceConfig, tools: ToolSet, repo: Path, remote_root: str,
             already: int, head: int, cancel: Optional[Event],
             on_progress: Optional[Callable[[float, str], None]]) -> int:
    mirror_url = repo.as_uri()
    initialised = already > 0 or _svnsync_initialised(tools, mirror_url, source)
    if not initialised:
        log.info("initialising svnsync mirror of %s", remote_root)
        run([tools.svnsync.path, "initialize", mirror_url, remote_root,
             *_sync_auth_args(source, "source")],
            timeout=600, cancel=cancel, secrets=[source.password],
            remedy="svnsync initialize failed. The mirror must be empty and its "
                   "pre-revprop-change hook must exit 0 (this tool installs one).")

    progress_re = re.compile(r"Committed revision (\d+)")
    latest = [already]

    def watch(line: str) -> None:
        match = progress_re.search(line)
        if match:
            rev = int(match.group(1))
            latest[0] = rev
            if on_progress and head:
                on_progress(min(1.0, rev / head), f"mirrored r{rev:,} of r{head:,}")

    run([tools.svnsync.path, "synchronize", mirror_url, *_sync_auth_args(source, "source")],
        timeout=None, cancel=cancel, secrets=[source.password],
        on_stdout=watch, collect_stdout=False,
        remedy="svnsync synchronize failed. It is safe to re-run - the mirror resumes from "
               "the last committed revision.")
    return max(0, latest[0] - already)


def _svnsync_initialised(tools: ToolSet, mirror_url: str, source: SourceConfig) -> bool:
    result = run([tools.svnsync.path, "info", mirror_url, "--non-interactive"],
                 check=False, timeout=120, secrets=[source.password])
    return result.ok and "Source URL" in result.stdout


def _svnrdump(source: SourceConfig, tools: ToolSet, repo: Path, remote_root: str,
              already: int, head: int, cancel: Optional[Event],
              on_progress: Optional[Callable[[float, str], None]]) -> int:
    """Fallback mirror using `svnrdump dump | svnadmin load`, resumable by revision."""
    from ..procs import pipe

    start = already + 1 if already else 0
    dump_args = [tools.svnrdump.path, "dump", remote_root, "-r", f"{start}:{head}",
                 "--non-interactive"]
    if already:
        dump_args.append("--incremental")
    if source.username:
        dump_args += ["--username", source.username]
    if source.password:
        dump_args += ["--password", source.password]
    if source.trust_server_cert:
        dump_args.append("--trust-server-cert-failures=unknown-ca,cn-mismatch,expired,"
                         "not-yet-valid,other")

    load_args = [tools.svnadmin.path, "load", str(repo), "--quiet"]
    progress_re = re.compile(r"[*<] Dumped revision (\d+)")
    latest = [already]

    def watch(line: str) -> None:
        match = progress_re.search(line)
        if match:
            rev = int(match.group(1))
            latest[0] = rev
            if on_progress and head:
                on_progress(min(1.0, rev / head), f"mirrored r{rev:,} of r{head:,}")

    pipe(dump_args, load_args, cancel=cancel, on_stderr=watch)
    return max(0, latest[0] - already)


# --------------------------------------------------------------------------- #

def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _force_remove(func, path, _exc_info):
    """shutil.rmtree onerror hook - SVN leaves read-only files behind on Windows."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        log.debug("could not remove %s", path)
