"""Verifying a TFVC migration against the source of truth.

Same claim as the Subversion side, and the same technique: every file in the
migrated branch is compared byte-for-byte with what TFVC holds, by computing the
*Git* blob hash of the source content and matching it against the tree. Identical
hashes prove identical bytes, with no checkout and no second copy of the repository.

One genuine advantage over the Subversion path: TFVC reports an MD5 for every item,
so the size and hash of each downloaded file are checked before it is compared,
which catches a truncated or substituted download rather than reporting it as a
content difference.
"""

from __future__ import annotations

import hashlib
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..logging_setup import get_logger
from ..convert.lfs import parse_pointer
from ..verify.verify import (BranchVerification, Difference, git_blob_hash,
                             normalised_blob_hash_bytes)
from .client import TfvcClient

log = get_logger("tfvc.verify")


def verify_branch(
    client: TfvcClient,
    git_tree: Dict[str, Tuple[str, str]],
    read_blob: Callable[[str], bytes],
    branch: str,
    branch_root: str,
    changeset: int,
    normalize_eol: bool = True,
    max_reported: int = 200,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> BranchVerification:
    """Compare one converted branch against TFVC at a changeset.

    `git_tree` maps repository-relative path -> (mode, blob sha); `read_blob` returns
    the bytes for a blob sha. Both come from the shared verifier so the Git side is
    read exactly the same way for either source.
    """
    started = time.time()
    record = BranchVerification(branch=branch, git_ref=branch)
    record.svn_revision = changeset

    if on_progress:
        on_progress(0.05, f"listing {branch_root} at changeset {changeset}")

    try:
        items = client.list_items(branch_root, version=changeset, recursive=True)
    except Exception as exc:
        record.skipped = True
        record.note = f"could not list {branch_root} at changeset {changeset}: {exc}"
        return record

    prefix = branch_root.rstrip("/") + "/"
    source: Dict[str, Dict] = {}
    for item in items:
        if item.get("isFolder"):
            continue
        path = item.get("path", "")
        if not path.startswith(prefix):
            continue
        source[path[len(prefix):]] = item

    total = len(source)
    checked = 0

    for relative, item in sorted(source.items()):
        checked += 1
        if on_progress and total and checked % 25 == 0:
            on_progress(0.1 + 0.85 * checked / total,
                        f"{checked:,} of {total:,} files")

        entry = git_tree.get(relative)
        if entry is None:
            record.differences.append(Difference(
                path=relative, kind="missing-in-git",
                detail="present in TFVC but absent from the migrated branch"))
            continue

        _mode, sha = entry
        git_bytes = read_blob(sha)

        expected_md5 = _md5_hex(item.get("hashValue") or "")
        try:
            source_bytes = client.item_content(
                item.get("path", ""), int(item.get("version") or changeset),
                expected_md5=expected_md5,
                expected_size=int(item.get("size", -1)),
            )
        except Exception as exc:
            record.differences.append(Difference(
                path=relative, kind="content", detail=str(exc)))
            continue

        # A file moved into Git LFS is a pointer whose OID is the SHA-256 of the
        # original bytes, so it verifies without downloading anything from GitLab.
        pointer = parse_pointer(git_bytes)
        if pointer:
            oid, size = pointer
            record.lfs_files += 1
            if hashlib.sha256(source_bytes).hexdigest() == oid and len(source_bytes) == size:
                record.files_matched += 1
            else:
                record.differences.append(Difference(
                    path=relative, kind="content",
                    detail="Git LFS pointer does not match the TFVC content"))
            record.files_compared += 1
            continue

        record.files_compared += 1
        if git_blob_hash(source_bytes) == sha:
            record.files_matched += 1
            continue

        if normalize_eol and normalised_blob_hash_bytes(source_bytes) == \
                normalised_blob_hash_bytes(git_bytes):
            record.eol_normalised_matches += 1
            record.files_matched += 1
            continue

        record.differences.append(Difference(
            path=relative, kind="content",
            detail=f"TFVC {len(source_bytes)} bytes, Git {len(git_bytes)} bytes"))

    for relative in sorted(set(git_tree) - set(source)):
        # `.gitignore` and `.gitkeep` are ours, not TFVC's.
        if relative in (".gitignore",) or relative.endswith("/.gitkeep"):
            continue
        record.differences.append(Difference(
            path=relative, kind="missing-in-svn",
            detail="present in the migrated branch but absent from TFVC"))

    if len(record.differences) > max_reported:
        extra = len(record.differences) - max_reported
        record.differences = record.differences[:max_reported]
        record.note = f"{extra} further difference(s) not listed"

    record.duration = time.time() - started
    log.info("branch %s: %d/%d files matched, %d difference(s)",
             branch, record.files_matched, record.files_compared,
             len(record.differences))
    return record


def _md5_hex(base64_value: str) -> str:
    if not base64_value:
        return ""
    import base64 as _b64
    try:
        return _b64.b64decode(base64_value).hex()
    except (ValueError, TypeError):
        return ""
