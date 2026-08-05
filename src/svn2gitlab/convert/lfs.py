"""Git LFS planning and history rewriting.

SVN stores every revision of a binary in full, so repositories that carry installers,
CAD files or game assets routinely convert into Git histories far larger than GitLab
will accept. GitLab SaaS enforces a per-file limit on pushes and a per-namespace
repository size limit; LFS moves the bytes out of the packfile and into object
storage, where a separate (and much larger) quota applies.

Planning is done against the converted Git mirror rather than against SVN HEAD,
because the file that breaks the push is usually one that was deleted years ago and
is invisible in a HEAD listing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from ..config import LfsConfig
from ..errors import ConversionError
from ..logging_setup import get_logger
from ..tools import ToolSet, human_bytes
from .git import Git

log = get_logger("lfs")

LFS_POINTER_MAGIC = b"version https://git-lfs.github.com/spec/v1"
_POINTER_OID = re.compile(rb"^oid sha256:([0-9a-f]{64})$", re.M)
_POINTER_SIZE = re.compile(rb"^size (\d+)$", re.M)


@dataclass
class LfsPlan:
    threshold_bytes: int = 0
    patterns: List[str] = field(default_factory=list)
    matched_blobs: int = 0
    matched_bytes: int = 0
    largest: List[Tuple[str, int]] = field(default_factory=list)
    oversize_for_gitlab: List[Tuple[str, int]] = field(default_factory=list)
    extensions: Dict[str, Tuple[int, int]] = field(default_factory=dict)  # ext -> (count, bytes)

    @property
    def empty(self) -> bool:
        return not self.patterns and self.matched_blobs == 0

    def to_dict(self) -> dict:
        return {
            "threshold_bytes": self.threshold_bytes,
            "patterns": self.patterns,
            "matched_blobs": self.matched_blobs,
            "matched_bytes": self.matched_bytes,
            "largest": [{"path": p, "size": s} for p, s in self.largest[:50]],
            "oversize_for_gitlab": [{"path": p, "size": s} for p, s in self.oversize_for_gitlab[:50]],
            "extensions": {k: {"count": v[0], "bytes": v[1]} for k, v in self.extensions.items()},
        }


@dataclass
class LfsResult:
    migrated: bool = False
    patterns: List[str] = field(default_factory=list)
    pointer_count: int = 0
    tracked_bytes: int = 0
    gitattributes: str = ""

    def to_dict(self) -> dict:
        return {
            "migrated": self.migrated,
            "patterns": self.patterns,
            "pointer_count": self.pointer_count,
            "tracked_bytes": self.tracked_bytes,
        }


def plan(git: Git, config: LfsConfig, gitlab_file_limit_mb: int = 100,
         scan_limit: int = 20000) -> LfsPlan:
    """Decide what should move to LFS by scanning every blob in the history."""
    threshold = config.size_threshold_mb * 1024 * 1024
    result = LfsPlan(threshold_bytes=threshold)

    blobs = git.largest_blobs(limit=scan_limit, min_bytes=1)
    if not blobs:
        return result

    wanted_ext = {e.lower() for e in config.extensions}
    wanted_paths = [p for p in config.paths if p]
    matched_paths: Set[str] = set()
    ext_stats: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    limit_bytes = gitlab_file_limit_mb * 1024 * 1024

    for _sha, size, path in blobs:
        if not path:
            continue
        ext = _extension(path)
        by_size = size >= threshold
        by_ext = bool(ext) and ext in wanted_ext
        by_path = any(_glob_match(path, pattern) for pattern in wanted_paths)
        if size >= limit_bytes:
            result.oversize_for_gitlab.append((path, size))
        if not (by_size or by_ext or by_path):
            continue
        matched_paths.add(path)
        result.matched_blobs += 1
        result.matched_bytes += size
        stats = ext_stats[ext or "(no extension)"]
        stats[0] += 1
        stats[1] += size
        if len(result.largest) < 200:
            result.largest.append((path, size))

    result.largest.sort(key=lambda item: item[1], reverse=True)
    result.oversize_for_gitlab.sort(key=lambda item: item[1], reverse=True)
    result.extensions = {k: (v[0], v[1]) for k, v in
                         sorted(ext_stats.items(), key=lambda kv: kv[1][1], reverse=True)}
    result.patterns = _derive_patterns(matched_paths, wanted_ext, wanted_paths)

    log.info("LFS plan: %d blob(s), %s, %d pattern(s)",
             result.matched_blobs, human_bytes(result.matched_bytes), len(result.patterns))
    return result


def _extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _glob_match(path: str, pattern: str) -> bool:
    from fnmatch import fnmatch
    return fnmatch(path, pattern) or fnmatch(path.rsplit("/", 1)[-1], pattern)


def _derive_patterns(paths: Set[str], wanted_ext: Set[str], wanted_paths: Sequence[str]) -> List[str]:
    """Prefer a handful of `*.ext` rules over thousands of literal paths.

    An extension becomes a wildcard rule when it is unambiguous; anything else is
    listed literally so we never sweep a source file into LFS by accident.
    """
    by_ext: Dict[str, List[str]] = defaultdict(list)
    literal: List[str] = []
    for path in paths:
        ext = _extension(path)
        if ext:
            by_ext[ext].append(path)
        else:
            literal.append(path)

    patterns: List[str] = list(wanted_paths)
    for ext, members in sorted(by_ext.items()):
        if ext in wanted_ext or len(members) >= 3:
            patterns.append(f"*.{ext}")
        else:
            literal.extend(members)
    patterns.extend(sorted(literal))

    seen, unique = set(), []
    for pattern in patterns:
        if pattern not in seen:
            seen.add(pattern)
            unique.append(pattern)
    return unique


def migrate(
    git: Git,
    tools: ToolSet,
    lfs_plan: LfsPlan,
    config: LfsConfig,
    cancel: Optional[Event] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> LfsResult:
    """Rewrite matching blobs in the whole history into LFS pointers."""
    result = LfsResult(patterns=list(lfs_plan.patterns))
    if lfs_plan.empty:
        log.info("nothing matches the LFS criteria; skipping")
        return result

    tools.require("git-lfs")
    if on_progress:
        on_progress(0.05, f"installing LFS hooks for {len(lfs_plan.patterns)} pattern(s)")

    git.run(["lfs", "install", "--local", "--skip-smudge"], check=False, timeout=300)

    # `--include` takes a comma-separated list; long lists overflow the Windows command
    # line, so patterns are batched.
    batches = _batch_patterns(lfs_plan.patterns)
    for index, batch in enumerate(batches, 1):
        args = ["lfs", "migrate", "import", "--everything", "--yes",
                f"--include={','.join(batch)}"]
        if on_progress:
            on_progress(0.1 + 0.7 * (index / max(1, len(batches))),
                        f"rewriting history for pattern batch {index} of {len(batches)}")
        proc = git.run(args, check=False, timeout=None,
                       on_stderr=lambda line: log.debug("lfs: %s", line), collect_stdout=False)
        if not proc.ok:
            raise ConversionError(
                f"`git lfs migrate import` failed (exit {proc.returncode})",
                "Confirm Git LFS is installed and that the export repository has no "
                "uncommitted changes. Re-run with `--restart-from export` after fixing.",
            )

    # A size threshold with no matching extension rule still needs a sweep by size.
    if config.size_threshold_mb and not lfs_plan.patterns:
        git.run(["lfs", "migrate", "import", "--everything", "--yes",
                 f"--above={config.size_threshold_mb}MB"], check=False, timeout=None)

    result.migrated = True
    result.pointer_count, result.tracked_bytes = _count_pointers(git)
    result.gitattributes = _read_gitattributes(git)
    if on_progress:
        on_progress(1.0, f"{result.pointer_count:,} file(s) now tracked by LFS")
    log.info("LFS migration complete: %d pointer(s), %s",
             result.pointer_count, human_bytes(result.tracked_bytes))
    return result


def _batch_patterns(patterns: Sequence[str], max_chars: int = 6000) -> List[List[str]]:
    batches: List[List[str]] = []
    current: List[str] = []
    length = 0
    for pattern in patterns:
        if current and length + len(pattern) + 1 > max_chars:
            batches.append(current)
            current, length = [], 0
        current.append(pattern)
        length += len(pattern) + 1
    if current:
        batches.append(current)
    return batches or [[]]


def _count_pointers(git: Git) -> Tuple[int, int]:
    result = git.run(["lfs", "ls-files", "--all", "--size"], check=False, timeout=None)
    if not result.ok:
        return 0, 0
    count = 0
    total = 0
    size_re = re.compile(r"\(([\d.]+)\s*([KMGT]?B)\)")
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        count += 1
        match = size_re.search(line)
        if match:
            total += _to_bytes(float(match.group(1)), match.group(2))
    return count, total


def _to_bytes(value: float, unit: str) -> int:
    factors = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    return int(value * factors.get(unit.upper(), 1))


def _read_gitattributes(git: Git) -> str:
    path = git.repo / ".gitattributes"
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


# --------------------------------------------------------------------------- #
# Pointer handling (used by verification)
# --------------------------------------------------------------------------- #

def parse_pointer(content: bytes) -> Optional[Tuple[str, int]]:
    """Return `(sha256_oid, size)` if `content` is an LFS pointer file.

    Verification relies on this: an LFS pointer's OID *is* the SHA-256 of the original
    file, so we can prove the Git side matches the SVN side without downloading a
    single byte from object storage.
    """
    if not content.startswith(LFS_POINTER_MAGIC):
        return None
    oid = _POINTER_OID.search(content)
    size = _POINTER_SIZE.search(content)
    if not oid or not size:
        return None
    return oid.group(1).decode("ascii"), int(size.group(1))


def push_objects(git: Git, remote: str, cancel: Optional[Event] = None,
                 on_progress: Optional[Callable[[float, str], None]] = None) -> None:
    """Upload every LFS object referenced by any ref."""
    if on_progress:
        on_progress(0.0, "uploading LFS objects")
    proc = git.run(["lfs", "push", "--all", remote], check=False, timeout=None,
                   on_stderr=lambda line: on_progress and on_progress(0.5, line.strip()[:120]),
                   collect_stdout=False)
    if not proc.ok:
        raise ConversionError(
            f"`git lfs push --all {remote}` failed (exit {proc.returncode})",
            "Common causes: the namespace has no LFS storage quota left, LFS is disabled "
            "on the GitLab project (Settings > General > Visibility > Git LFS), or the "
            "access token lacks the write_repository scope.",
        )
    if on_progress:
        on_progress(1.0, "LFS objects uploaded")
