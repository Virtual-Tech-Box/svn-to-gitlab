"""Incremental synchronisation between the initial migration and the cutover.

The realistic migration timeline is: convert once (hours or days), let people keep
working in Subversion, re-sync repeatedly, then cut over during a short window. This
module owns that middle phase.

Each round is: fetch new revisions into the mirror, re-derive the export, push. The
derivation is deterministic, so an unchanged history re-derives to identical commit
SHAs and the push is a fast-forward. When a *history-rewriting* option is enabled
(LFS or metadata stripping) and the underlying history changes shape, the push may
need to be forced - which is safe only because nobody should be committing to the
GitLab side until cutover. We say so loudly rather than forcing silently.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from ..errors import AbortedError, Svn2GitlabError
from ..logging_setup import get_logger
from ..state import StateStore

if TYPE_CHECKING:  # pragma: no cover
    from ..pipeline import MigrationPipeline

log = get_logger("sync")


@dataclass
class SyncResult:
    repository: str = ""
    started_at: float = 0.0
    duration: float = 0.0
    previous_revision: int = 0
    current_revision: int = 0
    new_revisions: int = 0
    pushed: bool = False
    forced: bool = False
    head_sha: str = ""
    up_to_date: bool = False
    error: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict:
        return {
            "repository": self.repository,
            "started_at": self.started_at,
            "duration": round(self.duration, 1),
            "previous_revision": self.previous_revision,
            "current_revision": self.current_revision,
            "new_revisions": self.new_revisions,
            "pushed": self.pushed,
            "forced": self.forced,
            "head_sha": self.head_sha,
            "up_to_date": self.up_to_date,
            "ok": self.ok,
            "error": self.error,
            "warnings": self.warnings,
        }


class SyncRunner:
    """Runs sync rounds for one repository, either once or on a schedule."""

    def __init__(
        self,
        pipeline: "MigrationPipeline",
        state: StateStore,
        cancel: Optional[Event] = None,
    ) -> None:
        self.pipeline = pipeline
        self.state = state
        self.cancel = cancel or Event()
        self.repo = pipeline.repo

    # -- one round -----------------------------------------------------------

    def run_once(
        self,
        push: bool = True,
        force_push: bool = False,
        on_progress: Optional[Callable[[float, str], None]] = None,
    ) -> SyncResult:
        started = time.time()
        result = SyncResult(repository=self.repo.name, started_at=started)
        stored = self.state.get_sync_state(self.repo.name)
        result.previous_revision = int(stored.get("last_svn_rev") or 0)

        def step(fraction: float, message: str) -> None:
            if on_progress:
                on_progress(fraction, message)

        try:
            step(0.05, "checking Subversion for new revisions")
            head = self.pipeline.svn_head_revision()
            result.current_revision = head

            if head and head <= result.previous_revision and not force_push:
                result.up_to_date = True
                result.duration = time.time() - started
                self.state.update_sync_state(
                    self.repo.name, last_sync_at=time.time(), consecutive_failures=0,
                    note=f"up to date at r{head}")
                log.info("%s: already up to date at r%d", self.repo.name, head)
                step(1.0, f"already up to date at r{head:,}")
                return result

            step(0.15, f"fetching revisions up to r{head:,}")
            fetch = self.pipeline.fetch_revisions(
                on_progress=lambda f, m: step(0.15 + 0.45 * f, m))
            result.current_revision = max(result.current_revision, fetch.last_revision)
            result.new_revisions = fetch.revisions_fetched

            if not result.new_revisions and not force_push and result.previous_revision:
                result.up_to_date = True
                result.duration = time.time() - started
                self.state.update_sync_state(
                    self.repo.name, last_svn_rev=result.current_revision,
                    last_sync_at=time.time(), consecutive_failures=0)
                step(1.0, "no new revisions")
                return result

            step(0.62, "re-deriving the publishable history")
            export = self.pipeline.build_export(
                on_progress=lambda f, m: step(0.62 + 0.20 * f, m))
            result.head_sha = export.head_sha

            if export.history_rewritten and result.previous_revision:
                result.warnings.append(
                    "this repository uses history rewriting (LFS or metadata stripping), so a "
                    "re-derived export can require a force push. Do not commit to the GitLab "
                    "project until after cutover."
                )

            if push:
                step(0.85, "pushing to GitLab")
                needs_force = force_push or (
                    export.history_rewritten and stored.get("pushed_head")
                    and stored.get("pushed_head") != export.head_sha
                )
                push_result = self.pipeline.push_to_gitlab(
                    export, force=bool(needs_force),
                    on_progress=lambda f, m: step(0.85 + 0.13 * f, m))
                result.pushed = True
                result.forced = bool(needs_force)
                self.state.update_sync_state(
                    self.repo.name, last_push_at=time.time(), pushed_head=export.head_sha)
                log.info("%s: pushed r%d as %s", self.repo.name, result.current_revision,
                         (push_result.project_path or ""))

            self.state.update_sync_state(
                self.repo.name,
                last_svn_rev=result.current_revision,
                last_sync_at=time.time(),
                consecutive_failures=0,
                note=f"synced {result.new_revisions} revision(s) to r{result.current_revision}",
            )
            step(1.0, f"synced through r{result.current_revision:,}")

        except AbortedError:
            raise
        except Svn2GitlabError as exc:
            result.error = str(exc)
            failures = int(stored.get("consecutive_failures") or 0) + 1
            self.state.update_sync_state(self.repo.name, consecutive_failures=failures,
                                         last_sync_at=time.time(), note=result.error[:500])
            log.error("%s: sync failed (%d consecutive): %s", self.repo.name, failures, exc)
        except Exception as exc:  # noqa: BLE001 - a scheduled sync must not die silently
            result.error = f"unexpected error: {exc}"
            failures = int(stored.get("consecutive_failures") or 0) + 1
            self.state.update_sync_state(self.repo.name, consecutive_failures=failures,
                                         last_sync_at=time.time(), note=result.error[:500])
            log.exception("%s: sync failed unexpectedly", self.repo.name)

        result.duration = time.time() - started
        return result

    # -- scheduled loop ------------------------------------------------------

    def run_forever(
        self,
        interval_minutes: int,
        max_rounds: int = 0,
        on_round: Optional[Callable[[SyncResult], None]] = None,
    ) -> List[SyncResult]:
        """Sync on an interval until cancelled.

        Used by `svn2gitlab sync --watch` and by the web UI. For unattended operation
        on a server, prefer registering a scheduled task that calls `sync --once`;
        a supervised OS scheduler survives reboots and this loop does not.
        """
        results: List[SyncResult] = []
        interval = max(60, interval_minutes * 60)
        rounds = 0
        backoff = 1

        while not self.cancel.is_set():
            rounds += 1
            result = self.run_once()
            results.append(result)
            if on_round:
                try:
                    on_round(result)
                except Exception:
                    log.debug("sync round callback raised", exc_info=True)

            if result.ok:
                backoff = 1
            else:
                backoff = min(8, backoff * 2)
                log.warning("%s: backing off to %dx the interval after a failure",
                            self.repo.name, backoff)

            if max_rounds and rounds >= max_rounds:
                break
            wait = interval * backoff
            log.info("%s: next sync in %d minute(s)", self.repo.name, wait // 60)
            if self.cancel.wait(wait):
                break

        return results

    # -- cutover -------------------------------------------------------------

    def cutover(
        self,
        lock_svn: bool = True,
        lock_message: str = "",
        exempt_users: Optional[List[str]] = None,
        verify: bool = True,
        dry_run: bool = False,
        on_progress: Optional[Callable[[float, str], None]] = None,
    ) -> Dict[str, object]:
        """Final sync, lock Subversion, verify, and record the handover.

        Order matters. We lock *before* the final sync so that no commit can slip in
        between the last fetch and the lock - the alternative loses data silently.
        """
        from .cutover import lock_repository  # local import keeps the module graph acyclic

        def step(fraction: float, message: str) -> None:
            if on_progress:
                on_progress(fraction, message)

        outcome: Dict[str, object] = {"repository": self.repo.name, "dry_run": dry_run}

        lock_result = None
        is_tfvc = getattr(self.pipeline, "is_tfvc", False)
        if lock_svn and is_tfvc:
            # TFVC has no repository-level hook to install. Freezing a team project
            # is a permission change in TFS, which this tool deliberately does not
            # make on the customer's behalf.
            outcome["lock"] = {
                "locked": False,
                "warnings": ["TFVC cannot be locked from here. Before announcing the "
                             "cutover, deny the 'Check in' permission on the team "
                             "project in TFS, then re-run the final sync."],
            }
            log.warning("cutover without a source lock - TFVC is frozen in TFS, not here")
        elif lock_svn:
            step(0.05, "making the Subversion repository read-only")
            local_path = self.repo.source.local_path
            if not local_path:
                outcome["lock"] = {
                    "locked": False,
                    "warnings": ["Subversion could not be locked: no `source.local_path` is "
                                 "configured, so this host has no filesystem access to the "
                                 "repository. Lock it manually on the SVN server before "
                                 "announcing the cutover."],
                }
                log.warning("cutover without an SVN lock - remote source")
            else:
                lock_result = lock_repository(
                    Path(local_path),
                    lock_message or self.repo.sync.lock_message,
                    exempt_users or self.repo.sync.lock_exempt_users,
                    dry_run=dry_run,
                )
                outcome["lock"] = lock_result.to_dict()

        step(0.20, "final synchronisation")
        final = self.run_once(push=not dry_run,
                              on_progress=lambda f, m: step(0.20 + 0.45 * f, m))
        outcome["final_sync"] = final.to_dict()
        if not final.ok:
            outcome["status"] = "failed"
            outcome["error"] = final.error
            return outcome

        if verify and not dry_run:
            step(0.70, "verifying the migrated content")
            report = self.pipeline.verify(on_progress=lambda f, m: step(0.70 + 0.25 * f, m))
            outcome["verification"] = report.to_dict()
            if not report.ok:
                outcome["status"] = "verification-failed"
                return outcome

        if not dry_run:
            self.state.update_sync_state(self.repo.name, cutover_at=time.time(),
                                         note="cutover complete")
        outcome["status"] = "complete"
        step(1.0, "cutover complete")
        log.info("%s: cutover complete at r%d", self.repo.name, final.current_revision)
        return outcome
