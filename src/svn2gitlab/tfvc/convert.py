"""TFVC changesets to Git, via the same `git fast-import` writer the SVN side uses.

The shape of the problem is close to Subversion's: a centralised server with global
sequential versions (changesets rather than revisions), branches represented as
folders under a server path (`$/Project/Main`), and branch creation recorded as a
copy. So the mapping logic mirrors `convert/native.py`, and the output is the same
kind of mirror — `refs/remotes/tfvc/*` with a trailer per commit recording which
changeset it came from — which is what lets the export, push, verify and sync stages
work unchanged.

Where TFVC differs, and why each matters:

* **`changeType` is a flags enum.** `rename, edit` is one change that both moves a
  file and alters its bytes. Handling only the rename leaves the new path holding
  the old content.
* **Branch creation enumerates every file.** TFVC emits a `branch` change per item,
  not a single directory copy. Replaying those literally would download an entire
  branch's worth of content that Git already has; instead the folder-level `branch`
  change is resolved with fast-import's `ls`, exactly as SVN directory copies are,
  and the per-file children are skipped.
* **Content arrives over HTTP, one item at a time.** Item content at a changeset is
  immutable and frequently duplicated across branches, so blobs are cached by
  content hash and written to the stream once.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..convert.fastimport import FastImport
from ..errors import ConversionError
from ..logging_setup import get_logger
from ..svn.authors import AuthorMap
from .client import Change, Changeset, TfvcClient

log = get_logger("tfvc.convert")

TFVC_PREFIX = "tfvc/"
MAIN_BRANCH_KEY = "\x00main"

# Folder names that conventionally hold branches rather than being one.
BRANCH_CONTAINERS = ("branches", "branch", "dev", "development", "releases", "release",
                     "features", "feature")
# Folder names that conventionally *are* the mainline.
MAIN_NAMES = ("main", "trunk", "master", "mainline", "current")


@dataclass
class TfvcLayout:
    """Which server paths are branches."""

    project_root: str                       # "$/DemoProject"
    branch_roots: List[str] = field(default_factory=list)   # full server paths
    main_root: str = ""
    detected_from: str = ""

    def to_dict(self) -> dict:
        return {"project_root": self.project_root, "main_root": self.main_root,
                "branch_roots": self.branch_roots, "detected_from": self.detected_from}


@dataclass
class TfvcResult:
    changesets: int = 0
    commits: int = 0
    branches: Dict[str, str] = field(default_factory=dict)
    skipped_paths: int = 0
    unresolved_branches: List[str] = field(default_factory=list)
    bytes_downloaded: int = 0
    blobs_written: int = 0
    blobs_deduplicated: int = 0
    last_changeset: int = 0
    duration: float = 0.0

    def to_dict(self) -> dict:
        return {
            "source": "tfvc",
            "changesets": self.changesets,
            "commits": self.commits,
            "branch_count": len(self.branches),
            "branches": sorted(self.branches),
            "skipped_paths": self.skipped_paths,
            "unresolved_branches": self.unresolved_branches[:50],
            "bytes_downloaded": self.bytes_downloaded,
            "blobs_written": self.blobs_written,
            "blobs_deduplicated": self.blobs_deduplicated,
            "last_changeset": self.last_changeset,
            "duration": round(self.duration, 1),
        }


def detect_layout(project_root: str, branch_paths: Sequence[str],
                  sample_paths: Sequence[str] = ()) -> TfvcLayout:
    """Work out the branch roots for a team project.

    TFVC's branch API only knows about folders explicitly *converted to branches*.
    Plenty of real projects branch by copying a folder and never convert it, so the
    API is treated as a strong hint and path conventions fill the gaps.
    """
    root = project_root.rstrip("/")
    layout = TfvcLayout(project_root=root)

    known = [p.rstrip("/") for p in branch_paths if p and p.rstrip("/") != root]
    if known:
        layout.branch_roots = sorted(set(known))
        layout.detected_from = "server branch definitions"
    else:
        # Fall back to the folders directly under the project root.
        candidates = set()
        prefix = root + "/"
        for path in sample_paths:
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):].split("/")
            if not rest or not rest[0]:
                continue
            first = rest[0]
            if first.lower() in BRANCH_CONTAINERS and len(rest) > 1:
                candidates.add(f"{root}/{first}/{rest[1]}")
            else:
                candidates.add(f"{root}/{first}")
        layout.branch_roots = sorted(candidates)
        layout.detected_from = "path conventions (no server branch definitions found)"

    for candidate in layout.branch_roots:
        if candidate.rsplit("/", 1)[-1].lower() in MAIN_NAMES:
            layout.main_root = candidate
            break
    if not layout.main_root and layout.branch_roots:
        layout.main_root = layout.branch_roots[0]
    return layout


class TfvcMapper:
    """Maps a TFVC server path onto (branch, path-within-branch)."""

    def __init__(self, layout: TfvcLayout, default_branch: str = "main") -> None:
        self.layout = layout
        self.default_branch = default_branch
        # Longest first, so `$/P/Branches/X` wins over `$/P/Branches`.
        self.roots = sorted(layout.branch_roots, key=len, reverse=True)

    def classify(self, path: str) -> Optional[Tuple[str, str, bool]]:
        """`(branch_key, subpath, is_branch_root)` or None if outside the layout."""
        clean = path.rstrip("/")
        for root in self.roots:
            if clean == root:
                return self.key_for(root), "", True
            if clean.startswith(root + "/"):
                return self.key_for(root), clean[len(root) + 1:], False
        return None

    def key_for(self, root: str) -> str:
        if root == self.layout.main_root:
            return MAIN_BRANCH_KEY
        return root.rsplit("/", 1)[-1]

    def branch_name(self, key: str) -> str:
        return self.default_branch if key == MAIN_BRANCH_KEY else key

    def ref(self, key: str) -> str:
        return f"refs/remotes/{TFVC_PREFIX}{self.branch_name(key)}"


class TfvcConverter:
    """Streams a TFVC history into a Git repository."""

    def __init__(
        self,
        client: TfvcClient,
        git_exe: str,
        repo_dir: Path,
        layout: TfvcLayout,
        authors: AuthorMap,
        default_branch: str = "main",
        default_domain: str = "localhost",
        env: Optional[Dict[str, str]] = None,
        cancel: Optional[Event] = None,
        collection_url: str = "",
    ) -> None:
        self.client = client
        self.git_exe = git_exe
        self.repo_dir = Path(repo_dir)
        self.mapper = TfvcMapper(layout, default_branch)
        self.authors = authors
        self.default_domain = default_domain
        self.env = env
        self.cancel = cancel
        self.collection_url = collection_url.rstrip("/")

        self.heads: Dict[str, object] = {}                      # key -> mark or sha
        self.history: Dict[str, List[Tuple[int, object]]] = {}   # key -> [(cs, ref)]
        # content md5 -> blob mark, so identical bytes are written to the stream once
        # however many branches or paths carry them.
        self._blob_marks: Dict[str, int] = {}
        self.result = TfvcResult()

    # -- identity -------------------------------------------------------------

    def _identity(self, changeset: Changeset) -> Tuple[str, str]:
        key = changeset.author.key
        identity = self.authors.get(key)
        if identity:
            return identity.name, identity.email
        # Fall back to the display name, then a synthesised address.
        from ..svn.authors import normalise_username, humanise
        base = normalise_username(key) or "tfs"
        name = changeset.author.display_name or humanise(base)
        return name, f"{base.lower()}@{self.default_domain}"

    # -- copy resolution ------------------------------------------------------

    def _head_at(self, key: str, changeset_id: int):
        entries = self.history.get(key)
        if not entries:
            return None
        best = None
        for cs_id, ref in entries:
            if cs_id <= changeset_id:
                best = ref
            else:
                break
        return best

    @staticmethod
    def _ref_of(mark_or_sha) -> str:
        return f":{mark_or_sha}" if isinstance(mark_or_sha, int) else str(mark_or_sha)

    # -- conversion -----------------------------------------------------------

    def convert(
        self,
        changesets: Sequence[Changeset],
        on_progress: Optional[Callable[[float, str], None]] = None,
    ) -> TfvcResult:
        started = time.time()
        total = len(changesets)
        last_report = [0.0]

        with FastImport(self.git_exe, self.repo_dir, env=self.env) as importer:
            self.seed_from_repository(importer)
            for index, changeset in enumerate(changesets, 1):
                if self.cancel is not None and self.cancel.is_set():
                    raise ConversionError("conversion cancelled")
                self._convert_changeset(importer, changeset)
                self.result.changesets += 1
                self.result.last_changeset = changeset.id

                now = time.time()
                if on_progress and (now - last_report[0] > 0.5 or index == total):
                    last_report[0] = now
                    on_progress(index / total if total else 1.0,
                                f"changeset {changeset.id} ({index:,} of {total:,})")

        self.result.duration = time.time() - started
        log.info("TFVC conversion: %d changesets -> %d commits across %d branch(es) in %.0fs "
                 "(%d blobs, %d deduplicated)",
                 self.result.changesets, self.result.commits, len(self.result.branches),
                 self.result.duration, self.result.blobs_written,
                 self.result.blobs_deduplicated)
        return self.result

    def seed_from_repository(self, _importer: FastImport) -> None:
        """Adopt refs already present so an incremental run continues them."""
        # Populated by the mirror wrapper before conversion; kept as a hook so the
        # incremental path mirrors the Subversion engine's.
        return

    def _convert_changeset(self, importer: FastImport, changeset: Changeset) -> None:
        try:
            changes = self.client.changeset_changes(changeset.id)
        except Exception as exc:
            raise ConversionError(
                f"could not read the changes in changeset {changeset.id}: {exc}",
                "The conversion stops rather than silently skipping a changeset, "
                "which would leave a hole in the migrated history.",
            ) from exc

        grouped: Dict[str, List[Change]] = {}
        for change in changes:
            placed = self.mapper.classify(change.path)
            if placed is None:
                self.result.skipped_paths += 1
                continue
            grouped.setdefault(placed[0], []).append(change)

        for key, items in grouped.items():
            self._commit(importer, changeset, key, items)

    def _commit(self, importer: FastImport, changeset: Changeset, key: str,
                changes: List[Change]) -> None:
        # A folder-level `branch` change means the whole tree was copied. Resolve it
        # from the source commit and drop the per-file children TFVC also emits,
        # rather than downloading a branch's worth of content Git already holds.
        branch_copy = self._branch_copy_source(changes)
        skip_prefixes: List[str] = []
        if branch_copy is not None:
            root_change, source_ref = branch_copy
            skip_prefixes.append(root_change.path.rstrip("/") + "/")

        payload: List[Tuple[Change, Optional[int]]] = []
        for change in changes:
            if change.is_folder:
                continue
            if any(change.path.startswith(prefix) for prefix in skip_prefixes) \
                    and change.is_branch_creation:
                continue
            mark = None
            if change.touches_content and not change.is_delete:
                mark = self._blob_for(importer, change)
            payload.append((change, mark))

        if not payload and branch_copy is None:
            return  # nothing representable in Git (locks, property-only changes)

        name, email = self._identity(changeset)
        when = changeset.git_date()
        message = self._message(changeset, key)

        parent_ref = self.heads.get(key)
        parent = self._ref_of(parent_ref) if parent_ref is not None else None
        if parent is None and branch_copy is not None:
            parent = branch_copy[1]

        mark = importer.begin_commit(
            ref=self.mapper.ref(key),
            author=(name, email, when),
            committer=(name, email, when),
            message=message,
            parent=parent,
        )

        if branch_copy is not None:
            root_change, source_ref = branch_copy
            result = importer.ls(source_ref, "")
            if result.missing:
                self.result.unresolved_branches.append(
                    f"cs{changeset.id}: {root_change.path} <- {root_change.source_path}")
            else:
                if parent is not None and parent != source_ref:
                    importer.deleteall()
                importer.filemodify("040000", result.dataref, "")

        for change, blob_mark in payload:
            self._apply(importer, change, blob_mark)

        importer.end_commit()

        self.heads[key] = mark
        self.history.setdefault(key, []).append((changeset.id, mark))
        self.result.commits += 1
        self.result.branches[self.mapper.branch_name(key)] = f":{mark}"

    def _branch_copy_source(self, changes: List[Change]):
        """The folder-level branch change and the commit to copy its tree from."""
        for change in changes:
            if not (change.is_folder and change.is_branch_creation and change.source_path):
                continue
            source = self.mapper.classify(change.source_path)
            if source is None:
                continue
            head = self._head_at(source[0], 10 ** 9)
            if head is None:
                continue
            return change, self._ref_of(head)
        return None

    def _blob_for(self, importer: FastImport, change: Change) -> Optional[int]:
        """Write the item's content as a blob, reusing one already in the stream."""
        digest = change.md5
        if digest and digest in self._blob_marks:
            self.result.blobs_deduplicated += 1
            return self._blob_marks[digest]

        content = self.client.item_content(
            change.path, change.version,
            expected_md5=digest, expected_size=change.size if change.size else -1,
        )
        self.result.bytes_downloaded += len(content)
        mark = importer.blob(content)
        self.result.blobs_written += 1
        if digest:
            self._blob_marks[digest] = mark
        return mark

    def _apply(self, importer: FastImport, change: Change,
               blob_mark: Optional[int]) -> None:
        placed = self.mapper.classify(change.path)
        if placed is None:
            return
        _key, subpath, is_root = placed
        if is_root or not subpath:
            return

        if change.is_delete:
            importer.filedelete(subpath)
            return

        # A rename moves the old path away first. When the change also carries an
        # edit, the new content is written below - handling only the move would
        # leave the renamed file holding its previous bytes.
        if change.is_rename and change.source_path:
            old = self.mapper.classify(change.source_path)
            if old is not None and old[1]:
                importer.filedelete(old[1])

        if blob_mark is not None:
            mode = "120000" if change.is_symlink else "100644"
            importer.filemodify(mode, f":{blob_mark}", subpath)

    def _message(self, changeset: Changeset, key: str) -> bytes:
        text = (changeset.message or "").rstrip()
        body = text.encode("utf-8", "surrogateescape") if text else b""
        # The same role `git-svn-id` plays on the Subversion side: it records which
        # changeset a commit came from, which is what makes incremental sync work.
        location = f"{self.collection_url}{self.mapper.layout.project_root}" \
            if self.collection_url else self.mapper.layout.project_root
        trailer = f"tfs-changeset-id: {location}@{changeset.id}".encode("utf-8")
        message = (body + b"\n\n" + trailer) if body else trailer
        return message + b"\n"
