"""The migration pipeline.

Stages, in order:

    preflight -> analyze -> authors -> acquire -> convert -> export -> push -> verify

Each stage records its outcome in the state store, so a re-run resumes at the first
stage that has not succeeded. That is not a convenience feature: on a repository with
100,000 revisions the convert stage runs for a day, and a machine that reboots
mid-way must not start over.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Callable, Dict, List, Optional, Sequence

from .config import (AuthorPolicy, FastPath, MigrationConfig, ResolvedRepo,
                     VerifyMode)
from .convert.export import ExportResult, Exporter
from .convert.git import Git
from .convert.engine import build_mirror, resolve_engine
from .convert.gitsvn import GitSvnMirror, FetchResult
from .errors import (AbortedError, ConfigError, ConversionError, Svn2GitlabError,
                     ToolMissingError, VerificationError)
from .gitlab.api import GitLabClient
from .gitlab.push import Publisher, PushResult
from .logging_setup import get_logger
from .report import MigrationManifest, write_reports
from .state import JobStatus, StageStatus, StateStore
from .svn import acquire as acquire_mod
from .svn.analyze import (RepoAnalysis, collect_externals, collect_history_stats,
                          collect_refs, collect_tree_stats, evaluate_findings,
                          layout_for_source)
from .svn.authors import AUTHORS_HEADER, AuthorMap, build_author_map, unmapped
from .svn.client import SvnClient, url_join
from .tools import ToolSet, detect_tools, free_disk_bytes, human_bytes
from .verify.verify import BranchVerification, VerificationReport, Verifier

log = get_logger("pipeline")

STAGES = ["preflight", "analyze", "authors", "acquire", "convert", "export", "push", "verify"]

# Rough multiplier from SVN repository size to converted Git working set (mirror +
# export + a verification export). Deliberately generous - running out of disk at
# hour nine of a conversion is the worst possible failure.
DISK_MULTIPLIER = 4.0


@dataclass
class StageOutcome:
    name: str
    status: str
    duration: float = 0.0
    message: str = ""
    error: str = ""
    result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "duration": self.duration,
                "message": self.message, "error": self.error}


@dataclass
class MigrationOutcome:
    repository: str
    job_id: str = ""
    status: str = "pending"
    stages: List[StageOutcome] = field(default_factory=list)
    analysis: Optional[RepoAnalysis] = None
    export: Optional[ExportResult] = None
    push: Optional[PushResult] = None
    verification: Optional[VerificationReport] = None
    reports: Dict[str, str] = field(default_factory=dict)
    error: str = ""
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    def to_dict(self) -> dict:
        return {
            "repository": self.repository,
            "job_id": self.job_id,
            "status": self.status,
            "ok": self.ok,
            "duration": round(self.duration, 1),
            "stages": [s.to_dict() for s in self.stages],
            "reports": self.reports,
            "error": self.error,
            "project_url": self.push.project_url if self.push else "",
        }


ProgressFn = Callable[[str, float, str], None]


class MigrationPipeline:
    """Owns one repository's migration from end to end."""

    def __init__(
        self,
        repo: ResolvedRepo,
        state: StateStore,
        tools: Optional[ToolSet] = None,
        cancel: Optional[Event] = None,
        on_progress: Optional[ProgressFn] = None,
        job_id: str = "",
        dry_run: bool = False,
    ) -> None:
        self.repo = repo
        self.state = state
        self.tools = tools or detect_tools()
        self.cancel = cancel or Event()
        self._on_progress = on_progress
        self.job_id = job_id
        self.dry_run = dry_run

        self.repo.workdir.mkdir(parents=True, exist_ok=True)
        self.repo.reports_dir.mkdir(parents=True, exist_ok=True)

        # Lazily built, but shared between the initial run and later sync rounds.
        self._svn: Optional[SvnClient] = None
        self._mirror: Optional[GitSvnMirror] = None
        self._exporter: Optional[Exporter] = None
        self._gitlab: Optional[GitLabClient] = None
        self._analysis: Optional[RepoAnalysis] = None
        self._authors: Optional[AuthorMap] = None
        self._acquisition: Optional[acquire_mod.Acquisition] = None
        self._engine: Optional[str] = None
        self._current_stage = ""

    # -- shared components ---------------------------------------------------

    @property
    def svn(self) -> SvnClient:
        if self._svn is None:
            self._svn = SvnClient(
                self.tools,
                username=self.repo.source.username,
                password=self.repo.source.password,
                trust_server_cert=self.repo.source.trust_server_cert,
                config_dir=self.repo.source.config_dir,
                cancel=self.cancel,
            )
        return self._svn

    @property
    def gitlab(self) -> GitLabClient:
        if self._gitlab is None:
            self._gitlab = GitLabClient(self.repo.target)
        return self._gitlab

    def source_url(self) -> str:
        """The URL to inspect: the live source, including any configured subpath."""
        base = self.repo.source.url or self.repo.source.effective_url()
        if self.repo.source.subpath:
            return url_join(base, self.repo.source.subpath)
        return base

    def convert_url(self) -> str:
        """The URL to convert from - the acquired local copy when we have one."""
        if self._acquisition:
            return self._acquisition.url
        stored = self.state.stage_result(self.job_id, "acquire") if self.job_id else None
        if stored and stored.get("url"):
            return str(stored["url"])
        return self.source_url()

    @property
    def analysis_path(self) -> Path:
        return self.repo.workdir / "analysis.json"

    @property
    def analysis(self) -> RepoAnalysis:
        """The last analysis of this repository, from the job record or from disk.

        `sync` and `cutover` run without a job, so the state store has nothing for
        them. They still need the layout and the live branch/tag lists, which is why
        the analyze stage also writes `analysis.json` into the work directory.
        """
        if self._analysis is None:
            stored = self.state.stage_result(self.job_id, "analyze") if self.job_id else None
            if not stored and self.analysis_path.is_file():
                try:
                    stored = json.loads(self.analysis_path.read_text(encoding="utf-8"))
                    log.debug("loaded cached analysis from %s", self.analysis_path)
                except (OSError, json.JSONDecodeError) as exc:
                    log.warning("could not read %s: %s", self.analysis_path, exc)
            self._analysis = _analysis_from_dict(stored) if stored else RepoAnalysis()
        return self._analysis

    def live_refs(self) -> tuple:
        """Branch and tag names that exist in Subversion right now.

        Queried live rather than taken from the cached analysis, because a branch
        deleted between the migration and the cutover must stop being published.
        Returns `(None, None)` if Subversion cannot be reached, which the exporter
        reads as "do not filter" - degrading to publishing a stale branch is far
        better than silently dropping every branch.
        """
        try:
            probe = RepoAnalysis()
            collect_refs(self.svn, self.source_url(), self.mirror.layout, probe)
            return probe.branch_names, probe.tag_names
        except Svn2GitlabError as exc:
            log.warning("could not list live refs from Subversion (%s); "
                        "publishing every ref the mirror holds", exc)
            return None, None

    def local_repo_path(self) -> Optional[Path]:
        """The local Subversion repository, if the acquisition stage produced one."""
        if self._acquisition and self._acquisition.local_repo:
            return Path(self._acquisition.local_repo)
        stored = self.state.stage_result(self.job_id, "acquire") if self.job_id else None
        if stored and stored.get("local_repo"):
            return Path(str(stored["local_repo"]))
        if self.repo.source.local_path:
            return Path(self.repo.source.local_path)
        if self.repo.local_repo_dir.is_dir():
            return self.repo.local_repo_dir
        return None

    def engine(self) -> str:
        if self._engine is None:
            self._engine = resolve_engine(self.repo.convert.engine, self.tools,
                                          has_local_repo=self.local_repo_path() is not None)
            log.info("conversion engine: %s", self._engine)
        return self._engine

    @property
    def mirror(self):
        if self._mirror is None:
            layout = self.analysis.layout
            if not layout.trunk and not layout.branches and not layout.tags:
                layout = layout_for_source(self.svn, self.repo.source, self.source_url())
            self._mirror = build_mirror(
                engine=self.engine(),
                tools=self.tools,
                repo_dir=self.repo.mirror_dir,
                source_url=self.convert_url(),
                layout=layout,
                convert=self.repo.convert,
                source=self.repo.source,
                authors_file=self.repo.authors_file,
                authors=self._authors or AuthorMap.load(self.repo.authors_file),
                local_repo=self.local_repo_path(),
                uuid=self.analysis.repository_uuid,
                cancel=self.cancel,
            )
        return self._mirror

    @property
    def exporter(self) -> Exporter:
        if self._exporter is None:
            branches, tags = self.live_refs()
            self._exporter = Exporter(
                tools=self.tools, mirror=self.mirror, export_dir=self.repo.export_dir,
                convert=self.repo.convert, cancel=self.cancel,
                live_branches=branches, live_tags=tags,
            )
        return self._exporter

    # -- progress plumbing ---------------------------------------------------

    def _progress(self, fraction: float, message: str) -> None:
        if self.job_id:
            self.state.stage_progress(self.job_id, self._current_stage, fraction, message)
        if self._on_progress:
            self._on_progress(self._current_stage, fraction, message)

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise AbortedError("migration cancelled by the operator")

    # -- stage runner --------------------------------------------------------

    def _stage(self, name: str, func: Callable[[], Dict[str, Any]],
               outcome: MigrationOutcome, skip_if_done: bool = True) -> Dict[str, Any]:
        self._check_cancel()
        self._current_stage = name

        if skip_if_done and self.job_id and self.state.stage_succeeded(self.job_id, name):
            stored = self.state.stage_result(self.job_id, name) or {}
            log.info("[%s] already completed; resuming past it", name)
            outcome.stages.append(StageOutcome(name, "skipped", message="already completed"))
            return stored

        started = time.time()
        log.info("[%s] starting", name)
        if self.job_id:
            self.state.stage_start(self.job_id, name)
        try:
            result = func() or {}
        except AbortedError as exc:
            if self.job_id:
                self.state.stage_finish(self.job_id, name, StageStatus.CANCELLED, error=str(exc))
            outcome.stages.append(StageOutcome(name, "cancelled", time.time() - started,
                                               error=str(exc)))
            raise
        except Svn2GitlabError as exc:
            if self.job_id:
                self.state.stage_finish(self.job_id, name, StageStatus.FAILED, error=str(exc))
            outcome.stages.append(StageOutcome(name, "failed", time.time() - started,
                                               error=str(exc)))
            log.error("[%s] failed: %s", name, exc)
            raise
        except Exception as exc:  # noqa: BLE001
            message = f"unexpected error in stage {name}: {exc}"
            if self.job_id:
                self.state.stage_finish(self.job_id, name, StageStatus.FAILED, error=message)
            outcome.stages.append(StageOutcome(name, "failed", time.time() - started,
                                               error=message))
            log.exception("[%s] failed unexpectedly", name)
            raise Svn2GitlabError(message) from exc

        duration = time.time() - started
        if self.job_id:
            self.state.stage_finish(self.job_id, name, StageStatus.SUCCEEDED, result=result)
        outcome.stages.append(StageOutcome(name, "succeeded", duration, result=result))
        log.info("[%s] completed in %s", name, _fmt_duration(duration))
        return result

    # ----------------------------------------------------------------- run --

    def run(
        self,
        stages: Optional[Sequence[str]] = None,
        restart_from: str = "",
        force_push: bool = False,
    ) -> MigrationOutcome:
        started = time.time()
        wanted = list(stages) if stages else list(STAGES)
        outcome = MigrationOutcome(repository=self.repo.name, job_id=self.job_id)

        if restart_from:
            if restart_from not in STAGES:
                raise ConfigError(f"unknown stage {restart_from!r}",
                                  f"Valid stages: {', '.join(STAGES)}")
            if self.job_id:
                self.state.reset_stages_from(self.job_id, restart_from)
            index = STAGES.index(restart_from)
            wanted = [s for s in wanted if STAGES.index(s) >= index]
            log.info("restarting from stage %s", restart_from)

        if self.job_id:
            self.state.start_job(self.job_id)

        try:
            if "preflight" in wanted:
                self._stage("preflight", self._stage_preflight, outcome, skip_if_done=False)
            if "analyze" in wanted:
                self._stage("analyze", self._stage_analyze, outcome)
            if "authors" in wanted:
                self._stage("authors", self._stage_authors, outcome)
            if "acquire" in wanted:
                self._stage("acquire", self._stage_acquire, outcome)
            if "convert" in wanted:
                self._stage("convert", self._stage_convert, outcome)
            if "export" in wanted:
                self._stage("export", lambda: self._stage_export(outcome), outcome)
            if "push" in wanted and not self.dry_run:
                self._stage("push", lambda: self._stage_push(outcome, force_push), outcome)
            elif "push" in wanted:
                outcome.stages.append(StageOutcome("push", "skipped", message="dry run"))
            # Verification compares the local conversion against Subversion, so it is
            # just as meaningful on a dry run - that is precisely when you want to know
            # whether the conversion is faithful, before anything reaches GitLab.
            if "verify" in wanted:
                self._stage("verify", lambda: self._stage_verify(outcome), outcome)

            outcome.status = "succeeded"
            if self.job_id:
                self.state.finish_job(self.job_id, JobStatus.SUCCEEDED,
                                      summary=outcome.to_dict())
        except AbortedError as exc:
            outcome.status = "cancelled"
            outcome.error = str(exc)
            if self.job_id:
                self.state.finish_job(self.job_id, JobStatus.CANCELLED, error=str(exc))
        except Svn2GitlabError as exc:
            outcome.status = "failed"
            outcome.error = str(exc)
            if self.job_id:
                self.state.finish_job(self.job_id, JobStatus.FAILED, error=str(exc))
        finally:
            outcome.duration = time.time() - started
            try:
                outcome.reports = {k: str(v) for k, v in self._write_reports(outcome).items()}
            except Exception:
                log.debug("could not write reports", exc_info=True)

        return outcome

    # --------------------------------------------------------------- stages --

    def _stage_preflight(self) -> Dict[str, Any]:
        """Fail fast, and fail with instructions."""
        self._progress(0.1, "checking external tools")
        problems: List[str] = []
        warnings: List[str] = []

        for name in ("git", "svn"):
            info = getattr(self.tools, name.replace("-", "_"))
            if not info.available:
                problems.append(f"{name}: {info.detail or 'not found'}")
        # git-svn is only needed if that engine is the one that will run. The native
        # engine needs svnadmin instead, and neither is universally present.
        if not problems:
            try:
                engine = resolve_engine(
                    self.repo.convert.engine, self.tools,
                    has_local_repo=bool(self.repo.source.local_path)
                    or self.repo.source.fast_path != FastPath.NONE)
                self._engine = engine
            except ConversionError as exc:
                problems.append(str(exc))
        if self.repo.convert.lfs.enabled and not self.tools.git_lfs.available:
            problems.append(f"git-lfs: {self.tools.git_lfs.detail or 'not found'} "
                            "(required because convert.lfs.enabled is true)")
        if problems:
            raise ToolMissingError("required tools are missing or unusable:\n  - "
                                   + "\n  - ".join(problems),
                                   "Run `svn2gitlab doctor` for install instructions.")

        self._progress(0.3, "checking Subversion connectivity")
        info = self.svn.info(self.source_url())
        head = info.revision

        self._progress(0.5, "checking disk space")
        estimated = 0
        if self.repo.source.local_path:
            estimated = acquire_mod._dir_size(Path(self.repo.source.local_path))
        needed = int((estimated or head * 40 * 1024) * DISK_MULTIPLIER)
        free = free_disk_bytes(self.repo.workdir)
        if free and needed and free < needed:
            warnings.append(
                f"only {human_bytes(free)} free on the work volume; this migration is "
                f"estimated to need about {human_bytes(needed)}. Point `workdir` at a "
                "larger disk before starting."
            )
        if free and needed and free < needed * 0.5:
            raise Svn2GitlabError(
                f"not enough disk space: {human_bytes(free)} free, about "
                f"{human_bytes(needed)} required",
                "Set `workdir` to a volume with more space. The mirror, the export and the "
                "verification export each hold a copy of the working set.",
            )

        self._progress(0.75, "checking GitLab access")
        gitlab_info: Dict[str, Any] = {}
        if not self.dry_run:
            gitlab_info = self.gitlab.check_access()
            log.info("authenticated to %s as %s (GitLab %s)", gitlab_info.get("gitlab_url"),
                     gitlab_info.get("username"), gitlab_info.get("gitlab_version"))

        self._progress(1.0, "preflight passed")
        for warning in warnings:
            log.warning("%s", warning)
        return {
            "tools": self.tools.to_dict(),
            "svn": {"url": info.url, "root": info.repository_root, "uuid": info.repository_uuid,
                    "head_revision": head},
            "gitlab": gitlab_info,
            "disk_free": free,
            "disk_estimated": needed,
            "warnings": warnings,
        }

    def _stage_analyze(self) -> Dict[str, Any]:
        url = self.source_url()
        analysis = RepoAnalysis(url=url, analysed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))

        self._progress(0.05, "reading repository information")
        info = self.svn.info(url)
        analysis.repository_root = info.repository_root
        analysis.repository_uuid = info.repository_uuid
        analysis.head_revision = info.revision

        self._progress(0.10, "detecting the repository layout")
        analysis.layout = layout_for_source(self.svn, self.repo.source, url)
        log.info("layout: %s (confidence=%s) - %s", analysis.layout.mode.value,
                 analysis.layout.confidence, "; ".join(analysis.layout.rationale))

        self._progress(0.15, "listing branches and tags")
        collect_refs(self.svn, url, analysis.layout, analysis)

        self._progress(0.20, "scanning the revision history")
        collect_history_stats(
            self.svn, url, analysis,
            start=self.repo.source.revision_start,
            end=self.repo.source.revision_end,
            on_progress=lambda f, m: self._progress(0.20 + 0.50 * f, m),
        )

        self._progress(0.72, "profiling file sizes at HEAD")
        tree_url = url
        if analysis.layout.trunk and analysis.layout.trunk != ".":
            tree_url = url_join(url, analysis.layout.trunk)
        collect_tree_stats(
            self.svn, tree_url, analysis,
            lfs_threshold_bytes=self.repo.convert.lfs.size_threshold_mb * 1024 * 1024,
            lfs_extensions=self.repo.convert.lfs.extensions,
        )

        self._progress(0.88, "checking for svn:externals")
        collect_externals(self.svn, tree_url, analysis)

        self._progress(0.95, "evaluating findings")
        existing = AuthorMap.load(self.repo.authors_file)
        evaluate_findings(analysis, self.repo.source,
                          known_authors={u: str(i) for u, i in existing},
                          lfs_enabled=self.repo.convert.lfs.enabled)

        blockers = analysis.blockers
        if blockers:
            raise Svn2GitlabError(
                "analysis found blocking problems:\n  - "
                + "\n  - ".join(f.message for f in blockers),
                blockers[0].remedy,
            )

        self._analysis = analysis
        data = analysis.to_dict()
        # Persist alongside the work directory so `sync`, `cutover` and `verify` -
        # which run without a job record - inherit the layout and reference lists.
        try:
            self.analysis_path.write_text(json.dumps(data, indent=2, default=str),
                                          encoding="utf-8")
        except OSError as exc:
            log.warning("could not write %s: %s", self.analysis_path, exc)

        self._progress(1.0, f"r{analysis.head_revision:,}, {analysis.revision_count:,} revisions, "
                            f"{len(analysis.authors)} authors")
        return data

    def _stage_authors(self) -> Dict[str, Any]:
        self._progress(0.2, "building the author map")
        config = self.repo.authors
        existing = AuthorMap.load(self.repo.authors_file)
        amap, synthesised = build_author_map(self.analysis.authors.keys(), config, existing)

        missing = unmapped(self.analysis.authors.keys(), amap)
        if missing and config.policy == AuthorPolicy.STRICT:
            raise ConfigError(
                f"{len(missing)} SVN author(s) are not mapped: {', '.join(missing[:15])}",
                f"Add them to {self.repo.authors_file} as `svnuser = Real Name <email>`, or set "
                "`authors.policy: generate` to synthesise addresses automatically.",
            )

        self._progress(0.6, "writing the author map")
        header = list(AUTHORS_HEADER)
        if synthesised:
            header += ["", f"Generated automatically for {len(synthesised)} user(s): "
                           + ", ".join(sorted(synthesised)[:30])]
        amap.save(self.repo.authors_file, header=header)
        amap.write_git_svn_file(self.repo.workdir / "authors-git-svn.txt")

        invalid = amap.invalid_emails()
        duplicates = amap.duplicate_emails()
        for user, email in invalid[:20]:
            log.warning("author %s has an implausible email address %r", user, email)
        for email, users in list(duplicates.items())[:10]:
            log.info("email %s is shared by %s", email, ", ".join(users))

        self._authors = amap
        self._progress(1.0, f"{len(amap)} author(s) mapped "
                            f"({len(synthesised)} generated)")
        return {
            "count": len(amap),
            "synthesised": synthesised,
            "unmapped": missing,
            "invalid_emails": [{"user": u, "email": e} for u, e in invalid],
            "duplicate_emails": duplicates,
            "authors_file": str(self.repo.authors_file),
        }

    def _stage_acquire(self) -> Dict[str, Any]:
        estimated = 0
        if self.repo.source.local_path:
            estimated = acquire_mod._dir_size(Path(self.repo.source.local_path))
        acquisition = acquire_mod.acquire(
            source=self.repo.source,
            tools=self.tools,
            client=self.svn,
            target_dir=self.repo.local_repo_dir,
            head_revision=self.analysis.head_revision,
            estimated_bytes=estimated,
            cancel=self.cancel,
            on_progress=self._progress,
            require_local=self.engine() == "native",
        )
        self._acquisition = acquisition
        # The mirror must be rebuilt against the acquired URL.
        self._mirror = None
        self._progress(1.0, f"{acquisition.strategy}: {acquisition.note}")
        return acquisition.to_dict()

    def _stage_convert(self) -> Dict[str, Any]:
        mirror = self.mirror
        mirror.authors_file = self.repo.workdir / "authors-git-svn.txt"
        self._progress(0.02, "initialising the git-svn mirror")
        mirror.init()

        already = mirror.last_fetched_revision()
        if already:
            log.info("mirror already contains revisions up to r%d", already)

        result = mirror.fetch(
            revision_start=self.repo.source.revision_start,
            revision_end=self.repo.source.revision_end,
            head_revision=self.analysis.head_revision,
            on_progress=self._progress,
        )
        payload = result.to_dict()
        payload["engine"] = self.engine()
        return payload

    def _stage_export(self, outcome: MigrationOutcome) -> Dict[str, Any]:
        export = self.exporter.build(on_progress=self._progress)
        outcome.export = export
        return export.to_dict()

    def _stage_push(self, outcome: MigrationOutcome, force: bool) -> Dict[str, Any]:
        export = outcome.export or self.exporter.build(on_progress=self._progress)
        git = Git(self.tools, self.repo.export_dir, cancel=self.cancel)
        publisher = Publisher(self.gitlab, self.repo.target, git, cancel=self.cancel)

        previously_pushed = self.already_published()
        self._progress(0.02, "running push preflight checks")
        checks = publisher.preflight(self.repo.convert.default_branch,
                                     self.repo.convert.lfs.enabled,
                                     allow_existing=previously_pushed)
        blockers = checks.get("blockers") or []
        if blockers:
            raise Svn2GitlabError("GitLab preflight failed:\n  - " + "\n  - ".join(blockers),  # type: ignore[arg-type]
                                  "Resolve these before pushing; nothing has been uploaded.")
        for warning in (checks.get("warnings") or []):  # type: ignore[union-attr]
            log.warning("%s", warning)

        try:
            result = publisher.publish(
                default_branch=self.repo.convert.default_branch,
                lfs_enabled=self.repo.convert.lfs.enabled,
                branches=[m.name for m in export.branches],
                tags=[m.name for m in export.tags],
                force=force,
                allow_existing=previously_pushed,
                on_progress=self._progress,
            )
        finally:
            # Never leave a token-bearing remote behind in the repository config.
            publisher.clear_remote()

        outcome.push = result
        self.state.update_sync_state(
            self.repo.name, pushed_head=export.head_sha, last_push_at=time.time())
        return result.to_dict()

    def _stage_verify(self, outcome: MigrationOutcome) -> Dict[str, Any]:
        report = self.verify(on_progress=self._progress)
        outcome.verification = report
        if not report.ok and self.repo.verify.fail_on_mismatch:
            raise VerificationError(
                f"verification found {report.total_differences} difference(s) across "
                f"{sum(1 for b in report.branches if not b.ok)} branch(es)",
                "Open the HTML report in the reports directory for the per-file detail. "
                "Set `verify.fail_on_mismatch: false` to record differences without failing "
                "the run.",
            )
        return report.to_dict()

    # ------------------------------------------------------- public helpers --

    def svn_head_revision(self) -> int:
        return self.svn.head_revision(self.source_url())

    def refresh_authors(self, since_revision: int = 0) -> List[str]:
        """Pick up authors who have committed since the last sync.

        Without this, the first commit by a new joiner aborts every subsequent sync:
        git-svn refuses to convert a revision whose author is not in the map, and the
        operator has to notice, run `authors --write`, and re-run by hand.
        """
        url = self.source_url()
        head = self.svn_head_revision()
        start = max(1, since_revision + 1)
        if start > head:
            return []

        seen = {entry.author for entry in self.svn.iter_log(url, start=start, end=head)}
        existing = AuthorMap.load(self.repo.authors_file)
        new_authors = sorted(a for a in seen if a and a not in existing)
        if not new_authors:
            return []

        log.info("%d new author(s) since r%d: %s", len(new_authors), since_revision,
                 ", ".join(new_authors[:10]))
        if self.repo.authors.policy == AuthorPolicy.STRICT:
            raise ConfigError(
                f"new SVN author(s) since the last sync are not mapped: "
                f"{', '.join(new_authors)}",
                f"Add them to {self.repo.authors_file}, then run the sync again. "
                "Set `authors.policy: generate` to have addresses synthesised instead.",
            )

        amap, synthesised = build_author_map(sorted(seen), self.repo.authors, existing)
        amap.save(self.repo.authors_file, header=list(AUTHORS_HEADER))
        amap.write_git_svn_file(self.repo.workdir / "authors-git-svn.txt")
        self._authors = amap
        return new_authors

    def fetch_revisions(self, on_progress: Optional[Callable[[float, str], None]] = None) -> FetchResult:
        """Fetch new revisions into the mirror (used by incremental sync)."""
        mirror = self.mirror
        mirror.authors_file = self.repo.workdir / "authors-git-svn.txt"
        mirror.init()
        self.refresh_authors(mirror.last_fetched_revision())
        head = self.svn_head_revision()
        return mirror.fetch(
            revision_start=self.repo.source.revision_start,
            revision_end=self.repo.source.revision_end,
            head_revision=head,
            on_progress=on_progress,
        )

    def build_export(self, on_progress: Optional[Callable[[float, str], None]] = None) -> ExportResult:
        self._exporter = None  # re-derive from scratch so the result is reproducible
        return self.exporter.build(on_progress=on_progress)

    def already_published(self) -> bool:
        """True when this migration has previously pushed to the target project.

        The non-empty-project guard exists to stop us writing over somebody else's
        repository. It must not fire on our own incremental syncs, which by
        definition push into a project we filled ourselves - so the recorded push is
        what distinguishes the two.
        """
        return bool(self.state.get_sync_state(self.repo.name).get("pushed_head"))

    def push_to_gitlab(self, export: ExportResult, force: bool = False,
                       on_progress: Optional[Callable[[float, str], None]] = None) -> PushResult:
        git = Git(self.tools, self.repo.export_dir, cancel=self.cancel)
        publisher = Publisher(self.gitlab, self.repo.target, git, cancel=self.cancel)
        try:
            return publisher.publish(
                default_branch=self.repo.convert.default_branch,
                lfs_enabled=self.repo.convert.lfs.enabled,
                force=force,
                allow_existing=self.already_published(),
                on_progress=on_progress,
            )
        finally:
            publisher.clear_remote()

    def verify(self, on_progress: Optional[Callable[[float, str], None]] = None) -> VerificationReport:
        config = self.repo.verify
        report = VerificationReport(repository=self.repo.name, mode=config.mode.value,
                                    started_at=time.time())
        started = time.time()

        if config.mode == VerifyMode.OFF:
            report.notes.append("verification is disabled in the configuration")
            return report

        git = Git(self.tools, self.repo.export_dir, cancel=self.cancel)
        mirror = self.mirror
        analysis = self.analysis

        report.svn_head_revision = analysis.head_revision
        report.svn_revision_count = analysis.revision_count
        report.git_commit_count = git.commit_count("--all")

        # Revision coverage: how many SVN revisions produced a Git commit. Revisions
        # that touched only paths outside the migrated subtree legitimately produce
        # none, so this is reported, not asserted.
        mirror_commits = Git(self.tools, self.repo.mirror_dir, cancel=self.cancel)
        report.git_commits_with_revision = mirror_commits.commit_count("--all")
        if report.svn_revision_count:
            report.revision_coverage = min(
                1.0, report.git_commits_with_revision / report.svn_revision_count)

        # Tags
        git_tags = {r.name[len("refs/tags/"):] for r in git.list_refs("refs/tags/")}
        svn_tags = set(analysis.tag_names)
        report.tag_count_git = len(git_tags)
        report.tag_count_svn = len(svn_tags)
        if svn_tags:
            from .svn.analyze import sanitize_ref_name
            expected = {sanitize_ref_name(f"{self.repo.convert.tag_prefix}{t}")
                        for t in svn_tags}
            report.missing_tags = sorted(expected - git_tags)

        # Authors
        report.authors_total = len(analysis.authors)
        amap = self._authors or AuthorMap.load(self.repo.authors_file)
        report.authors_mapped = sum(1 for a in analysis.authors if a in amap or not a)
        if config.mode == VerifyMode.FULL and not self.dry_run:
            try:
                emails = sorted({identity.email for _u, identity in amap})
                matches = self.gitlab.match_authors(emails)
                report.authors_matched_in_gitlab = sum(1 for v in matches.values() if v)
                report.unmatched_author_emails = sorted(e for e, v in matches.items() if not v)
            except Exception as exc:
                report.notes.append(f"GitLab user matching was skipped: {exc}")

        if config.mode == VerifyMode.COUNTS:
            report.duration = time.time() - started
            return report

        # Content verification, branch by branch.
        branches = self._branches_to_verify(git, config)
        verifier = Verifier(
            git=git, svn=self.svn, svn_base_url=self.convert_url(), cancel=self.cancel,
            normalize_eol=config.normalize_eol, max_reported=config.max_reported_diffs,
            temp_root=self.repo.workdir,
        )
        for index, (branch, svn_path) in enumerate(branches, 1):
            self._check_cancel()
            if on_progress:
                on_progress((index - 1) / max(1, len(branches)),
                            f"verifying {branch} ({index} of {len(branches)})")
            revision = mirror.revision_of(self._mirror_ref_for(branch, svn_path)) or None
            record = verifier.verify_branch(
                branch=branch, git_ref=f"refs/heads/{branch}", svn_path=svn_path,
                svn_revision=revision,
                on_progress=lambda f, m, i=index: on_progress and on_progress(
                    (i - 1 + f) / max(1, len(branches)), m),
            )
            report.branches.append(record)

        report.duration = time.time() - started
        if on_progress:
            on_progress(1.0, "verification complete" if report.ok
                        else f"{report.total_differences} difference(s) found")
        return report

    def _branches_to_verify(self, git: Git, config) -> List[tuple]:
        """(git branch, svn path) pairs to compare."""
        default = self.repo.convert.default_branch
        layout = self.analysis.layout
        trunk_path = layout.trunk if layout.trunk and layout.trunk != "." else ""
        pairs: List[tuple] = [(default, trunk_path)]

        if config.verify_all_branches or config.branches:
            wanted = set(config.branches) if config.branches else None
            branch_root = layout.branches[0].rstrip("/*") if layout.branches else "branches"
            for ref in git.list_refs("refs/heads/"):
                name = ref.name[len("refs/heads/"):]
                if name == default:
                    continue
                if wanted is not None and name not in wanted:
                    continue
                prefix = self.repo.convert.branch_prefix
                svn_name = name[len(prefix):] if prefix and name.startswith(prefix) else name
                pairs.append((name, f"{branch_root}/{svn_name}"))
        return pairs

    def _mirror_ref_for(self, branch: str, svn_path: str) -> str:
        from .convert.gitsvn import SVN_PREFIX
        if svn_path:
            return f"refs/remotes/{SVN_PREFIX}{svn_path}"
        return self.mirror.trunk_ref

    # ------------------------------------------------------------- reports --

    def _write_reports(self, outcome: MigrationOutcome) -> Dict[str, Path]:
        manifest = MigrationManifest(repository=self.repo.name)
        manifest.source = {
            "url": self.repo.source.url or "",
            "local_path": self.repo.source.local_path or "",
            "subpath": self.repo.source.subpath or "",
        }
        manifest.target = {
            "gitlab_url": self.repo.target.gitlab_url,
            "project": self.repo.target.full_path(),
            "visibility": self.repo.target.visibility.value,
        }
        if self.job_id:
            stored = {s.name: s for s in self.state.list_stages(self.job_id)}
            manifest.analysis = (stored["analyze"].result or {}) if "analyze" in stored else {}
            manifest.acquisition = (stored["acquire"].result or {}) if "acquire" in stored else {}
            manifest.conversion = (stored["convert"].result or {}) if "convert" in stored else {}
            manifest.export = (stored["export"].result or {}) if "export" in stored else {}
            manifest.push = (stored["push"].result or {}) if "push" in stored else {}
            manifest.verification = (stored["verify"].result or {}) if "verify" in stored else {}
            manifest.stages = [
                {"name": s.name, "status": s.status.value, "duration": s.duration,
                 "message": s.message, "error": s.error}
                for s in self.state.list_stages(self.job_id)
            ]
        else:
            manifest.analysis = self.analysis.to_dict()
            manifest.export = outcome.export.to_dict() if outcome.export else {}
            manifest.push = outcome.push.to_dict() if outcome.push else {}
            manifest.verification = outcome.verification.to_dict() if outcome.verification else {}

        paths = write_reports(manifest, self.repo.reports_dir)
        if self.job_id:
            for kind, path in paths.items():
                self.state.add_artifact(self.job_id, f"report-{kind}", str(path))
        return paths


