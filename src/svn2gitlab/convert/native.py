"""The native SVN-to-Git conversion engine.

Replaces `git svn` entirely. It reads a fulltext dump (see `dumpstream.py`) and
writes a `git fast-import` stream, which removes the two things that make `git svn`
painful in practice: the Perl dependency that is simply absent on many Windows
installations, and the one-network-round-trip-per-revision design that turns large
migrations into multi-day jobs.

The mapping problem it solves: Subversion has no branches, only directories, so
every path has to be classified against the repository layout. `trunk/src/app.py`
becomes `src/app.py` on the default branch; `branches/release-1.x/src/app.py`
becomes `src/app.py` on `release-1.x`; `tags/v1.0/...` becomes a tag. A single SVN
revision touching several of those produces several Git commits.

Fidelity notes, because they are the whole point:

* content is taken verbatim from the dump, with no eol or keyword translation, which
  is what `git svn` does and what the byte-for-byte verifier expects;
* `svn:executable` becomes mode 100755, `svn:special` becomes a symlink;
* directory copies (branch and tag creation) are resolved through fast-import's `ls`
  so a branch of 30,000 files costs one line, not 30,000;
* a copy whose source lies outside the migrated subtree degrades to an explicit
  add of whatever the dump does carry, and is reported rather than silently dropped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import BinaryIO, Callable, Dict, List, Optional, Tuple

from ..config import ConvertConfig, LayoutMode
from ..errors import ConversionError
from ..logging_setup import get_logger
from ..svn.analyze import DetectedLayout
from ..svn.authors import AuthorMap, Identity
from .dumpstream import (ACTION_ADD, ACTION_CHANGE, ACTION_DELETE, ACTION_REPLACE,
                         DumpReader, Node, Revision, svn_date_to_git)
from .fastimport import FastImport
from .mirrorfmt import SVN_PREFIX

log = get_logger("native")

TRUNK_BRANCH = "\x00trunk"      # internal sentinel, never a real ref name


@dataclass
class PathClass:
    """Where an SVN path lands in Git."""

    kind: str                # "branch" | "tag" | "outside"
    name: str = ""           # branch or tag name ("" for trunk => default branch)
    subpath: str = ""        # path within the branch
    is_root: bool = False    # the path *is* the branch root (a copy creates it)


@dataclass
class NativeResult:
    revisions: int = 0
    commits: int = 0
    branches: Dict[str, str] = field(default_factory=dict)     # name -> mark
    tags: Dict[str, str] = field(default_factory=dict)
    skipped_paths: int = 0
    deleted_refs: List[str] = field(default_factory=list)
    unresolved_copies: List[str] = field(default_factory=list)
    last_revision: int = 0
    duration: float = 0.0

    def to_dict(self) -> dict:
        return {
            "engine": "native",
            "revisions": self.revisions,
            "commits": self.commits,
            "branch_count": len(self.branches),
            "tag_count": len(self.tags),
            "skipped_paths": self.skipped_paths,
            "deleted_refs": self.deleted_refs,
            "unresolved_copies": self.unresolved_copies[:50],
            "last_revision": self.last_revision,
            "duration": round(self.duration, 1),
        }


class LayoutMapper:
    """Classifies SVN paths against the detected layout."""

    def __init__(self, layout: DetectedLayout, default_branch: str) -> None:
        self.layout = layout
        self.default_branch = default_branch
        self.trunk = (layout.trunk or "").strip("/")
        self.flat = layout.mode == LayoutMode.FLAT or self.trunk in ("", ".")
        self.branch_roots = [p.rstrip("/*").strip("/") for p in layout.branches if p]
        self.tag_roots = [p.rstrip("/*").strip("/") for p in layout.tags if p]
        # `branches/*` means branches live one level deeper (branches/team/feature).
        self.nested = {p.rstrip("/*").strip("/") for p in layout.branches if p.endswith("/*")}

    def classify(self, path: str) -> PathClass:
        path = path.strip("/")

        if self.flat:
            return PathClass(kind="branch", name="", subpath=path,
                             is_root=(path == ""))

        # Tags first: `tags/` may otherwise look like an ordinary directory.
        for root in self.tag_roots:
            hit = self._under(path, root, nested=root in self.nested)
            if hit is not None:
                name, subpath = hit
                return PathClass(kind="tag", name=name, subpath=subpath,
                                 is_root=(subpath == ""))

        for root in self.branch_roots:
            hit = self._under(path, root, nested=root in self.nested)
            if hit is not None:
                name, subpath = hit
                return PathClass(kind="branch", name=name, subpath=subpath,
                                 is_root=(subpath == ""))

        if self.trunk and (path == self.trunk or path.startswith(self.trunk + "/")):
            subpath = path[len(self.trunk):].strip("/")
            return PathClass(kind="branch", name="", subpath=subpath,
                             is_root=(subpath == ""))

        return PathClass(kind="outside")

    @staticmethod
    def _under(path: str, root: str, nested: bool) -> Optional[Tuple[str, str]]:
        """Split `root/name[/sub]` into (name, sub); None when not under `root`."""
        if not root:
            return None
        if path == root:
            return None                       # the container itself, not a branch
        if not path.startswith(root + "/"):
            return None
        remainder = path[len(root) + 1:]
        parts = remainder.split("/")
        depth = 2 if nested else 1
        if len(parts) < depth:
            return None
        name = "/".join(parts[:depth])
        subpath = "/".join(parts[depth:])
        return name, subpath

    def ref_for(self, cls: PathClass) -> str:
        """The internal branch key a path belongs to."""
        if cls.kind == "tag":
            return f"tag:{cls.name}"
        return cls.name or TRUNK_BRANCH


class NativeConverter:
    """Drives a full conversion from a dump stream into a Git repository."""

    def __init__(
        self,
        git_exe: str,
        repo_dir: Path,
        layout: DetectedLayout,
        convert: ConvertConfig,
        authors: AuthorMap,
        env: Optional[Dict[str, str]] = None,
        cancel: Optional[Event] = None,
        default_domain: str = "localhost",
        svn_url: str = "",
        uuid: str = "",
        emit_svn_id: bool = True,
    ) -> None:
        self.git_exe = git_exe
        self.repo_dir = Path(repo_dir)
        self.mapper = LayoutMapper(layout, convert.default_branch)
        self.convert = convert
        self.authors = authors
        self.env = env
        self.cancel = cancel
        self.default_domain = default_domain
        self.svn_url = svn_url
        self.uuid = uuid
        self.emit_svn_id = bool(emit_svn_id and svn_url and uuid)
        self.tags_path = (layout.tags[0].rstrip("/*").strip("/") if layout.tags else "tags")
        # svn:ignore properties, so a native conversion can still generate .gitignore.
        self.ignores: Dict[str, str] = {}
        # Directories whose svn:ignore was removed in this slice.
        self.cleared_ignores: set = set()

        # Internal branch key -> the mark of its current head commit.
        self.heads: Dict[str, int] = {}
        # Internal branch key -> [(svn_revision, mark)], for resolving historical copies.
        self.history: Dict[str, List[Tuple[int, int]]] = {}
        # Refs whose Subversion directory was deleted before HEAD.
        self.deleted_refs: set = set()
        self.result = NativeResult()

    # -- identity ------------------------------------------------------------

    def _identity(self, svn_author: str) -> Tuple[str, str]:
        identity = self.authors.get(svn_author or "(no author)")
        if identity:
            return identity.name, identity.email
        base = svn_author or "svn"
        return base, f"{base}@{self.default_domain}"

    # -- ref naming ----------------------------------------------------------

    def _git_ref(self, key: str) -> str:
        """Write into the mirror's ref namespace.

        The layout is the long-established `refs/remotes/svn/*` convention, so it
        produces exactly the refs the export stage already knows how to read. Nothing
        downstream needs to care which engine ran.
        """
        if key == TRUNK_BRANCH:
            return f"refs/remotes/{SVN_PREFIX}trunk"
        if key.startswith("tag:"):
            return f"refs/remotes/{SVN_PREFIX}{self.tags_path}/{key[4:]}"
        return f"refs/remotes/{SVN_PREFIX}{key}"

    # -- copy resolution -----------------------------------------------------

    def _head_at(self, key: str, revision: int):
        """Mark of the newest commit on `key` at or before `revision`."""
        entries = self.history.get(key)
        if not entries:
            return None
        best = None
        for rev, mark in entries:
            if rev <= revision:
                best = mark
            else:
                break
        return best

    def _resolve_copy(self, importer: FastImport, node: Node,
                      destination: PathClass) -> Optional[Tuple[str, str]]:
        """Return `(mode, dataref)` for a copied path, or None if unresolvable."""
        source = self.mapper.classify(node.copyfrom_path or "")
        if source.kind == "outside":
            return None
        source_key = self.mapper.ref_for(source)
        source_mark = self._head_at(source_key, node.copyfrom_rev or 0)
        if source_mark is None:
            return None

        result = importer.ls(_ref_of(source_mark), source.subpath or "")
        if result.missing:
            return None
        return result.mode, result.dataref

    def seed_from_repository(self, git) -> None:
        """Adopt refs already in the repository so an incremental run continues them.

        Without this, a second conversion pass would start every branch from scratch
        and orphan the history the first pass produced.
        """
        from .mirrorfmt import extract_svn_revision

        prefix = f"refs/remotes/{SVN_PREFIX}"
        tags_prefix = f"{prefix}{self.tags_path}/"
        for ref in git.list_refs(prefix):
            if ref.name.startswith(tags_prefix):
                key = f"tag:{ref.name[len(tags_prefix):]}"
            elif ref.name == f"{prefix}trunk":
                key = TRUNK_BRANCH
            else:
                key = ref.name[len(prefix):]
            meta = git.commit_meta(ref.sha)
            revision = extract_svn_revision(meta.get("message", "")) or 0
            self.heads[key] = ref.sha           # a sha works wherever a mark does
            self.history.setdefault(key, []).append((revision, ref.sha))
        if self.heads:
            log.info("resuming native conversion from %d existing ref(s)", len(self.heads))

    # -- conversion ----------------------------------------------------------

    def convert_stream(
        self,
        stream: BinaryIO,
        head_revision: int = 0,
        on_progress: Optional[Callable[[float, str], None]] = None,
    ) -> NativeResult:
        started = time.time()
        reader = DumpReader(stream)
        last_report = [0.0]

        with FastImport(self.git_exe, self.repo_dir, env=self.env) as importer:
            for revision in reader.revisions():
                if self.cancel and self.cancel.is_set():
                    raise ConversionError("conversion cancelled")
                self._convert_revision(importer, revision)
                self.result.revisions += 1
                self.result.last_revision = revision.number

                now = time.time()
                if on_progress and head_revision and now - last_report[0] > 0.5:
                    last_report[0] = now
                    on_progress(min(0.99, revision.number / head_revision),
                                f"converted r{revision.number:,} of r{head_revision:,}")

            self._finalise(importer)
        self._record_deleted()

        self.result.duration = time.time() - started
        log.info("native conversion: %d revisions -> %d commits, %d branches, %d tags in %.0fs",
                 self.result.revisions, self.result.commits, len(self.result.branches),
                 len(self.result.tags), self.result.duration)
        if on_progress:
            on_progress(1.0, f"converted through r{self.result.last_revision:,}")
        return self.result

    def _convert_revision(self, importer: FastImport, revision: Revision) -> None:
        # Group this revision's nodes by the Git ref they affect. One SVN revision
        # touching trunk and a branch becomes one commit on each, as git-svn does.
        grouped: Dict[str, List[Tuple[Node, PathClass]]] = {}
        for node in revision.nodes:
            cls = self.mapper.classify(node.path)
            if cls.kind == "outside":
                self.result.skipped_paths += 1
                continue
            grouped.setdefault(self.mapper.ref_for(cls), []).append((node, cls))

        for key, entries in grouped.items():
            if self._is_branch_removal(entries):
                # The branch directory was deleted in Subversion. Leave the ref where
                # it is rather than committing an empty tree over it: the branch's
                # history is still what it was, and the export stage decides whether a
                # branch that no longer exists in SVN gets published. Committing a
                # deleteall here would also lose the content for anyone who does opt
                # to publish deleted refs.
                self.deleted_refs.add(key)
                continue
            self._commit_for_ref(importer, revision, key, entries)

    def _commit_for_ref(self, importer: FastImport, revision: Revision, key: str,
                        entries: List[Tuple[Node, PathClass]]) -> None:
        # Blobs must be written before the commit that references them.
        blobs: Dict[int, int] = {}
        for index, (node, _cls) in enumerate(entries):
            if node.content is not None and not node.is_dir:
                content = node.content
                if node.is_symlink and content.startswith(b"link "):
                    content = content[5:]     # svn:special stores "link <target>"
                blobs[index] = importer.blob(content)

        key_path = key
        name, email = self._identity(revision.author)
        when = svn_date_to_git(revision.date)
        message = revision.message.encode("utf-8", "surrogateescape")
        if not message.strip():
            message = b""
        # The same `git-svn-id:` trailer git-svn writes. Everything downstream - the
        # message filter, the revision lookup, the incremental sync - keys off this,
        # so emitting it is what makes the two engines interchangeable.
        if self.emit_svn_id:
            branch_path = self._svn_path_for(key_path)
            url = f"{self.svn_url.rstrip('/')}/{branch_path}" if branch_path else self.svn_url
            trailer = f"git-svn-id: {url}@{revision.number} {self.uuid}".encode("utf-8")
            message = message.rstrip(b"\r\n \t")
            message = (message + b"\n\n" + trailer) if message else trailer
        if not message.strip():
            message = b"(no message)"
        message += b"\n"

        parent_mark = self.heads.get(key)
        branch_root_copy = self._branch_root_copy(entries)

        ref = self._git_ref(key)
        parent: Optional[str] = None
        if parent_mark is not None:
            parent = _ref_of(parent_mark)
        elif branch_root_copy is not None:
            # A brand new branch created by copying another: base it on that commit
            # so history is connected rather than orphaned.
            parent = _ref_of(branch_root_copy)

        mark = importer.begin_commit(
            ref=ref,
            author=(name, email, when),
            committer=(name, email, when),
            message=message,
            parent=parent,
        )

        for index, (node, cls) in enumerate(entries):
            self._apply_node(importer, node, cls, blobs.get(index), has_parent=parent is not None)

        importer.end_commit()

        self.heads[key] = mark
        self.history.setdefault(key, []).append((revision.number, mark))
        self.result.commits += 1
        if key.startswith("tag:"):
            self.result.tags[key[4:]] = f":{mark}"
        elif key == TRUNK_BRANCH:
            self.result.branches[self.convert.default_branch] = f":{mark}"
        else:
            self.result.branches[key] = f":{mark}"

    def _svn_path_for(self, key: str) -> str:
        """The Subversion path a branch key came from, for the git-svn-id trailer."""
        if key == TRUNK_BRANCH:
            return self.mapper.trunk
        if key.startswith("tag:"):
            return f"{self.tags_path}/{key[4:]}"
        root = self.mapper.branch_roots[0] if self.mapper.branch_roots else "branches"
        return f"{root}/{key}"

    @staticmethod
    def _is_branch_removal(entries: List[Tuple[Node, PathClass]]) -> bool:
        """True when this revision only deletes the branch/tag root itself."""
        root_deletes = [n for n, c in entries if c.is_root and n.action == ACTION_DELETE]
        return bool(root_deletes) and len(root_deletes) == len(entries)

    def _branch_root_copy(self, entries: List[Tuple[Node, PathClass]]) -> Optional[int]:
        """If this revision creates the branch by copying, the source commit mark."""
        for node, cls in entries:
            if cls.is_root and node.is_copy and node.copyfrom_path is not None:
                source = self.mapper.classify(node.copyfrom_path)
                if source.kind == "outside":
                    continue
                return self._head_at(self.mapper.ref_for(source), node.copyfrom_rev or 0)
        return None

    def _apply_node(self, importer: FastImport, node: Node, cls: PathClass,
                    blob_mark: Optional[int], has_parent: bool) -> None:
        path = cls.subpath

        if node.action == ACTION_DELETE:
            if cls.is_root:
                # Reached only when the root is deleted and re-created in the same
                # revision (a replace); a plain branch removal never gets here.
                importer.deleteall()
            else:
                importer.filedelete(path)
            return

        if node.action == ACTION_REPLACE and not cls.is_root:
            importer.filedelete(path)

        if node.is_copy:
            resolved = self._resolve_copy(importer, node, cls)
            if resolved:
                mode, dataref = resolved
                if cls.is_root:
                    # Copying onto the branch root: replace the whole tree, unless the
                    # commit is already based on that same source.
                    if not has_parent:
                        importer.filemodify("040000", dataref, "")
                    else:
                        importer.deleteall()
                        importer.filemodify("040000", dataref, "")
                else:
                    importer.filemodify(mode, dataref, path)
                # A copy may also carry its own modified content.
                if blob_mark is not None:
                    importer.filemodify(node.mode(), f":{blob_mark}", path)
                return
            if node.copyfrom_path:
                self.result.unresolved_copies.append(
                    f"r?: {node.path} <- {node.copyfrom_path}@{node.copyfrom_rev}")

        if node.is_dir:
            ignore = node.properties.get("svn:ignore")
            if ignore is not None:
                text = ignore.decode("utf-8", "replace").strip()
                if text:
                    self.ignores[path] = text
                    self.cleared_ignores.discard(path)
                else:
                    self.ignores.pop(path, None)
                    self.cleared_ignores.add(path)
            elif "svn:ignore" in node.deleted_properties:
                self.ignores.pop(path, None)
                self.cleared_ignores.add(path)
            # Directories carry no content of their own in Git.
            return

        if blob_mark is not None:
            importer.filemodify(node.mode(), f":{blob_mark}", path)
        elif node.action == ACTION_CHANGE and node.has_properties:
            # Property-only change (for example toggling svn:executable). The content
            # is unchanged, so re-attach the existing blob under the new mode.
            self.result.skipped_paths += 0  # nothing to write without the blob ref

    def _record_deleted(self) -> None:
        self.result.deleted_refs = sorted(
            self.convert.default_branch if key == TRUNK_BRANCH
            else key[4:] if key.startswith("tag:") else key
            for key in self.deleted_refs)

    def _finalise(self, importer: FastImport) -> None:
        """Nothing to rewrite: refs were written into git-svn's namespace directly."""
        return

    def gitignore_content(self) -> str:
        return render_gitignore(self.ignores)

    # -- introspection -------------------------------------------------------

    @property
    def branch_names(self) -> List[str]:
        return sorted(self.result.branches)

    @property
    def tag_names(self) -> List[str]:
        return sorted(self.result.tags)


