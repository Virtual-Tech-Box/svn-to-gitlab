"""Background job execution for the web dashboard.

Migrations are long-running and must survive the operator closing the browser tab,
so each one runs on its own thread with its own cancel token, and all progress goes
through the same SQLite state store the CLI uses. Refreshing the page (or attaching
from a different browser) shows the real state, not a client-side guess.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config import MigrationConfig
from ..errors import Svn2GitlabError
from ..logging_setup import get_logger
from ..pipeline import MigrationPipeline, build_pipelines
from ..state import JobStatus, StateStore

log = get_logger("web.jobs")


@dataclass
class RunningJob:
    job_id: str
    repository: str
    kind: str                     # migrate | sync | cutover | analyze
    thread: threading.Thread
    cancel: threading.Event
    started_at: float = field(default_factory=time.time)
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    finished: bool = False
    error: str = ""
    result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "repository": self.repository,
            "kind": self.kind,
            "started_at": self.started_at,
            "elapsed": time.time() - self.started_at,
            "stage": self.stage,
            "progress": self.progress,
            "message": self.message,
            "finished": self.finished,
            "error": self.error,
            "alive": self.thread.is_alive(),
        }


class JobManager:
    """Owns the running threads. One per repository at a time."""

    def __init__(self, state: StateStore) -> None:
        self.state = state
        self._jobs: Dict[str, RunningJob] = {}
        self._lock = threading.Lock()

    # -- inspection ----------------------------------------------------------

    def running(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [job.to_dict() for job in self._jobs.values() if not job.finished]

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [job.to_dict() for job in self._jobs.values()]

    def get(self, repository: str) -> Optional[RunningJob]:
        with self._lock:
            for job in self._jobs.values():
                if job.repository == repository and not job.finished:
                    return job
        return None

    def is_busy(self, repository: str) -> bool:
        return self.get(repository) is not None

    # -- control -------------------------------------------------------------

    def cancel(self, repository: str) -> bool:
        job = self.get(repository)
        if not job:
            return False
        job.cancel.set()
        log.info("cancellation requested for %s", repository)
        return True

    def cancel_all(self) -> int:
        count = 0
        with self._lock:
            for job in self._jobs.values():
                if not job.finished:
                    job.cancel.set()
                    count += 1
        return count

    def prune(self, max_age: float = 3600) -> None:
        cutoff = time.time() - max_age
        with self._lock:
            for key in [k for k, j in self._jobs.items()
                        if j.finished and j.started_at < cutoff]:
                self._jobs.pop(key, None)

    # -- launching -----------------------------------------------------------

    def start(
        self,
        config: MigrationConfig,
        repository: str,
        kind: str = "migrate",
        options: Optional[Dict[str, Any]] = None,
    ) -> RunningJob:
        if self.is_busy(repository):
            raise Svn2GitlabError(f"{repository} already has a job running",
                                  "Wait for it to finish, or stop it first.")

        options = options or {}
        cancel = threading.Event()
        holder: Dict[str, RunningJob] = {}

        def progress(stage: str, fraction: float, message: str) -> None:
            job = holder.get("job")
            if job:
                job.stage = stage
                job.progress = fraction
                job.message = message

        def target() -> None:
            job = holder["job"]
            try:
                pipelines = build_pipelines(
                    config, self.state, only=[repository], cancel=cancel,
                    on_progress=progress, dry_run=bool(options.get("dry_run")),
                    create_jobs=kind in ("migrate",),
                    resume=not options.get("no_resume"),
                )
                if not pipelines:
                    raise Svn2GitlabError(f"no such repository: {repository}")
                pipeline = pipelines[0]
                job.job_id = pipeline.job_id or job.job_id
                job.result = self._dispatch(pipeline, kind, options, cancel, progress)
            except Svn2GitlabError as exc:
                job.error = str(exc)
                log.error("%s job for %s failed: %s", kind, repository, exc)
            except Exception as exc:  # noqa: BLE001
                job.error = f"unexpected error: {exc}"
                log.exception("%s job for %s failed unexpectedly", kind, repository)
            finally:
                job.finished = True
                job.progress = 1.0 if not job.error else job.progress

        thread = threading.Thread(target=target, name=f"{kind}-{repository}", daemon=True)
        job = RunningJob(job_id="", repository=repository, kind=kind,
                         thread=thread, cancel=cancel)
        holder["job"] = job
        with self._lock:
            self._jobs[f"{repository}:{time.time()}"] = job
        thread.start()
        log.info("started %s job for %s", kind, repository)
        return job

    def _dispatch(self, pipeline: MigrationPipeline, kind: str, options: Dict[str, Any],
                  cancel: threading.Event,
                  progress: Callable[[str, float, str], None]) -> Dict[str, Any]:
        from ..sync.incremental import SyncRunner

        if kind == "migrate":
            outcome = pipeline.run(
                stages=options.get("stages"),
                restart_from=options.get("restart_from", ""),
                force_push=bool(options.get("force_push")),
            )
            return outcome.to_dict()

        if kind == "analyze":
            pipeline._current_stage = "analyze"
            return pipeline._stage_analyze()

        if kind == "sync":
            runner = SyncRunner(pipeline, self.state, cancel=cancel)
            result = runner.run_once(
                push=not options.get("no_push"),
                force_push=bool(options.get("force_push")),
                on_progress=lambda f, m: progress("sync", f, m),
            )
            return result.to_dict()

        if kind == "cutover":
            runner = SyncRunner(pipeline, self.state, cancel=cancel)
            return runner.cutover(
                lock_svn=bool(options.get("lock_svn", True)),
                verify=bool(options.get("verify", True)),
                dry_run=bool(options.get("dry_run")),
                on_progress=lambda f, m: progress("cutover", f, m),
            )

        if kind == "verify":
            report = pipeline.verify(on_progress=lambda f, m: progress("verify", f, m))
            return report.to_dict()

        raise Svn2GitlabError(f"unknown job kind: {kind}")
