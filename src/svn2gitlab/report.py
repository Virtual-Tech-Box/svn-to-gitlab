"""Migration reports.

Two audiences, two formats from the same data: a JSON manifest for tooling and
audit, and a self-contained HTML report an operator can email to the client as
evidence for sign-off. The HTML embeds all styling - no CDN, because the machine
that reads it may have no internet access at all.
"""

from __future__ import annotations

import hashlib
import html
import json
import platform
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .logging_setup import get_logger
from .tools import human_bytes

log = get_logger("report")


@dataclass
class MigrationManifest:
    """The record of what was migrated, from where, to where, and whether it verified."""

    repository: str = ""
    tool_version: str = __version__
    generated_at: str = ""
    host: str = ""
    operator: str = ""
    source: Dict[str, Any] = field(default_factory=dict)
    target: Dict[str, Any] = field(default_factory=dict)
    analysis: Dict[str, Any] = field(default_factory=dict)
    acquisition: Dict[str, Any] = field(default_factory=dict)
    conversion: Dict[str, Any] = field(default_factory=dict)
    export: Dict[str, Any] = field(default_factory=dict)
    push: Dict[str, Any] = field(default_factory=dict)
    verification: Dict[str, Any] = field(default_factory=dict)
    sync: Dict[str, Any] = field(default_factory=dict)
    stages: List[Dict[str, Any]] = field(default_factory=list)
    checksum: str = ""

    def finalise(self) -> "MigrationManifest":
        self.generated_at = datetime.now(timezone.utc).isoformat()
        self.host = platform.node()
        payload = self.to_dict(include_checksum=False)
        self.checksum = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return self

    def to_dict(self, include_checksum: bool = True) -> Dict[str, Any]:
        data = {
            "repository": self.repository,
            "tool_version": self.tool_version,
            "generated_at": self.generated_at,
            "host": self.host,
            "operator": self.operator,
            "source": self.source,
            "target": self.target,
            "analysis": self.analysis,
            "acquisition": self.acquisition,
            "conversion": self.conversion,
            "export": self.export,
            "push": self.push,
            "verification": self.verification,
            "sync": self.sync,
            "stages": self.stages,
        }
        if include_checksum:
            data["checksum"] = self.checksum
        return data

    def write_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
        log.info("wrote migration manifest %s", path)
        return path


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #

_CSS = """
:root {
  --bg: #ffffff; --fg: #1b1f24; --muted: #5c6570; --line: #d8dee4;
  --card: #f6f8fa; --ok: #1a7f37; --warn: #9a6700; --bad: #cf222e; --accent: #0969da;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --line: #30363d;
    --card: #161b22; --ok: #3fb950; --warn: #d29922; --bad: #f85149; --accent: #58a6ff;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
.wrap { max-width: 1000px; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
h2 { font-size: 1.15rem; margin: 2.25rem 0 .75rem; padding-bottom: .35rem;
     border-bottom: 1px solid var(--line); }
h3 { font-size: 1rem; margin: 1.5rem 0 .5rem; }
.sub { color: var(--muted); margin: 0 0 1.5rem; }
.verdict { display: inline-block; padding: .35rem .8rem; border-radius: 999px;
           font-weight: 600; font-size: .85rem; }
.verdict.ok { background: rgba(63,185,80,.15); color: var(--ok); }
.verdict.bad { background: rgba(248,81,73,.15); color: var(--bad); }
.verdict.warn { background: rgba(210,153,34,.15); color: var(--warn); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: .75rem; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: .85rem 1rem; }
.tile .k { color: var(--muted); font-size: .78rem; text-transform: uppercase;
           letter-spacing: .04em; }
.tile .v { font-size: 1.35rem; font-weight: 600; margin-top: .15rem; word-break: break-word; }
.tile .v.small { font-size: .95rem; font-weight: 500; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
.scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: .8rem; text-transform: uppercase;
     letter-spacing: .03em; white-space: nowrap; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
code, .mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
              font-size: .87em; }
.finding { border-left: 3px solid var(--line); padding: .5rem .85rem; margin: .5rem 0;
           background: var(--card); border-radius: 0 6px 6px 0; }
.finding.blocker { border-left-color: var(--bad); }
.finding.warning { border-left-color: var(--warn); }
.finding.info { border-left-color: var(--accent); }
.finding .msg { font-weight: 500; }
.finding .remedy { color: var(--muted); font-size: .88rem; margin-top: .2rem; }
.pill { display: inline-block; padding: .1rem .5rem; border-radius: 4px; background: var(--card);
        border: 1px solid var(--line); font-size: .8rem; margin: .12rem .2rem .12rem 0; }
footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--line);
         color: var(--muted); font-size: .82rem; }
.ok-text { color: var(--ok); } .bad-text { color: var(--bad); } .warn-text { color: var(--warn); }
"""


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _tile(key: str, value: Any, small: bool = False) -> str:
    cls = "v small" if small else "v"
    return f'<div class="tile"><div class="k">{_e(key)}</div><div class="{cls}">{_e(value)}</div></div>'