# --------------------------------------------------------------------------- #

def build_pipelines(
    config: MigrationConfig,
    state: StateStore,
    only: Optional[List[str]] = None,
    cancel: Optional[Event] = None,
    on_progress: Optional[ProgressFn] = None,
    dry_run: bool = False,
    create_jobs: bool = True,
    resume: bool = True,
) -> List[MigrationPipeline]:
    tools = detect_tools(config.tool_dirs)
    pipelines: List[MigrationPipeline] = []
    for repo in config.all_resolved(only):
        job_id = ""
        if create_jobs:
            # Resuming attaches to the most recent job whatever its outcome, so stages
            # that already succeeded are not repeated. A repository that took nine
            # hours to convert must never be converted again just because the operator
            # re-ran the command. `--no-resume` (resume=False) starts a fresh job, and
            # `--restart-from` invalidates a specific stage onwards.
            latest = state.latest_job(repo.name) if resume else None
            if latest:
                job_id = latest["id"]
                log.info("resuming job %s for %s (previous status: %s)",
                         job_id, repo.name, latest["status"])
            else:
                job_id = state.create_job(
                    name=config.name, repo=repo.name,
                    config_path=str(config._source_path or ""),
                    workdir=str(repo.workdir))
        pipelines.append(MigrationPipeline(
            repo=repo, state=state, tools=tools, cancel=cancel,
            on_progress=on_progress, job_id=job_id, dry_run=dry_run,
        ))
    return pipelines