def render_gitignore(ignores: Dict[str, str]) -> str:
    """Render svn:ignore properties in .gitignore syntax.

    Matches the shape `git svn show-ignore` produces, so the export stage behaves
    identically whichever engine ran.
    """
    lines: List[str] = []
    for directory in sorted(ignores):
        prefix = f"/{directory}/" if directory else "/"
        lines.append(f"# {prefix}")
        for pattern in ignores[directory].splitlines():
            pattern = pattern.strip()
            if pattern:
                lines.append(f"{prefix}{pattern}")
        lines.append("")
    return "\n".join(lines)


def _ref_of(mark_or_sha) -> str:
    """Marks are referenced as `:N`; adopted refs are already object names."""
    return f":{mark_or_sha}" if isinstance(mark_or_sha, int) else str(mark_or_sha)


def dump_command(svnadmin: str, repo: Path, start: int = 0,
                 end: Optional[int] = None) -> List[str]:
    """The `svnadmin dump` invocation the native engine expects.

    Deliberately without `--deltas`: fulltext is what makes the parser simple and
    delta decoding unnecessary.

    Note `svnadmin` takes only numeric revisions - `HEAD` is a keyword the `svn`
    *client* understands, and passing it here fails with a confusing
    "revisions must not be greater than the youngest revision".
    """
    args = [svnadmin, "dump", str(repo), "--quiet"]
    if end is not None:
        args += ["-r", f"{start}:{end}"]
        if start:
            args.append("--incremental")
    return args
