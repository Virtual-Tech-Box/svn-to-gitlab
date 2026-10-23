"""Repository analysis: layout detection, history statistics, migration risk report.

Nothing here mutates anything. The point is to produce, in minutes, the report you
would otherwise write by hand over a week of discovery calls: what layout this
repository actually uses (as opposed to what the client believes it uses), who
committed to it, how big it is, and which specific things will bite during the
conversion.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..config import LayoutMode, SourceConfig
from ..logging_setup import get_logger
from .client import SvnClient, SvnEntry, url_join

log = get_logger("analyze")

# Bucket label for files with no extension. Deliberately not "": the manifest is
# JSON, and Windows PowerShell's ConvertFrom-Json refuses to build an object with an
# empty property name — which made the report unreadable on the very platform the
# tool is designed to run on.
NO_EXTENSION = "(no extension)"

# Directory names that in practice mean trunk / branches / tags.
TRUNK_NAMES = ("trunk", "TRUNK", "Trunk", "main", "master", "head", "MAIN")
BRANCH_NAMES = ("branches", "BRANCHES", "Branches", "branch", "brances", "dev", "development")
TAG_NAMES = ("tags", "TAGS", "Tags", "tag", "releases", "release", "Releases")

# Extensions that are essentially always better off in Git LFS.
BINARY_EXTENSIONS = {
    "zip", "7z", "rar", "gz", "tgz", "bz2", "xz", "jar", "war", "ear", "nupkg",
    "exe", "dll", "so", "dylib", "lib", "obj", "pdb", "msi", "cab", "bin",
    "iso", "vmdk", "vhd", "vhdx",
    "pdf", "psd", "ai", "eps", "indd", "sketch", "fig",
    "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "ico", "svgz", "webp",
    "mp3", "wav", "flac", "aac", "ogg", "mp4", "avi", "mov", "wmv", "mkv", "flv",
    "mdb", "accdb", "bak", "dmp", "sqlite", "db", "mdf", "ldf",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "vsd", "vsdx",
    "dwg", "dxf", "step", "stp", "igs", "iges", "stl", "3ds", "max", "blend",
    "unity3d", "asset", "fbx", "pak", "pkg", "apk", "ipa", "aab",
}

# Ref name characters git will not accept.
_INVALID_REF_CHARS = re.compile(r"[\x00-\x20~^:?*\[\\\x7f]")


@dataclass
class DetectedLayout:
    mode: LayoutMode = LayoutMode.FLAT
    trunk: Optional[str] = None
    branches: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    projects: List[str] = field(default_factory=list)
    nested_branches: bool = False
    confidence: str = "low"
    rationale: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d["mode"] = self.mode.value
        return d


@dataclass
class LargeFile:
    path: str
    size: int
    extension: str


@dataclass
class Finding:
    """Something the operator needs to know before starting."""

    level: str          # info | warning | blocker
    code: str
    message: str
    remedy: str = ""

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass
class RepoAnalysis:
    url: str = ""
    repository_root: str = ""
    repository_uuid: str = ""
    head_revision: int = 0
    first_revision: int = 1
    revision_count: int = 0
    authors: Dict[str, int] = field(default_factory=dict)
    unmapped_authors: List[str] = field(default_factory=list)
    first_commit: Optional[str] = None
    last_commit: Optional[str] = None
    layout: DetectedLayout = field(default_factory=DetectedLayout)
    branch_names: List[str] = field(default_factory=list)
    tag_names: List[str] = field(default_factory=list)
    head_file_count: int = 0
    head_size_bytes: int = 0
    largest_files: List[LargeFile] = field(default_factory=list)
    extension_sizes: Dict[str, int] = field(default_factory=dict)
    lfs_candidate_count: int = 0
    lfs_candidate_bytes: int = 0
    externals: Dict[str, str] = field(default_factory=dict)
    findings: List[Finding] = field(default_factory=list)
    analysed_at: str = ""

    def add(self, level: str, code: str, message: str, remedy: str = "") -> None:
        self.findings.append(Finding(level, code, message, remedy))

    @property
    def blockers(self) -> List[Finding]:
        return [f for f in self.findings if f.level == "blocker"]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    def to_dict(self) -> Dict[str, object]:
        return {
            "url": self.url,
            "repository_root": self.repository_root,
            "repository_uuid": self.repository_uuid,
            "head_revision": self.head_revision,
            "first_revision": self.first_revision,
            "revision_count": self.revision_count,
            "authors": self.authors,
            "unmapped_authors": self.unmapped_authors,
            "first_commit": self.first_commit,
            "last_commit": self.last_commit,
            "layout": self.layout.to_dict(),
            "branch_names": self.branch_names,
            "tag_names": self.tag_names,
            "head_file_count": self.head_file_count,
            "head_size_bytes": self.head_size_bytes,
            "largest_files": [asdict(f) for f in self.largest_files],
            "extension_sizes": self.extension_sizes,
            "lfs_candidate_count": self.lfs_candidate_count,
            "lfs_candidate_bytes": self.lfs_candidate_bytes,
            "externals": self.externals,
            "findings": [f.to_dict() for f in self.findings],
            "analysed_at": self.analysed_at,
        }


# --------------------------------------------------------------------------- #
# Layout detection
# --------------------------------------------------------------------------- #

def _match(entries: Sequence[SvnEntry], candidates: Sequence[str]) -> Optional[str]:
    by_name = {e.path.rstrip("/"): e for e in entries if e.is_dir}
    for name in candidates:
        if name in by_name:
            return name
    lowered = {name.lower(): name for name in by_name}
    for name in candidates:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def detect_layout(client: SvnClient, base_url: str,
                  projects_filter: Sequence[str] = ()) -> DetectedLayout:
    """Work out how this repository is organised by looking, not by assuming."""
    layout = DetectedLayout()
    root_entries = client.ls(base_url, check=False)
    if not root_entries:
        layout.rationale.append("repository root is empty at HEAD")
        layout.mode = LayoutMode.FLAT
        layout.trunk = "."
        layout.confidence = "low"
        return layout

    trunk = _match(root_entries, TRUNK_NAMES)
    branches = _match(root_entries, BRANCH_NAMES)
    tags = _match(root_entries, TAG_NAMES)

    if trunk:
        layout.mode = LayoutMode.STANDARD
        layout.trunk = trunk
        layout.branches = [branches] if branches else []
        layout.tags = [tags] if tags else []
        layout.confidence = "high" if (branches and tags) else "medium"
        layout.rationale.append(
            f"found '{trunk}' at the repository root"
            + (f", branches in '{branches}'" if branches else ", no branches directory")
            + (f", tags in '{tags}'" if tags else ", no tags directory")
        )
        if branches:
            layout.nested_branches = _looks_nested(client, url_join(base_url, branches))
            if layout.nested_branches:
                layout.branches = [f"{branches}/*"]
                layout.rationale.append(
                    f"'{branches}' contains directories of directories - using a two-level "
                    f"branch pattern '{branches}/*'"
                )
        return layout

    # No trunk at the root: is every top-level directory its own standard project?
    candidate_projects: List[str] = []
    dirs = [e.path.rstrip("/") for e in root_entries if e.is_dir]
    if projects_filter:
        wanted = {p.strip("/") for p in projects_filter}
        dirs = [d for d in dirs if d in wanted]
    for name in dirs[:40]:  # bounded probe; large repos have hundreds of top-level dirs
        sub = client.ls(url_join(base_url, name), check=False)
        if sub and _match(sub, TRUNK_NAMES):
            candidate_projects.append(name)

    if candidate_projects and len(candidate_projects) >= max(1, len(dirs) // 2):
        layout.mode = LayoutMode.MULTI_PROJECT
        layout.projects = candidate_projects
        layout.confidence = "high" if len(candidate_projects) == len(dirs) else "medium"
        layout.rationale.append(
            f"{len(candidate_projects)} of {len(dirs)} top-level directories contain their own "
            "trunk - this repository holds multiple projects"
        )
        return layout

    layout.mode = LayoutMode.FLAT
    layout.trunk = "."
    layout.confidence = "medium"
    layout.rationale.append(
        "no trunk/branches/tags convention found - treating the whole path as a single "
        "line of development"
    )
    return layout


def _looks_nested(client: SvnClient, branches_url: str, sample: int = 8) -> bool:
    """True when branches are grouped one level deeper (`branches/team/feature`)."""
    entries = client.ls(branches_url, check=False)
    dirs = [e for e in entries if e.is_dir][:sample]
    if len(dirs) < 2:
        return False
    nested_votes = 0
    for entry in dirs:
        children = client.ls(url_join(branches_url, entry.path.rstrip("/")), check=False)
        if not children:
            continue
        has_files = any(not c.is_dir for c in children)
        has_dirs = any(c.is_dir for c in children)
        # A real branch almost always has at least one file at its root.
        if has_dirs and not has_files:
            nested_votes += 1
    return nested_votes >= max(2, len(dirs) // 2)


def layout_for_source(client: SvnClient, source: SourceConfig, base_url: str) -> DetectedLayout:
    """Honour an explicit layout, otherwise detect one."""
    cfg = source.layout
    if cfg.mode == LayoutMode.AUTO:
        return detect_layout(client, base_url, cfg.projects)
    layout = DetectedLayout(
        mode=cfg.mode,
        trunk=cfg.trunk or ("." if cfg.mode == LayoutMode.FLAT else "trunk"),
        branches=list(cfg.branches),
        tags=list(cfg.tags),
        projects=list(cfg.projects),
        confidence="explicit",
        rationale=["layout supplied in the configuration; detection skipped"],
    )
    if cfg.mode == LayoutMode.STANDARD:
        layout.trunk = cfg.trunk or "trunk"
        layout.branches = cfg.branches or ["branches"]
        layout.tags = cfg.tags or ["tags"]
    layout.nested_branches = any("*" in b for b in layout.branches)
    return layout


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #

def collect_history_stats(
    client: SvnClient,
    base_url: str,
    analysis: RepoAnalysis,
    start: int = 1,
    end: Optional[int] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> None:
    """Walk the whole log once, cheaply (`-q`), to get authors and the revision span."""
    authors: Counter = Counter()
    first: Optional[Tuple[int, Optional[datetime]]] = None
    last: Optional[Tuple[int, Optional[datetime]]] = None
    count = 0
    head = end if end is not None else analysis.head_revision

    def progress(current: int, total: int) -> None:
        if on_progress and total:
            on_progress(min(1.0, current / total), f"scanned r{current:,} of r{total:,}")

    for entry in client.iter_log(base_url, start=start, end=head, with_paths=False,
                                 on_progress=progress):
        count += 1
        authors[entry.author or ""] += 1
        if first is None:
            first = (entry.revision, entry.date)
        last = (entry.revision, entry.date)

    analysis.revision_count = count
    analysis.authors = dict(authors.most_common())
    if first:
        analysis.first_revision = first[0]
        analysis.first_commit = first[1].isoformat() if first[1] else None
    if last:
        analysis.last_commit = last[1].isoformat() if last[1] else None


def collect_tree_stats(
    client: SvnClient,
    tree_url: str,
    analysis: RepoAnalysis,
    lfs_threshold_bytes: int,
    lfs_extensions: Sequence[str] = (),
    top_n: int = 40,
) -> None:
    """Size profile of one tree at HEAD (normally trunk).

    This is an estimate used for planning. The authoritative large-object scan runs
    against the converted Git mirror, where every historical revision of every blob
    is visible - SVN can only cheaply show us the current tip.
    """
    entries = client.ls(tree_url, recursive=True, check=False)
    files = [e for e in entries if not e.is_dir]
    ext_sizes: Dict[str, int] = defaultdict(int)
    large: List[LargeFile] = []
    lfs_ext = {e.lower() for e in lfs_extensions}
    lfs_count = 0
    lfs_bytes = 0
    total = 0

    for entry in files:
        total += entry.size
        ext = entry.path.rsplit(".", 1)[-1].lower() if "." in entry.path.rsplit("/", 1)[-1] else NO_EXTENSION
        ext_sizes[ext] += entry.size
        if entry.size >= lfs_threshold_bytes or (ext and ext in lfs_ext):
            lfs_count += 1
            lfs_bytes += entry.size
        if entry.size >= max(lfs_threshold_bytes // 4, 1024 * 1024):
            large.append(LargeFile(entry.path, entry.size, ext))

    analysis.head_file_count = len(files)
    analysis.head_size_bytes = total
    analysis.largest_files = sorted(large, key=lambda f: f.size, reverse=True)[:top_n]
    analysis.extension_sizes = dict(sorted(ext_sizes.items(), key=lambda kv: kv[1], reverse=True)[:40])
    analysis.lfs_candidate_count = lfs_count
    analysis.lfs_candidate_bytes = lfs_bytes


def collect_refs(client: SvnClient, base_url: str, layout: DetectedLayout,
                 analysis: RepoAnalysis, limit: int = 2000) -> None:
    """List branch and tag names as they exist at HEAD."""
    for spec, sink in ((layout.branches, analysis.branch_names), (layout.tags, analysis.tag_names)):
        for path in spec:
            if path.endswith("/*"):
                parent = path[:-2]
                for group in client.ls(url_join(base_url, parent), check=False):
                    if not group.is_dir:
                        continue
                    group_name = group.path.rstrip("/")
                    for child in client.ls(url_join(base_url, parent, group_name), check=False):
                        if child.is_dir and len(sink) < limit:
                            sink.append(f"{group_name}/{child.path.rstrip('/')}")
            else:
                for entry in client.ls(url_join(base_url, path), check=False):
                    if entry.is_dir and len(sink) < limit:
                        sink.append(entry.path.rstrip("/"))


def collect_externals(client: SvnClient, base_url: str, analysis: RepoAnalysis) -> None:
    """svn:externals have no Git equivalent; the operator must be told about every one."""
    try:
        props = client.propget("svn:externals", base_url, recursive=True)
    except Exception as exc:
        log.debug("svn:externals probe failed: %s", exc)
        return
    analysis.externals = {k: v.strip() for k, v in props.items() if v.strip()}


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #

def evaluate_findings(analysis: RepoAnalysis, source: SourceConfig,
                      known_authors: Optional[Dict[str, str]] = None,
                      lfs_enabled: bool = False) -> None:
    """Turn the raw numbers into things a human should act on."""
    known = {k.lower() for k in (known_authors or {})}
    unmapped = [a for a in analysis.authors if (a or "").lower() not in known]
    analysis.unmapped_authors = sorted(unmapped, key=lambda a: -analysis.authors.get(a, 0))

    if analysis.revision_count == 0:
        analysis.add("blocker", "empty-repository",
                     "The source contains no revisions in the selected range.",
                     "Check source.url, source.subpath and source.revision_start.")

    if analysis.layout.mode == LayoutMode.FLAT and analysis.layout.confidence != "explicit":
        analysis.add("warning", "flat-layout",
                     "No trunk/branches/tags structure was found; the history will convert as a "
                     "single branch and no tags will be created.",
                     "If the repository really does have branches under different names, set "
                     "`source.layout.mode: custom` with explicit trunk/branches/tags paths.")

    if analysis.layout.mode == LayoutMode.MULTI_PROJECT:
        analysis.add("info", "multi-project",
                     f"This repository holds {len(analysis.layout.projects)} projects "
                     f"({', '.join(analysis.layout.projects[:8])}"
                     f"{', ...' if len(analysis.layout.projects) > 8 else ''}).",
                     "GitLab convention is one project per repository. Generate a batch config "
                     "with `svn2gitlab init --from-analysis` to split them.")

    if analysis.externals:
        sample = list(analysis.externals.items())[:5]
        detail = "; ".join(f"{path or '/'} -> {value.splitlines()[0]}" for path, value in sample)
        analysis.add("warning", "externals",
                     f"{len(analysis.externals)} path(s) define svn:externals. Git has no direct "
                     f"equivalent and they will not be migrated. Examples: {detail}",
                     "Plan to replace them with Git submodules, a package manager dependency, or "
                     "a vendored copy after the migration.")

    if analysis.unmapped_authors:
        shown = ", ".join(analysis.unmapped_authors[:10])
        analysis.add("warning", "unmapped-authors",
                     f"{len(analysis.unmapped_authors)} SVN author(s) have no Git identity yet: "
                     f"{shown}{', ...' if len(analysis.unmapped_authors) > 10 else ''}",
                     "Run `svn2gitlab authors --write` to generate authors.txt, then map each "
                     "account to the person's GitLab email so commits are attributed correctly.")

    if "" in analysis.authors:
        analysis.add("info", "anonymous-commits",
                     f"{analysis.authors['']} revision(s) have no author recorded (typically "
                     "created by hooks or by `svnadmin load`).",
                     "These are attributed to `authors.no_author_name`.")

    threshold = 100 * 1024 * 1024
    huge = [f for f in analysis.largest_files if f.size >= threshold]
    if huge and not lfs_enabled:
        analysis.add("warning", "large-files",
                     f"{len(huge)} file(s) at HEAD exceed 100 MiB (largest: "
                     f"{huge[0].path}, {huge[0].size / 1048576:.0f} MiB). GitLab SaaS rejects "
                     "pushes containing files above its per-file limit.",
                     "Enable `convert.lfs.enabled: true` so these are rewritten into Git LFS.")
    elif analysis.lfs_candidate_count and lfs_enabled:
        analysis.add("info", "lfs-plan",
                     f"{analysis.lfs_candidate_count} file(s) at HEAD "
                     f"({analysis.lfs_candidate_bytes / 1048576:.0f} MiB) will move to Git LFS.",
                     "Confirm the GitLab namespace has enough LFS storage quota.")

    bad_refs = [n for n in analysis.branch_names + analysis.tag_names
                if _INVALID_REF_CHARS.search(n) or n.endswith(".lock") or ".." in n]
    if bad_refs:
        analysis.add("warning", "invalid-ref-names",
                     f"{len(bad_refs)} branch/tag name(s) are not valid Git refs: "
                     f"{', '.join(bad_refs[:8])}",
                     "They will be sanitised automatically (set `convert.sanitize_ref_names: false` "
                     "to fail instead).")

    if analysis.revision_count > 50000:
        analysis.add("info", "large-history",
                     f"{analysis.revision_count:,} revisions. A network conversion would take days.",
                     "Use the local fast path: run this tool on the SVN server with "
                     "`source.local_path` set, or leave `fast_path: auto` so it takes an "
                     "svnrdump copy first.")

    if source.url and not source.local_path and analysis.revision_count > 5000:
        analysis.add("info", "remote-source",
                     "Converting over the network from a remote URL is 10-50x slower than "
                     "converting from a local copy of the repository.",
                     "Set `source.local_path` when running on the SVN server itself.")

    dupes = _case_insensitive_collisions(analysis.branch_names + analysis.tag_names)
    if dupes:
        analysis.add("warning", "ref-case-collision",
                     f"Branch/tag names differ only by case: {', '.join(dupes[:6])}. "
                     "Git on Windows and macOS cannot hold both.",
                     "Rename them in SVN before migrating, or accept that one will win.")


def _case_insensitive_collisions(names: Sequence[str]) -> List[str]:
    seen: Dict[str, str] = {}
    collisions: List[str] = []
    for name in names:
        key = name.lower()
        if key in seen and seen[key] != name:
            collisions.append(f"{seen[key]} / {name}")
        else:
            seen[key] = name
    return collisions


def sanitize_ref_name(name: str) -> str:
    """Make an SVN directory name usable as a Git ref."""
    cleaned = _INVALID_REF_CHARS.sub("-", name.strip())
    cleaned = cleaned.replace("..", "-").replace("@{", "-")
    cleaned = re.sub(r"/{2,}", "/", cleaned)
    cleaned = re.sub(r"(^|/)[.]+", r"\1", cleaned)
    cleaned = cleaned.strip("/. ")
    if cleaned.endswith(".lock"):
        cleaned = cleaned[: -len(".lock")] + "-lock"
    return cleaned or "unnamed"