def _num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return _e(value)


def render_html(manifest: MigrationManifest) -> str:
    verification = manifest.verification or {}
    analysis = manifest.analysis or {}
    export = manifest.export or {}
    push = manifest.push or {}

    verified = verification.get("ok")
    if verification:
        verdict_cls, verdict_text = ("ok", "Verified") if verified else ("bad", "Differences found")
    else:
        verdict_cls, verdict_text = ("warn", "Not verified")

    parts: List[str] = [
        "<div class=\"wrap\">",
        f"<h1>SVN to GitLab migration report</h1>",
        f'<p class="sub"><strong>{_e(manifest.repository)}</strong> &middot; '
        f'generated {_e(manifest.generated_at)} on {_e(manifest.host)} '
        f'by svn2gitlab {_e(manifest.tool_version)}</p>',
        f'<p><span class="verdict {verdict_cls}">{_e(verdict_text)}</span></p>',
    ]

    # Headline numbers
    parts.append("<h2>Summary</h2><div class=\"grid\">")
    parts.append(_tile("SVN revisions", _num(analysis.get("revision_count"))))
    parts.append(_tile("Git commits", _num(export.get("commit_count"))))
    parts.append(_tile("Branches", _num(export.get("branch_count"))))
    parts.append(_tile("Tags", _num(export.get("tag_count"))))
    parts.append(_tile("Authors", _num(len(analysis.get("authors") or {}))))
    if push.get("repository_size"):
        parts.append(_tile("Repository size", human_bytes(push["repository_size"])))
    parts.append("</div>")

    # Source and target
    parts.append("<h2>Source and target</h2><div class=\"grid\">")
    parts.append(_tile("SVN URL", manifest.source.get("url", ""), small=True))
    parts.append(_tile("Repository UUID", analysis.get("repository_uuid", ""), small=True))
    parts.append(_tile("Layout", (analysis.get("layout") or {}).get("mode", ""), small=True))
    parts.append(_tile("HEAD revision", f"r{_num(analysis.get('head_revision'))}", small=True))
    parts.append(_tile("GitLab project", push.get("project_path", ""), small=True))
    parts.append(_tile("Default branch", export.get("default_branch", ""), small=True))
    parts.append("</div>")
    if push.get("project_url"):
        parts.append(f'<p><a href="{_e(push["project_url"])}">{_e(push["project_url"])}</a></p>')

    # Verification detail
    if verification:
        parts.append("<h2>Verification</h2>")
        parts.append("<div class=\"grid\">")
        parts.append(_tile("Mode", verification.get("mode", "")))
        parts.append(_tile("Differences", _num(verification.get("total_differences", 0))))
        coverage = verification.get("revision_coverage")
        if coverage is not None:
            parts.append(_tile("Revision coverage", f"{float(coverage) * 100:.1f}%"))
        parts.append(_tile("Duration", f"{verification.get('duration', 0):.0f}s"))
        parts.append("</div>")

        branches = verification.get("branches") or []
        if branches:
            parts.append('<div class="scroll"><table><thead><tr>'
                         "<th>Branch</th><th>SVN path</th><th>Revision</th>"
                         "<th>Files</th><th>Matched</th><th>LFS</th><th>EOL-normalised</th>"
                         "<th>Result</th></tr></thead><tbody>")
            for branch in branches:
                if branch.get("skipped"):
                    status = f'<span class="warn-text">skipped: {_e(branch.get("skip_reason"))}</span>'
                elif branch.get("ok"):
                    status = '<span class="ok-text">identical</span>'
                else:
                    status = (f'<span class="bad-text">{_num(branch.get("difference_count"))} '
                              "difference(s)</span>")
                parts.append(
                    f'<tr><td><code>{_e(branch.get("branch"))}</code></td>'
                    f'<td><code>{_e(branch.get("svn_path"))}</code></td>'
                    f'<td class="num">r{_num(branch.get("svn_revision"))}</td>'
                    f'<td class="num">{_num(branch.get("files_compared"))}</td>'
                    f'<td class="num">{_num(branch.get("files_matched"))}</td>'
                    f'<td class="num">{_num(branch.get("lfs_files"))}</td>'
                    f'<td class="num">{_num(branch.get("eol_normalised_matches"))}</td>'
                    f"<td>{status}</td></tr>")
            parts.append("</tbody></table></div>")

        for branch in branches:
            diffs = branch.get("differences") or []
            if not diffs:
                continue
            parts.append(f'<h3>Differences on <code>{_e(branch.get("branch"))}</code></h3>')
            parts.append('<div class="scroll"><table><thead><tr><th>Path</th><th>Kind</th>'
                         "<th>Detail</th></tr></thead><tbody>")
            for diff in diffs[:200]:
                parts.append(f'<tr><td><code>{_e(diff.get("path"))}</code></td>'
                             f'<td>{_e(diff.get("kind"))}</td>'
                             f'<td>{_e(diff.get("detail"))}</td></tr>')
            parts.append("</tbody></table></div>")

        if verification.get("missing_tags"):
            parts.append("<h3>Tags present in SVN but not in Git</h3><p>")
            parts.append("".join(f'<span class="pill">{_e(t)}</span>'
                                 for t in verification["missing_tags"][:100]))
            parts.append("</p>")

    # Authors
    authors = analysis.get("authors") or {}
    if authors:
        parts.append("<h2>Authors</h2>")
        mapped = verification.get("authors_mapped")
        matched = verification.get("authors_matched_in_gitlab")
        if mapped is not None:
            parts.append(f"<p>{_num(mapped)} of {_num(len(authors))} SVN authors mapped to a Git "
                         f"identity"
                         + (f"; {_num(matched)} matched an existing GitLab user." if matched
                            is not None else ".") + "</p>")
        parts.append('<div class="scroll"><table><thead><tr><th>SVN user</th>'
                     "<th>Commits</th></tr></thead><tbody>")
        for user, count in list(authors.items())[:100]:
            parts.append(f'<tr><td><code>{_e(user or "(no author)")}</code></td>'
                         f'<td class="num">{_num(count)}</td></tr>')
        parts.append("</tbody></table></div>")
        unmatched = verification.get("unmatched_author_emails") or []
        if unmatched:
            parts.append("<p>No GitLab user was found for these addresses, so their commits will "
                         "not link to a profile:</p><p>")
            parts.append("".join(f'<span class="pill">{_e(e)}</span>' for e in unmatched[:60]))
            parts.append("</p>")

    # Findings from analysis
    findings = analysis.get("findings") or []
    if findings:
        parts.append("<h2>Findings</h2>")
        order = {"blocker": 0, "warning": 1, "info": 2}
        for finding in sorted(findings, key=lambda f: order.get(f.get("level", "info"), 3)):
            level = finding.get("level", "info")
            parts.append(
                f'<div class="finding {_e(level)}"><div class="msg">{_e(finding.get("message"))}</div>'
                + (f'<div class="remedy">{_e(finding.get("remedy"))}</div>'
                   if finding.get("remedy") else "")
                + "</div>")

    # Conversion detail
    if export:
        parts.append("<h2>Conversion detail</h2><div class=\"grid\">")
        parts.append(_tile("History rewritten", "yes" if export.get("history_rewritten") else "no",
                           small=True))
        filter_stats = export.get("filter_stats") or {}
        if filter_stats:
            parts.append(_tile("Messages cleaned", _num(filter_stats.get("messages_rewritten")),
                               small=True))
        lfs = export.get("lfs") or {}
        if lfs:
            parts.append(_tile("LFS files", _num(lfs.get("pointer_count")), small=True))
            parts.append(_tile("LFS bytes", human_bytes(lfs.get("matched_bytes") or 0), small=True))
        parts.append(_tile(".gitignore generated",
                           "yes" if export.get("gitignore_committed") else "no", small=True))
        parts.append("</div>")

        renamed = export.get("renamed_refs") or []
        if renamed:
            parts.append("<h3>Renamed refs</h3><div class=\"scroll\"><table><thead><tr>"
                         "<th>Subversion</th><th>Git</th></tr></thead><tbody>")
            for entry in renamed[:200]:
                parts.append(f'<tr><td><code>{_e(entry.get("from"))}</code></td>'
                             f'<td><code>{_e(entry.get("to"))}</code></td></tr>')
            parts.append("</tbody></table></div>")

        skipped = export.get("skipped") or []
        if skipped:
            parts.append("<h3>Skipped refs</h3><div class=\"scroll\"><table><thead><tr>"
                         "<th>Ref</th><th>Reason</th></tr></thead><tbody>")
            for entry in skipped[:200]:
                parts.append(f'<tr><td><code>{_e(entry.get("ref"))}</code></td>'
                             f'<td>{_e(entry.get("reason"))}</td></tr>')
            parts.append("</tbody></table></div>")

    # Externals need a human decision; make them prominent.
    externals = analysis.get("externals") or {}
    if externals:
        parts.append("<h2>svn:externals not migrated</h2>")
        parts.append("<p>Git has no equivalent of svn:externals. Each of these needs a "
                     "replacement (submodule, package dependency, or a vendored copy).</p>")
        parts.append('<div class="scroll"><table><thead><tr><th>Path</th><th>Definition</th>'
                     "</tr></thead><tbody>")
        for path, value in list(externals.items())[:100]:
            parts.append(f'<tr><td><code>{_e(path or "/")}</code></td>'
                         f'<td><code>{_e(value)}</code></td></tr>')
        parts.append("</tbody></table></div>")

    # Stage timings
    if manifest.stages:
        parts.append("<h2>Stages</h2><div class=\"scroll\"><table><thead><tr><th>Stage</th>"
                     "<th>Status</th><th>Duration</th><th>Detail</th></tr></thead><tbody>")
        for stage in manifest.stages:
            status = stage.get("status", "")
            cls = {"succeeded": "ok-text", "failed": "bad-text"}.get(status, "warn-text")
            parts.append(f'<tr><td>{_e(stage.get("name"))}</td>'
                         f'<td class="{cls}">{_e(status)}</td>'
                         f'<td class="num">{_e(_duration(stage.get("duration")))}</td>'
                         f'<td>{_e(stage.get("message") or stage.get("error") or "")}</td></tr>')
        parts.append("</tbody></table></div>")

    parts.append(
        "<footer>"
        f"<div>Manifest checksum: <code>{_e(manifest.checksum)}</code></div>"
        "<div>This report is generated from the migration's own records. The verification "
        "section compares Subversion content against the pushed Git objects directly; where it "
        "reports <em>identical</em>, every file at that revision hashes to the same value on "
        "both sides.</div>"
        "</footer></div>")
    return "".join(parts)


def _duration(seconds: Any) -> str:
    try:
        value = float(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if value < 60:
        return f"{value:.0f}s"
    if value < 3600:
        return f"{value / 60:.1f}m"
    return f"{value / 3600:.1f}h"


def write_html_report(manifest: MigrationManifest, path: Path, title: Optional[str] = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_e(title or f'Migration report - {manifest.repository}')}</title>"
        f"<style>{_CSS}</style></head><body>{render_html(manifest)}</body></html>"
    )
    path.write_text(document, encoding="utf-8")
    log.info("wrote HTML report %s", path)
    return path


def write_reports(manifest: MigrationManifest, reports_dir: Path) -> Dict[str, Path]:
    manifest.finalise()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = reports_dir / f"manifest-{stamp}.json"
    html_path = reports_dir / f"report-{stamp}.html"
    manifest.write_json(json_path)
    write_html_report(manifest, html_path)
    # Stable names so automation and the web UI always find the newest.
    latest_json = reports_dir / "manifest-latest.json"
    latest_html = reports_dir / "report-latest.html"
    latest_json.write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    latest_html.write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")
    return {"json": json_path, "html": html_path,
            "latest_json": latest_json, "latest_html": latest_html}
