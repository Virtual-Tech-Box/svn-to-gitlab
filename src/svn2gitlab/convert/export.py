"""Deriving the publishable Git repository from the mirror.

Why two repositories?

The mirror needs its `git-svn-id` trailers and `refs/remotes/svn/*` layout to stay
resumable and incremental - the trailer is how we know which Subversion revision it
already holds. Everything we want to publish - real branches, real tags, clean commit
messages, LFS pointers - requires rewriting exactly those things. Doing both in one
repository means every rewrite breaks the next sync.

So the mirror stays pristine and the export is re-derived from it on demand. Because
every step here is deterministic (fixed tagger dates, fixed synthetic-commit
identities, a byte-stable message filter), re-deriving after a sync produces the same
commit SHAs and the push stays a fast-forward.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..config import ConvertConfig, TagStyle
from ..errors import ConversionError
from ..logging_setup import get_logger
from ..svn.analyze import sanitize_ref_name
from ..tools import ToolSet
from . import fastfilter, lfs as lfs_mod
from .git import Git
from .mirrorfmt import SVN_FORMAT, SVN_PREFIX

log = get_logger("export")

SRC_NAMESPACE = "refs/svnsrc"
GITIGNORE_IDENTITY = "svn2gitlab <svn2gitlab@localhost>"
# Fixed epoch for synthetic commits so re-derivation is byte-identical.
SYNTHETIC_DATE_FALLBACK = "2000-01-01T00:00:00+00:00"


@dataclass
class RefMapping:
    source_ref: str
    name: str
    sha: str
    kind: str            # branch | tag
    retargeted: bool = False   # tag moved to the copy's parent commit
    renamed_from: str = ""
    note: str = ""


@dataclass
class ExportResult:
    branches: List[RefMapping] = field(default_factory=list)
    tags: List[RefMapping] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    default_branch: str = ""
    head_sha: str = ""
    commit_count: int = 0
    filter_stats: Optional[dict] = None
    lfs: Optional[dict] = None
    gitignore_committed: bool = False
    empty_dirs_preserved: int = 0
    history_rewritten: bool = False

    def to_dict(self) -> dict:
        return {
            "default_branch": self.default_branch,
            "head_sha": self.head_sha,
            "commit_count": self.commit_count,
            "branch_count": len(self.branches),
            "tag_count": len(self.tags),
            "branches": [m.name for m in self.branches],
            "tags": [m.name for m in self.tags],
            "retargeted_tags": [m.name for m in self.tags if m.retargeted],
            "renamed_refs": [{"from": m.renamed_from, "to": m.name}
                             for m in self.branches + self.tags if m.renamed_from],
            "skipped": [{"ref": r, "reason": why} for r, why in self.skipped],
            "filter_stats": self.filter_stats,
            "lfs": self.lfs,
            "gitignore_committed": self.gitignore_committed,
            "empty_dirs_preserved": self.empty_dirs_preserved,
            "history_rewritten": self.history_rewritten,
        }


class Exporter:
    def __init__(
        self,
        tools: ToolSet,
        mirror,
        export_dir: Path,
        convert: ConvertConfig,
        cancel: Optional[Event] = None,
        live_branches: Optional[Sequence[str]] = None,
        live_tags: Optional[Sequence[str]] = None,
    ) -> None:
        self.tools = tools
        self.mirror = mirror
        self.convert = convert
        self.cancel = cancel
        self.git = Git(tools, export_dir, cancel=cancel)
        self.export_dir = Path(export_dir)
        # Branch and tag paths that still exist in Subversion at HEAD. git-svn keeps a
        # ref for every path it ever saw, including ones deleted years ago, and those
        # refs look perfectly healthy from inside Git - only Subversion knows they are
        # gone. `None` means "we were not told", and nothing is filtered.
        self.live_branches = set(live_branches) if live_branches is not None else None
        self.live_tags = set(live_tags) if live_tags is not None else None
        # Which source produced the mirror. Both use the same ref/trailer shape, so
        # everything below is written against the format rather than against
        # Subversion specifically.
        self.fmt = getattr(mirror, "fmt", SVN_FORMAT)

    @property
    def rewrites_history(self) -> bool:
        """True when the export cannot be a plain copy of the mirror's commits."""
        return bool(self.convert.strip_svn_metadata or self.convert.lfs.enabled)

    # -- main entry point ----------------------------------------------------

    def build(self, on_progress: Optional[Callable[[float, str], None]] = None) -> ExportResult:
        result = ExportResult(default_branch=self.convert.default_branch)

        def step(fraction: float, message: str) -> None:
            if on_progress:
                on_progress(fraction, message)

        step(0.02, "preparing export repository")
        self._init_export()

        step(0.08, "importing refs from the mirror")
        self._fetch_from_mirror()

        step(0.18, "mapping trunk and branches")
        self._map_branches(result)

        step(0.30, "creating tags")
        self._map_tags(result)

        self._cleanup_source_refs()
        self._set_head(result)

        if self.convert.strip_svn_metadata:
            step(0.40, "rewriting commit messages")
            stats = fastfilter.strip_svn_metadata(
                self.git,
                strip_svn_id=True,
                revision_trailer=self.convert.keep_revision_trailer,
                cancel=self.cancel,
                on_progress=lambda f, m: step(0.40 + 0.20 * f, m),
            )
            result.filter_stats = stats.to_dict()
            result.history_rewritten = True
            self._refresh_ref_shas(result)

        if self.convert.lfs.enabled:
            step(0.62, "planning Git LFS migration")
            plan = lfs_mod.plan(self.git, self.convert.lfs)
            lfs_result = lfs_mod.migrate(
                self.git, self.tools, plan, self.convert.lfs, cancel=self.cancel,
                on_progress=lambda f, m: step(0.62 + 0.20 * f, m),
            )
            result.lfs = {**plan.to_dict(), **lfs_result.to_dict()}
            if lfs_result.migrated:
                result.history_rewritten = True
                self._refresh_ref_shas(result)

        if self.convert.generate_gitignore:
            step(0.85, "generating .gitignore from svn:ignore")
            result.gitignore_committed = self._commit_gitignore()
            if result.gitignore_committed:
                # A synthetic commit sits at the tip, so the next re-derivation (which
                # appends new SVN revisions *below* a freshly made tip commit) orphans
                # this one. That is a non-fast-forward, and the push must know.
                result.history_rewritten = True

        step(0.92, "packing objects")
        self.git.repack()

        result.commit_count = self.git.commit_count("--all")
        result.head_sha = self.git.rev_parse(self.convert.default_branch) or ""
        self._refresh_ref_shas(result)
        step(1.0, f"{result.commit_count:,} commits, {len(result.branches)} branches, "
                  f"{len(result.tags)} tags")
        log.info("export ready: %s", result.to_dict())
        return result

    # -- steps ---------------------------------------------------------------

    def _init_export(self) -> None:
        fresh = not self.git.exists
        self.git.init(bare=False, initial_branch=self.convert.default_branch)
        self.git.apply_base_config()
        if not fresh:
            # A previous derivation may have left rewritten refs behind; start clean so
            # re-derivation is reproducible rather than incremental.
            for ref in self.git.list_refs("refs/heads/"):
                self.git.delete_ref(ref.name)
            for ref in self.git.list_refs("refs/tags/"):
                self.git.delete_ref(ref.name)
            for ref in self.git.list_refs(SRC_NAMESPACE):
                self.git.delete_ref(ref.name)
            self.git.run(["reflog", "expire", "--expire=now", "--all"], check=False)

    def _fetch_from_mirror(self) -> None:
        mirror_path = str(self.mirror.repo_dir.resolve())
        self.git.set_remote("mirror", mirror_path)
        self.git.run(
            ["fetch", "--no-tags", "--prune", "--force", "mirror",
             f"{self.fmt.ref_root}*:{SRC_NAMESPACE}/*"],
            timeout=None,
            remedy="Could not read refs from the git-svn mirror. Re-run with "
                   "`--restart-from convert` to rebuild it.",
        )

    def _src_refs(self) -> Dict[str, str]:
        prefix = f"{SRC_NAMESPACE}/"
        return {ref.name[len(prefix):]: ref.sha
                for ref in self.git.list_refs(SRC_NAMESPACE)
                if ref.name.startswith(prefix) and not ref.name.endswith("@")}

    def _tag_source_names(self) -> Dict[str, str]:
        """Short tag name -> source ref name, for every configured tags path."""
        out: Dict[str, str] = {}
        tags_paths = self.mirror.layout.tags or ["tags"]
        for path in tags_paths:
            prefix = path.rstrip("/*") + "/"
            for short, _sha in self._src_refs().items():
                if short.startswith(prefix):
                    out[short[len(prefix):]] = short
        return out

    def _map_branches(self, result: ExportResult) -> None:
        src = self._src_refs()
        tag_sources = set(self._tag_source_names().values())
        trunk_short = self.mirror.trunk_ref[len(self.fmt.ref_root):]

        trunk_sha = src.get(trunk_short)
        if not trunk_sha:
            # Some layouts land trunk under `git-svn` even when a trunk path was given.
            trunk_sha = src.get("trunk") or src.get("git-svn")
        if not trunk_sha:
            raise ConversionError(
                "the mirror contains no trunk ref to publish",
                "The fetch produced no commits on the main line of development. Check the "
                "detected layout with `svn2gitlab analyze` and set `source.layout` explicitly "
                "if trunk lives somewhere unusual.",
            )

        default = self.convert.default_branch
        self.git.update_ref(f"refs/heads/{default}", trunk_sha)
        result.branches.append(RefMapping(
            source_ref=f"{SRC_NAMESPACE}/{trunk_short}", name=default, sha=trunk_sha,
            kind="branch", note="trunk"))

        taken = {default.lower()}
        for short, sha in sorted(src.items()):
            if short in (trunk_short, "trunk", "git-svn") or short in tag_sources:
                continue
            if self._is_under_tags(short):
                continue
            if not self.convert.include_deleted_refs and self._is_deleted(short, sha, "branch"):
                result.skipped.append((short, "path no longer exists in Subversion at HEAD"))
                continue
            name, renamed = self._safe_ref_name(short, self.convert.branch_prefix, taken)
            if not name:
                result.skipped.append((short, "name could not be sanitised into a valid Git ref"))
                continue
            self.git.update_ref(f"refs/heads/{name}", sha)
            taken.add(name.lower())
            result.branches.append(RefMapping(
                source_ref=f"{SRC_NAMESPACE}/{short}", name=name, sha=sha, kind="branch",
                renamed_from=renamed))
        log.info("mapped %d branch(es)", len(result.branches))

    def _map_tags(self, result: ExportResult) -> None:
        if self.convert.tag_style == TagStyle.BRANCHES:
            self._map_tags_as_branches(result)
            return

        src = self._src_refs()
        taken: set = set()
        for short_name, source_short in sorted(self._tag_source_names().items()):
            sha = src.get(source_short)
            if not sha:
                continue
            if not self.convert.include_deleted_refs and \
                    self._is_deleted(short_name, sha, "tag"):
                result.skipped.append((source_short,
                                       "tag no longer exists in Subversion at HEAD"))
                continue
            name, renamed = self._safe_ref_name(short_name, self.convert.tag_prefix, taken)
            if not name:
                result.skipped.append((source_short, "tag name could not be sanitised"))
                continue

            target = sha
            retargeted = False
            note = ""
            if self.convert.tag_from_parent and self.git.is_empty_commit(sha):
                parent = self.git.first_parent(sha)
                if parent:
                    target = parent
                    retargeted = True
            elif self.convert.tag_from_parent:
                note = "tag path was modified after it was created; tagging the modified state"

            meta = self.git.commit_meta(sha)
            if self.convert.tag_style == TagStyle.ANNOTATED:
                message = meta.get("message") or f"Imported from SVN tag {short_name}"
                self.git.create_annotated_tag(
                    name=name, target=target, message=message,
                    tagger_name=meta.get("author_name") or "svn2gitlab",
                    tagger_email=meta.get("author_email") or "svn2gitlab@localhost",
                    tagger_date=meta.get("author_date") or SYNTHETIC_DATE_FALLBACK,
                )
            else:
                self.git.create_lightweight_tag(name, target)

            taken.add(name.lower())
            result.tags.append(RefMapping(
                source_ref=f"{SRC_NAMESPACE}/{source_short}", name=name, sha=target,
                kind="tag", retargeted=retargeted, renamed_from=renamed, note=note))
        log.info("created %d tag(s) (%d retargeted to the copy parent)",
                 len(result.tags), sum(1 for t in result.tags if t.retargeted))

    def _map_tags_as_branches(self, result: ExportResult) -> None:
        src = self._src_refs()
        taken = {m.name.lower() for m in result.branches}
        for short_name, source_short in sorted(self._tag_source_names().items()):
            sha = src.get(source_short)
            if not sha:
                continue
            name, renamed = self._safe_ref_name(short_name, self.convert.tag_prefix or "tags/", taken)
            if not name:
                continue
            self.git.update_ref(f"refs/heads/{name}", sha)
            taken.add(name.lower())
            result.branches.append(RefMapping(
                source_ref=f"{SRC_NAMESPACE}/{source_short}", name=name, sha=sha,
                kind="branch", renamed_from=renamed, note="SVN tag kept as a branch"))

    def _is_under_tags(self, short: str) -> bool:
        for path in (self.mirror.layout.tags or []):
            if short.startswith(path.rstrip("/*") + "/"):
                return True
        return False

    def _is_deleted(self, short_name: str, sha: str, kind: str) -> bool:
        """Whether this ref corresponds to a Subversion path that no longer exists.

        Two independent signals, because neither is sufficient alone:

        * the live-ref list taken from Subversion at HEAD - authoritative, but only
          available when the analysis stage ran;
        * an empty tree at the ref tip, which is what a branch looks like when the
          deletion itself was the last thing git-svn recorded on it.
        """
        live = self.live_branches if kind == "branch" else self.live_tags
        if live is not None:
            # git-svn refs are named by their path under branches/ or tags/, which is
            # exactly how the Subversion listing names them.
            if short_name in live:
                return False
            if short_name.rsplit("/", 1)[-1] in live:
                return False
            return True
        result = self.git.run(["ls-tree", "-r", "--name-only", sha], check=False, timeout=300)
        return result.ok and not result.stdout.strip()

    def _safe_ref_name(self, raw: str, prefix: str, taken: set) -> Tuple[str, str]:
        name = f"{prefix}{raw}" if prefix else raw
        cleaned = sanitize_ref_name(name) if self.convert.sanitize_ref_names else name
        if not cleaned:
            return "", ""
        if not self.convert.sanitize_ref_names and cleaned != name:
            return "", ""
        renamed = raw if cleaned != name else ""
        candidate = cleaned
        suffix = 2
        while candidate.lower() in taken:
            candidate = f"{cleaned}-{suffix}"
            suffix += 1
            renamed = renamed or raw
        return candidate, renamed

    def _cleanup_source_refs(self) -> None:
        for ref in self.git.list_refs(SRC_NAMESPACE):
            self.git.delete_ref(ref.name)
        self.git.run(["remote", "remove", "mirror"], check=False)

    def _set_head(self, result: ExportResult) -> None:
        default = self.convert.default_branch
        if not self.git.branch_exists(default):
            raise ConversionError(f"default branch {default!r} was not created")
        self.git.symbolic_head(default)
        self.git.run(["reset", "--hard", default], check=False, timeout=None)

    def _refresh_ref_shas(self, result: ExportResult) -> None:
        """Re-read SHAs after a history rewrite so reports quote the published values."""
        for mapping in result.branches:
            sha = self.git.rev_parse(f"refs/heads/{mapping.name}")
            if sha:
                mapping.sha = sha
        for mapping in result.tags:
            sha = self.git.rev_parse(f"refs/tags/{mapping.name}")
            if sha:
                mapping.sha = sha
        result.head_sha = self.git.rev_parse(self.convert.default_branch) or result.head_sha

    def _commit_gitignore(self) -> bool:
        """Translate svn:ignore properties into a committed .gitignore on the default branch."""
        content = self.mirror.svn_ignore_content() \
            if hasattr(self.mirror, "svn_ignore_content") else ""
        if not content.strip():
            log.info("no svn:ignore properties found; skipping .gitignore")
            return False

        target = self.export_dir / ".gitignore"
        existing = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        body = _format_gitignore(content)
        if existing.strip() == body.strip():
            return False
        merged = body if not existing.strip() else existing.rstrip() + "\n\n" + body
        target.write_text(merged, encoding="utf-8")

        head_meta = self.git.commit_meta("HEAD")
        date = head_meta.get("committer_date") or SYNTHETIC_DATE_FALLBACK
        sha = self.git.add_all_and_commit(
            message="Add .gitignore generated from svn:ignore properties\n\n"
                    "Created automatically during the SVN to GitLab migration.",
            author=GITIGNORE_IDENTITY,
            date=date,
            paths=[".gitignore"],
        )
        if sha:
            log.info("committed generated .gitignore as %s", sha[:12])
            return True
        return False


