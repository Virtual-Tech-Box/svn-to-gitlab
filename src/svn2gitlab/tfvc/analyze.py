"""Read-only analysis of a TFVC team project.

Produces the same `RepoAnalysis` the Subversion side does, so the report, the web
dashboard and the operator's decision-making are identical whichever source they are
looking at. Nothing here writes anything.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence

from ..config import LayoutMode, SourceConfig
from ..logging_setup import get_logger
from ..svn.analyze import DetectedLayout, RepoAnalysis, _case_insensitive_collisions
from .client import TfvcClient
from .convert import detect_layout

log = get_logger("tfvc.analyze")


def analyse(
    client: TfvcClient,
    source: SourceConfig,
    lfs_enabled: bool = False,
    lfs_threshold_bytes: int = 50 * 1024 * 1024,
    known_authors: Optional[Dict[str, str]] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> RepoAnalysis:
    analysis = RepoAnalysis()
    project_root = source.tfvc_project_root()
    analysis.url = f"{client.base_url}/{client.collection}  {project_root}".strip()
    analysis.repository_root = project_root

    if on_progress:
        on_progress(0.05, "negotiating the API version")
    api_version = client.probe()
    analysis.repository_uuid = f"tfvc-api-{api_version}"

    # -- layout ---------------------------------------------------------------
    if on_progress:
        on_progress(0.15, "discovering branches")
    declared = [b.get("path") for b in client.branches() if b.get("path")]
    if source.branch_roots:
        declared = list(source.branch_roots)

    sample_paths: List[str] = []
    if not declared:
        # No branch definitions: sample the tree so path conventions can be applied.
        try:
            sample_paths = [i.get("path", "") for i in
                            client.list_items(project_root, recursive=True)][:5000]
        except Exception as exc:
            log.debug("could not sample the item tree: %s", exc)

    tfvc_layout = detect_layout(project_root, declared, sample_paths)
    analysis.layout = DetectedLayout(
        mode=LayoutMode.CUSTOM,
        trunk=tfvc_layout.main_root,
        branches=[r for r in tfvc_layout.branch_roots if r != tfvc_layout.main_root],
        tags=[],
        confidence="high" if declared else "medium",
        rationale=[f"TFVC branches from {tfvc_layout.detected_from}"],
    )
    analysis.branch_names = [r.rsplit("/", 1)[-1]
                             for r in tfvc_layout.branch_roots if r != tfvc_layout.main_root]

    # -- history --------------------------------------------------------------
    if on_progress:
        on_progress(0.3, "walking the changeset history")
    newest = client.changeset_count_hint(project_root)
    analysis.head_revision = newest

    authors: Counter = Counter()
    first_date = last_date = ""
    count = 0
    first_id = 0
    for changeset in client.iter_changesets(item_path=project_root):
        count += 1
        authors[changeset.author.key] += 1
        if not first_id:
            first_id = changeset.id
            first_date = changeset.created_date
        last_date = changeset.created_date
        if on_progress and newest and count % 200 == 0:
            on_progress(0.3 + 0.5 * min(1.0, changeset.id / newest),
                        f"changeset {changeset.id:,} of {newest:,}")

    analysis.revision_count = count
    analysis.first_revision = first_id or 1
    analysis.authors = dict(authors.most_common())
    analysis.first_commit = _iso(first_date)
    analysis.last_commit = _iso(last_date)

    # -- size -----------------------------------------------------------------
    if on_progress:
        on_progress(0.85, "measuring the current tree")
    _measure_tree(client, tfvc_layout.main_root or project_root, analysis,
                  lfs_threshold_bytes)

    _findings(analysis, tfvc_layout, known_authors, lfs_enabled, api_version)
    if on_progress:
        on_progress(1.0, f"changeset {analysis.head_revision:,}, "
                         f"{analysis.revision_count:,} changesets, "
                         f"{len(analysis.authors)} authors")
    return analysis


def _iso(value: str) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


def _measure_tree(client: TfvcClient, scope: str, analysis: RepoAnalysis,
                  lfs_threshold_bytes: int) -> None:
    from ..svn.analyze import LargeFile

    try:
        items = client.list_items(scope, recursive=True)
    except Exception as exc:
        log.warning("could not list the tree under %s: %s", scope, exc)
        return

    total = 0
    large: List[LargeFile] = []
    extensions: Dict[str, int] = {}
    lfs_count = lfs_bytes = 0
    files = 0

    for item in items:
        if item.get("isFolder"):
            continue
        files += 1
        size = int(item.get("size") or 0)
        total += size
        path = item.get("path", "")
        name = path.rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        extensions[ext] = extensions.get(ext, 0) + size
        if size >= lfs_threshold_bytes:
            lfs_count += 1
            lfs_bytes += size
        if size >= max(lfs_threshold_bytes // 4, 1024 * 1024):
            large.append(LargeFile(path, size, ext))

    analysis.head_file_count = files
    analysis.head_size_bytes = total
    analysis.largest_files = sorted(large, key=lambda f: f.size, reverse=True)[:40]
    analysis.extension_sizes = dict(sorted(extensions.items(), key=lambda kv: kv[1],
                                           reverse=True)[:40])
    analysis.lfs_candidate_count = lfs_count
    analysis.lfs_candidate_bytes = lfs_bytes


def _findings(analysis: RepoAnalysis, layout, known_authors, lfs_enabled: bool,
              api_version: str) -> None:
    known = {k.lower() for k in (known_authors or {})}
    analysis.unmapped_authors = sorted(
        [a for a in analysis.authors if (a or "").lower() not in known],
        key=lambda a: -analysis.authors.get(a, 0))

    if analysis.revision_count == 0:
        analysis.add("blocker", "empty-project",
                     f"No changesets were found under {analysis.repository_root}.",
                     "Check source.project and that the account can read the team "
                     "project. A partially readable project cannot be migrated "
                     "faithfully.")

    if not layout.branch_roots:
        analysis.add("blocker", "no-branches",
                     "No branch roots could be determined for this team project.",
                     "Set `source.branch_roots` explicitly, for example "
                     "[\"$/MyProject/Main\"].")
    elif "conventions" in layout.detected_from:
        analysis.add("warning", "branches-inferred",
                     "This project has no TFVC branch definitions, so branch roots "
                     f"were inferred from path conventions: "
                     f"{', '.join(layout.branch_roots[:6])}.",
                     "Confirm these are branches rather than ordinary folders, and "
                     "set `source.branch_roots` if any are wrong. Getting this wrong "
                     "silently merges separate lines of development.")

    analysis.add("info", "api-version",
                 f"The server negotiated REST api-version {api_version}.",
                 "TFS 2015 speaks 2.0, 2017 speaks 3.x, 2018 speaks 4.x.")

    analysis.add("warning", "no-tags",
                 "TFVC has no tag concept, so no Git tags are created. Labels are "
                 "the nearest equivalent and are not migrated.",
                 "If releases are marked with TFVC labels, record them separately "
                 "and recreate the important ones as Git tags after the migration.")

    analysis.add("info", "merges-not-reconstructed",
                 "TFVC merge relationships are recorded but are not reconstructed as "
                 "Git merge commits; a merge changeset becomes an ordinary commit "
                 "containing its merged content.",
                 "The file content is correct; only the branch topology is "
                 "simplified. This matches how the Subversion side treats "
                 "svn:mergeinfo.")

    if analysis.unmapped_authors:
        shown = ", ".join(analysis.unmapped_authors[:10])
        analysis.add("warning", "unmapped-authors",
                     f"{len(analysis.unmapped_authors)} TFVC user(s) have no Git "
                     f"identity yet: {shown}"
                     f"{', ...' if len(analysis.unmapped_authors) > 10 else ''}",
                     "Run `svn2gitlab authors --write` and map each account to the "
                     "person's GitLab email so commits are attributed correctly.")

    if analysis.lfs_candidate_count and not lfs_enabled:
        analysis.add("warning", "large-files",
                     f"{analysis.lfs_candidate_count} file(s) at the current tree "
                     f"exceed the LFS threshold "
                     f"({analysis.lfs_candidate_bytes / 1048576:.0f} MiB total).",
                     "Enable `convert.lfs.enabled: true` so these are rewritten into "
                     "Git LFS before the push.")

    if analysis.revision_count > 20000:
        analysis.add("info", "large-history",
                     f"{analysis.revision_count:,} changesets. Conversion fetches item "
                     "content over HTTP, so this will take a while.",
                     "Run it on a machine with fast network access to the TFS "
                     "application tier. The conversion is resumable.")

    dupes = _case_insensitive_collisions(analysis.branch_names)
    if dupes:
        analysis.add("warning", "ref-case-collision",
                     f"Branch names differ only by case: {', '.join(dupes[:6])}.",
                     "Git on Windows and macOS cannot hold both.")