def _analysis_from_dict(data: Dict[str, Any]) -> RepoAnalysis:
    from .config import LayoutMode
    from .svn.analyze import DetectedLayout, Finding, LargeFile

    analysis = RepoAnalysis()
    for key in ("url", "repository_root", "repository_uuid", "head_revision", "first_revision",
                "revision_count", "authors", "unmapped_authors", "first_commit", "last_commit",
                "branch_names", "tag_names", "head_file_count", "head_size_bytes",
                "extension_sizes", "lfs_candidate_count", "lfs_candidate_bytes", "externals",
                "analysed_at"):
        if key in data:
            setattr(analysis, key, data[key])
    layout = data.get("layout") or {}
    analysis.layout = DetectedLayout(
        mode=LayoutMode(layout.get("mode", "flat")),
        trunk=layout.get("trunk"),
        branches=list(layout.get("branches") or []),
        tags=list(layout.get("tags") or []),
        projects=list(layout.get("projects") or []),
        nested_branches=bool(layout.get("nested_branches")),
        confidence=layout.get("confidence", "low"),
        rationale=list(layout.get("rationale") or []),
    )
    analysis.largest_files = [LargeFile(**f) for f in (data.get("largest_files") or [])]
    analysis.findings = [Finding(**f) for f in (data.get("findings") or [])]
    return analysis


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"