def _format_gitignore(show_ignore_output: str) -> str:
    """`git svn show-ignore` already emits .gitignore syntax; tidy and label it."""
    lines = [line.rstrip() for line in show_ignore_output.splitlines()]
    # Drop leading blank lines and collapse runs of blanks.
    cleaned: List[str] = []
    for line in lines:
        if not line and (not cleaned or not cleaned[-1]):
            continue
        cleaned.append(line)
    header = [
        "# Generated by svn2gitlab from svn:ignore properties.",
        "# Review these rules: SVN ignores are per-directory, Git's are hierarchical,",
        "# so a rule that was scoped to one folder in SVN may now apply more widely.",
        "",
    ]
    return "\n".join(header + cleaned).rstrip() + "\n"


def preserve_empty_directories(
    git: Git,
    svn_dirs: Sequence[str],
    identity: str = GITIGNORE_IDENTITY,
) -> int:
    """Materialise `.gitkeep` for directories that exist in SVN but vanished in Git.

    Git cannot record an empty directory. Projects that rely on one existing (log
    output folders, plugin drop-points) break silently after migration unless we
    place a marker file.
    """
    tracked = set()
    listing = git.run(["ls-tree", "-r", "--name-only", "HEAD"], check=False, timeout=None)
    for path in listing.stdout.splitlines():
        parts = path.split("/")
        for depth in range(1, len(parts)):
            tracked.add("/".join(parts[:depth]))

    created = 0
    for directory in svn_dirs:
        directory = directory.strip("/")
        if not directory or directory in tracked:
            continue
        keep = git.repo / directory / ".gitkeep"
        keep.parent.mkdir(parents=True, exist_ok=True)
        if not keep.exists():
            keep.write_text("", encoding="utf-8")
            created += 1

    if created:
        head_meta = git.commit_meta("HEAD")
        git.add_all_and_commit(
            message=f"Add .gitkeep markers for {created} empty director"
                    f"{'y' if created == 1 else 'ies'}\n\n"
                    "Git cannot track empty directories; these markers preserve the layout "
                    "that existed in Subversion.",
            author=identity,
            date=head_meta.get("committer_date") or SYNTHETIC_DATE_FALLBACK,
        )
        log.info("preserved %d empty director%s", created, "y" if created == 1 else "ies")
    return created
