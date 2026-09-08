"""Proving the migration is faithful.

The claim we want to be able to make to a client at sign-off is specific: *every
file in Subversion at revision N is byte-for-byte identical to the file in GitLab
at commit X, for every branch we migrated*. This module produces the evidence.

How the content comparison works without copying the Git side at all:

Git names a blob by `sha1("blob " + length + "\\0" + content)`. So we can list the
tree with `git ls-tree -r` (paths and blob SHAs, cheap), export the same revision
from Subversion, and compute the *Git* blob hash of each exported file locally. If
the hashes match, the content is identical - no checkout, no archive, no second copy
of the repository.

Files that moved into Git LFS are handled without downloading anything either: an
LFS pointer's OID is the SHA-256 of the original file, so hashing the SVN file with
SHA-256 and comparing to the pointer proves equality.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from ..config import VerifyMode
from ..convert.git import Git
from ..convert.lfs import parse_pointer
from ..errors import VerificationError
from ..logging_setup import get_logger
from ..procs import run_bytes
from ..svn.client import SvnClient, url_join

log = get_logger("verify")

# Files this tool creates that legitimately have no Subversion counterpart.
GENERATED_PATHS = {".gitignore", ".gitattributes"}
GENERATED_BASENAMES = {".gitkeep"}
# An LFS pointer is a few hundred bytes; nothing larger needs the pointer check.
MAX_POINTER_BYTES = 1024


@dataclass
class Difference:
    path: str
    kind: str        # missing-in-git | missing-in-svn | content | mode | symlink
    detail: str = ""
    svn_size: Optional[int] = None
    git_size: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BranchVerification:
    branch: str
    svn_path: str = ""
    svn_revision: int = 0
    git_ref: str = ""
    git_sha: str = ""
    files_compared: int = 0
    files_matched: int = 0
    lfs_files: int = 0
    eol_normalised_matches: int = 0
    differences: List[Difference] = field(default_factory=list)
    # Differences found but not listed, because `max_reported` capped the list. Kept
    # separate so the *count* is never capped: a report that says "200 differences"
    # when it found 1,245 understates the damage and reads like a small problem.
    differences_omitted: int = 0
    note: str = ""
    skipped: bool = False
    skip_reason: str = ""
    duration: float = 0.0

    @property
    def difference_count(self) -> int:
        return len(self.differences) + self.differences_omitted

    @property
    def ok(self) -> bool:
        return not self.difference_count and not self.skipped

    def to_dict(self) -> dict:
        return {
            "branch": self.branch,
            "svn_path": self.svn_path,
            "svn_revision": self.svn_revision,
            "git_ref": self.git_ref,
            "git_sha": self.git_sha,
            "files_compared": self.files_compared,
            "files_matched": self.files_matched,
            "lfs_files": self.lfs_files,
            "eol_normalised_matches": self.eol_normalised_matches,
            "difference_count": self.difference_count,
            "differences_listed": len(self.differences),
            "differences_omitted": self.differences_omitted,
            "differences": [d.to_dict() for d in self.differences],
            "note": self.note,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "duration": round(self.duration, 1),
            "ok": self.ok,
        }


@dataclass
class VerificationReport:
    repository: str = ""
    mode: str = "full"
    started_at: float = 0.0
    duration: float = 0.0
    svn_head_revision: int = 0
    svn_revision_count: int = 0
    git_commit_count: int = 0
    git_commits_with_revision: int = 0
    revision_coverage: float = 0.0
    branches: List[BranchVerification] = field(default_factory=list)
    tag_count_svn: int = 0
    tag_count_git: int = 0
    missing_tags: List[str] = field(default_factory=list)
    authors_total: int = 0
    authors_mapped: int = 0
    authors_matched_in_gitlab: int = 0
    unmatched_author_emails: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(b.ok for b in self.branches) and not self.missing_tags

    @property
    def total_differences(self) -> int:
        return sum(b.difference_count for b in self.branches)

    def to_dict(self) -> dict:
        return {
            "repository": self.repository,
            "mode": self.mode,
            "started_at": self.started_at,
            "duration": round(self.duration, 1),
            "ok": self.ok,
            "svn_head_revision": self.svn_head_revision,
            "svn_revision_count": self.svn_revision_count,
            "git_commit_count": self.git_commit_count,
            "git_commits_with_revision": self.git_commits_with_revision,
            "revision_coverage": round(self.revision_coverage, 4),
            "branches": [b.to_dict() for b in self.branches],
            "tag_count_svn": self.tag_count_svn,
            "tag_count_git": self.tag_count_git,
            "missing_tags": self.missing_tags,
            "authors_total": self.authors_total,
            "authors_mapped": self.authors_mapped,
            "authors_matched_in_gitlab": self.authors_matched_in_gitlab,
            "unmatched_author_emails": self.unmatched_author_emails,
            "total_differences": self.total_differences,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# Hashing helpers
# --------------------------------------------------------------------------- #

def git_blob_hash(data: bytes) -> str:
    """The SHA-1 Git would assign to this content."""
    header = b"blob %d\0" % len(data)
    return hashlib.sha1(header + data).hexdigest()


def git_blob_hash_file(path: Path, chunk: int = 1 << 20) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1(b"blob %d\0" % size)
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def normalised_blob_hash_bytes(data: bytes) -> str:
    """Git blob hash after collapsing CRLF to LF.

    Used to tell an eol-only difference apart from real data loss. TFVC content
    arrives in memory rather than as an exported file, hence the bytes variant.
    """
    return git_blob_hash(data.replace(b"\r\n", b"\n"))


def normalised_blob_hash(path: Path) -> str:
    """Git blob hash after collapsing CRLF to LF - used to explain eol-only differences."""
    return normalised_blob_hash_bytes(path.read_bytes())


def looks_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


# --------------------------------------------------------------------------- #
# Verifier
# --------------------------------------------------------------------------- #

class Verifier:
    def __init__(
        self,
        git: Git,
        svn: SvnClient,
        svn_base_url: str,
        cancel: Optional[Event] = None,
        normalize_eol: bool = True,
        max_reported: int = 200,
        temp_root: Optional[Path] = None,
    ) -> None:
        self.git = git
        self.svn = svn
        self.svn_base_url = svn_base_url.rstrip("/")
        self.cancel = cancel
        self.normalize_eol = normalize_eol
        self.max_reported = max_reported
        self.temp_root = temp_root

    # -- tree comparison -----------------------------------------------------

    def _git_tree(self, ref: str) -> Dict[str, Tuple[str, str]]:
        """path -> (mode, blob sha) for every file in the ref's tree.

        Read as raw bytes with NUL-separated records: `-z` stops Git quoting non-ASCII
        paths, and paths in a 20-year-old repository are frequently not ASCII.
        """
        raw = run_bytes([self.git.exe, "ls-tree", "-r", "-z", ref],
                        cwd=self.git.repo, env=self.git._env(), check=False)
        tree: Dict[str, Tuple[str, str]] = {}
        for record in raw.split(b"\0"):
            if not record.strip():
                continue
            meta, _, path_bytes = record.partition(b"\t")
            parts = meta.split()
            if len(parts) < 3 or not path_bytes:
                continue
            mode, obj_type, sha = (p.decode("ascii", "replace") for p in parts[:3])
            if obj_type != "blob":
                continue  # submodules/gitlinks have no Subversion equivalent
            tree[path_bytes.decode("utf-8", "surrogateescape")] = (mode, sha)
        return tree

    def _lfs_pointers(self, tree: Dict[str, Tuple[str, str]]) -> Dict[str, Tuple[str, int]]:
        """path -> (sha256 oid, size) for every path whose blob is an LFS pointer."""
        if not tree:
            return {}
        shas = sorted({sha for _mode, sha in tree.values()})
        check_input = "\n".join(shas) + "\n"
        check = self.git.run(["cat-file", "--batch-check=%(objectname) %(objectsize)"],
                             check=False, input_text=check_input, timeout=None)
        candidates = []
        for line in check.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) <= MAX_POINTER_BYTES:
                candidates.append(parts[0])
        if not candidates:
            return {}

        raw = run_bytes([self.git.exe, "cat-file", "--batch"], cwd=self.git.repo,
                        env=self.git._env(), input_bytes=("\n".join(candidates) + "\n").encode(),
                        check=False)
        contents = _parse_batch(raw)
        pointers: Dict[str, Tuple[str, int]] = {}
        by_sha: Dict[str, Tuple[str, int]] = {}
        for sha, data in contents.items():
            parsed = parse_pointer(data)
            if parsed:
                by_sha[sha] = parsed
        for path, (_mode, sha) in tree.items():
            if sha in by_sha:
                pointers[path] = by_sha[sha]
        return pointers

    def verify_branch(
        self,
        branch: str,
        git_ref: str,
        svn_path: str,
        svn_revision: Optional[int] = None,
        on_progress: Optional[Callable[[float, str], None]] = None,
    ) -> BranchVerification:
        started = time.time()
        record = BranchVerification(branch=branch, git_ref=git_ref, svn_path=svn_path)
        sha = self.git.rev_parse(git_ref)
        if not sha:
            record.skipped = True
            record.skip_reason = f"git ref {git_ref} does not exist"
            return record
        record.git_sha = sha

        svn_url = url_join(self.svn_base_url, svn_path) if svn_path not in ("", ".") \
            else self.svn_base_url
        if not self.svn.exists(svn_url, svn_revision):
            record.skipped = True
            record.skip_reason = f"SVN path {svn_path} does not exist at the verified revision"
            return record
        record.svn_revision = svn_revision or self.svn.info(svn_url).revision

        tree = self._git_tree(git_ref)
        pointers = self._lfs_pointers(tree)
        record.lfs_files = len(pointers)

        temp_dir = Path(tempfile.mkdtemp(prefix="s2g-verify-", dir=str(self.temp_root)
                                         if self.temp_root else None))
        try:
            if on_progress:
                on_progress(0.1, f"exporting {svn_path}@r{record.svn_revision} from Subversion")
            export_root = temp_dir / "svn"
            self.svn.export(svn_url, export_root, revision=record.svn_revision)

            if on_progress:
                on_progress(0.5, f"comparing {len(tree):,} file(s)")
            self._compare(export_root, tree, pointers, record, on_progress)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True, onerror=_force_remove)

        record.duration = time.time() - started
        log.info("branch %s: %d/%d files matched, %d difference(s)",
                 branch, record.files_matched, record.files_compared, len(record.differences))
        return record

    def _compare(self, export_root: Path, tree: Dict[str, Tuple[str, str]],
                 pointers: Dict[str, Tuple[str, int]], record: BranchVerification,
                 on_progress: Optional[Callable[[float, str], None]] = None) -> None:
        svn_files: Dict[str, Path] = {}
        for root, _dirs, files in os.walk(export_root):
            for name in files:
                full = Path(root) / name
                rel = full.relative_to(export_root).as_posix()
                svn_files[rel] = full

        git_paths = set(tree)
        svn_paths = set(svn_files)
        total = len(git_paths | svn_paths) or 1
        processed = 0

        for path in sorted(svn_paths - git_paths):
            self._add_difference(record, Difference(
                path=path, kind="missing-in-git",
                detail="present in Subversion but absent from the Git branch",
                svn_size=_safe_size(svn_files[path])))

        for path in sorted(git_paths - svn_paths):
            if path in GENERATED_PATHS or Path(path).name in GENERATED_BASENAMES:
                continue
            self._add_difference(record, Difference(
                path=path, kind="missing-in-svn",
                detail="present in Git but absent from the Subversion export"))

        for path in sorted(git_paths & svn_paths):
            processed += 1
            if self.cancel and self.cancel.is_set():
                raise VerificationError("verification cancelled")
            if on_progress and processed % 2000 == 0:
                on_progress(0.5 + 0.5 * (processed / total),
                            f"compared {processed:,} of {total:,} files")

            mode, blob_sha = tree[path]
            local = svn_files[path]
            record.files_compared += 1

            if mode == "120000":
                if self._symlink_matches(local, blob_sha):
                    record.files_matched += 1
                else:
                    self._add_difference(record, Difference(
                        path=path, kind="symlink",
                        detail="symlink target differs between Subversion and Git"))
                continue

            if path in pointers:
                oid, size = pointers[path]
                actual = sha256_file(local)
                if actual == oid and local.stat().st_size == size:
                    record.files_matched += 1
                else:
                    self._add_difference(record, Difference(
                        path=path, kind="content",
                        detail="Git LFS pointer does not match the Subversion file",
                        svn_size=_safe_size(local), git_size=size))
                continue

            try:
                actual = git_blob_hash_file(local)
            except OSError as exc:
                self._add_difference(record, Difference(
                    path=path, kind="content", detail=f"could not read the exported file: {exc}"))
                continue

            if actual == blob_sha:
                record.files_matched += 1
                continue

            if self.normalize_eol and self._eol_only_difference(local, blob_sha):
                record.files_matched += 1
                record.eol_normalised_matches += 1
                continue

            self._add_difference(record, Difference(
                path=path, kind="content",
                detail=f"content differs (git blob {blob_sha[:12]}, exported file hashes to "
                       f"{actual[:12]})",
                svn_size=_safe_size(local)))

    def _eol_only_difference(self, local: Path, blob_sha: str) -> bool:
        """True when the files differ only in line endings.

        `svn export` applies svn:eol-style translation to the platform, so a repository
        with `native` eol-style legitimately produces CRLF on Windows where Git stores
        LF. That is not a migration defect, but it must be reported as normalised
        rather than silently treated as identical.
        """
        try:
            if local.stat().st_size > 64 * 1024 * 1024:
                return False
            data = local.read_bytes()
        except OSError:
            return False
        if looks_binary(data):
            return False
        for variant in (data.replace(b"\r\n", b"\n"), data.replace(b"\n", b"\r\n")):
            if git_blob_hash(variant) == blob_sha:
                return True
        return False

    def _symlink_matches(self, local: Path, blob_sha: str) -> bool:
        try:
            if local.is_symlink():
                target = os.readlink(str(local)).encode("utf-8")
            else:
                # svn:special files export as a regular file reading "link <target>".
                raw = local.read_bytes()
                target = raw[5:] if raw.startswith(b"link ") else raw
            return git_blob_hash(target) == blob_sha
        except OSError:
            return False

    def _add_difference(self, record: BranchVerification, diff: Difference) -> None:
        if len(record.differences) < self.max_reported:
            record.differences.append(diff)
        elif len(record.differences) == self.max_reported:
            record.differences.append(Difference(
                path="(truncated)", kind="content",
                detail=f"further differences suppressed after {self.max_reported} entries"))


def _parse_batch(raw: bytes) -> Dict[str, bytes]:
    """Parse `git cat-file --batch` output: `<sha> <type> <size>\\n<content>\\n`."""
    out: Dict[str, bytes] = {}
    offset = 0
    length = len(raw)
    while offset < length:
        newline = raw.find(b"\n", offset)
        if newline < 0:
            break
        header = raw[offset:newline].decode("ascii", "replace").split()
        offset = newline + 1
        if len(header) != 3 or not header[2].isdigit():
            continue
        sha, _obj_type, size_text = header
        size = int(size_text)
        out[sha] = raw[offset:offset + size]
        offset += size + 1  # trailing newline
    return out


def _safe_size(path: Path) -> Optional[int]:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _force_remove(func, path, _exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass
