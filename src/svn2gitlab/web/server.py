"""Local web dashboard.

Deliberately small: a FastAPI app serving one page plus a JSON API, backed by the
same state store and pipeline the CLI uses. Nothing about a migration is stored in
the browser, so closing the tab, refreshing, or attaching from a second machine all
show the same truth.

Security posture: this binds to loopback by default and has **no authentication**.
It reads a configuration file that may contain credentials, and it can start
operations that write to Subversion and GitLab. Do not expose it on a shared
interface; the CLI warns when you try.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from .. import __version__
from ..config import MigrationConfig, dump_config, load_config
from ..errors import Svn2GitlabError
from ..logging_setup import RING, get_logger, setup_logging
from ..state import StateStore
from ..tools import detect_tools, human_bytes
from .jobs import JobManager

log = get_logger("web")

TEMPLATE = Path(__file__).parent / "templates" / "dashboard.html"


class AppState:
    """Everything the request handlers share."""

    def __init__(self, config_path: Optional[Path]) -> None:
        self.config_path = Path(config_path) if config_path else None
        self.config: Optional[MigrationConfig] = None
        self.state: Optional[StateStore] = None
        self.jobs: Optional[JobManager] = None
        self.load_error = ""
        if self.config_path:
            self.reload()

    def reload(self) -> None:
        if not self.config_path:
            return
        try:
            self.config = load_config(self.config_path)
            self.state = StateStore(self.config.workdir_path() / "svn2gitlab.db")
            self.jobs = JobManager(self.state)
            self.load_error = ""
            log.info("loaded configuration %s (%d repositories)", self.config_path,
                     len(self.config.repositories))
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.config = None
            self.load_error = str(exc)
            log.error("could not load %s: %s", self.config_path, exc)

    def require(self) -> MigrationConfig:
        if not self.config:
            raise HTTPException(status_code=400,
                                detail=self.load_error or "no configuration is loaded")
        return self.config

    def require_jobs(self) -> JobManager:
        self.require()
        assert self.jobs is not None
        return self.jobs

    def require_state(self) -> StateStore:
        self.require()
        assert self.state is not None
        return self.state


def create_app(config_path: Optional[Path] = None) -> FastAPI:
    app = FastAPI(title="svn2gitlab", version=__version__, docs_url=None, redoc_url=None)
    app_state = AppState(config_path)
    app.state.svn2gitlab = app_state

    # -- page ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return TEMPLATE.read_text(encoding="utf-8")

    # -- meta ---------------------------------------------------------------

    @app.get("/api/meta")
    def meta() -> Dict[str, Any]:
        tools = detect_tools()
        return {
            "version": __version__,
            "config_path": str(app_state.config_path or ""),
            "config_loaded": app_state.config is not None,
            "config_error": app_state.load_error,
            "name": app_state.config.name if app_state.config else "",
            "workdir": str(app_state.config.workdir_path()) if app_state.config else "",
            "tools": tools.to_dict(),
            "tools_ready": tools.git.available and tools.git_svn.available and tools.svn.available,
        }

    @app.post("/api/reload")
    def reload_config() -> Dict[str, Any]:
        app_state.reload()
        return {"ok": app_state.config is not None, "error": app_state.load_error}

    @app.get("/api/config", response_class=PlainTextResponse)
    def show_config() -> str:
        config = app_state.require()
        return dump_config(config, redact=True)

    # -- repositories -------------------------------------------------------

    @app.get("/api/repositories")
    def repositories() -> List[Dict[str, Any]]:
        config = app_state.require()
        state = app_state.require_state()
        jobs = app_state.require_jobs()
        out: List[Dict[str, Any]] = []
        for resolved in config.all_resolved():
            latest = state.latest_job(resolved.name)
            sync_state = state.get_sync_state(resolved.name)
            running = jobs.get(resolved.name)
            out.append({
                "name": resolved.name,
                "source": resolved.source.url or resolved.source.local_path or "",
                "source_kind": "local" if resolved.source.local_path else "remote",
                "target": resolved.target.full_path(),
                "gitlab_url": resolved.target.gitlab_url,
                "default_branch": resolved.convert.default_branch,
                "lfs": resolved.convert.lfs.enabled,
                "workdir": str(resolved.workdir),
                "job": _job_summary(state, latest) if latest else None,
                "sync": sync_state,
                "running": running.to_dict() if running else None,
                "report": _latest_report(resolved.reports_dir),
            })
        return out

    @app.get("/api/repositories/{name}/stages")
    def repository_stages(name: str) -> List[Dict[str, Any]]:
        state = app_state.require_state()
        latest = state.latest_job(name)
        if not latest:
            return []
        return [
            {"name": s.name, "status": s.status.value, "progress": s.progress,
             "duration": s.duration, "message": s.message, "error": s.error,
             "result": s.result}
            for s in state.list_stages(latest["id"])
        ]

    @app.get("/api/repositories/{name}/report", response_class=HTMLResponse)
    def repository_report(name: str) -> str:
        config = app_state.require()
        for resolved in config.all_resolved():
            if resolved.name == name:
                report = resolved.reports_dir / "report-latest.html"
                if not report.is_file():
                    raise HTTPException(status_code=404,
                                        detail="no report has been generated yet")
                return report.read_text(encoding="utf-8")
        raise HTTPException(status_code=404, detail=f"unknown repository {name}")

    # -- job control --------------------------------------------------------

    @app.post("/api/repositories/{name}/run")
    async def run_job(name: str, request: Request) -> Dict[str, Any]:
        config = app_state.require()
        jobs = app_state.require_jobs()
        try:
            body = await request.json()
        except Exception:
            body = {}
        kind = str(body.get("kind", "migrate"))
        if kind not in ("migrate", "analyze", "sync", "cutover", "verify"):
            raise HTTPException(status_code=400, detail=f"unknown action {kind}")
        options = body.get("options") or {}
        try:
            job = jobs.start(config, name, kind=kind, options=options)
        except Svn2GitlabError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job.to_dict()

    @app.post("/api/repositories/{name}/stop")
    def stop_job(name: str) -> Dict[str, Any]:
        jobs = app_state.require_jobs()
        return {"stopped": jobs.cancel(name)}

    @app.get("/api/running")
    def running() -> List[Dict[str, Any]]:
        jobs = app_state.require_jobs()
        jobs.prune()
        return jobs.all()

    @app.get("/api/jobs")
    def job_history(limit: int = 50) -> List[Dict[str, Any]]:
        state = app_state.require_state()
        return [_job_summary(state, job) for job in state.list_jobs(limit=limit)]

    # -- logs ---------------------------------------------------------------

    @app.get("/api/logs")
    def logs(since: int = 0, limit: int = 400) -> Dict[str, Any]:
        entries = RING.since(since)[-limit:]
        return {"entries": entries, "last": entries[-1]["seq"] if entries else since}

    @app.get("/api/logs/stream")
    def stream_logs() -> StreamingResponse:
        """Server-sent events, so the log pane updates without polling."""
        subscriber: queue.Queue = queue.Queue(maxsize=2000)
        RING.subscribers.append(subscriber)

        def generate():
            try:
                for entry in RING.since(0)[-100:]:
                    yield f"data: {json.dumps(entry)}\n\n"
                while True:
                    try:
                        entry = subscriber.get(timeout=15)
                        yield f"data: {json.dumps(entry)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                try:
                    RING.subscribers.remove(subscriber)
                except ValueError:
                    pass

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.exception_handler(Svn2GitlabError)
    def handle_tool_error(_request: Request, exc: Svn2GitlabError) -> JSONResponse:
        return JSONResponse(status_code=400,
                            content={"detail": exc.message, "remedy": exc.remedy})

    return app


def _job_summary(state: StateStore, job: Dict[str, Any]) -> Dict[str, Any]:
    stages = state.list_stages(job["id"])
    done = sum(1 for s in stages if s.status.value == "succeeded")
    return {
        "id": job["id"],
        "repository": job["repo"],
        "status": job["status"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "error": job["error"],
        "stages_total": len(stages),
        "stages_done": done,
        "summary": job.get("summary"),
    }


def _latest_report(reports_dir: Path) -> Optional[Dict[str, Any]]:
    report = reports_dir / "report-latest.html"
    if not report.is_file():
        return None
    manifest = reports_dir / "manifest-latest.json"
    data: Dict[str, Any] = {"html": str(report), "modified": report.stat().st_mtime}
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            verification = payload.get("verification") or {}
            data["verified"] = verification.get("ok")
            data["differences"] = verification.get("total_differences")
        except (OSError, json.JSONDecodeError):
            pass
    return data


def run_server(config_path: Optional[Path] = None, host: str = "127.0.0.1",
               port: int = 8720, open_browser: bool = True) -> None:
    import uvicorn

    app = create_app(config_path)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
