"""Publishing the export repository to GitLab.

The push is the step most likely to fail at the very end of a long job, so it is
front-loaded with checks: size against the instance's limits, LFS objects before
refs (a push referencing missing LFS objects is rejected), and branch protection
applied only *after* the history has landed - protecting the default branch first is
a classic way to make your own initial push fail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Dict, List, Optional, Sequence

from ..config import TargetConfig
from ..convert.git import Git
from ..convert.lfs import push_objects
from ..errors import GitLabError
from ..logging_setup import get_logger
from ..tools import human_bytes
from .api import GitLabClient, Project

log = get_logger("push")

REMOTE_NAME = "gitlab"
# gitlab.com rejects pushes carrying a single file above this; self-managed varies.
SAAS_FILE_LIMIT_BYTES = 100 * 1024 * 1024
SAAS_REPO_SOFT_LIMIT_BYTES = 5 * 1024 * 1024 * 1024

_REJECTED = re.compile(r"^\s*!\s+\[(remote )?rejected\]\s+(?P<ref>\S+)\s+->\s+\S+\s+\((?P<why>.+)\)",
                       re.M)


@dataclass
class PushResult:
    project_path: str = ""
    project_url: str = ""
    project_id: int = 0
    branches_pushed: int = 0
    tags_pushed: int = 0
    lfs_pushed: bool = False
    forced: bool = False
    default_branch: str = ""
    protected: bool = False
    repository_size: int = 0
    rejected: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "project_path": self.project_path,
            "project_url": self.project_url,
            "project_id": self.project_id,
            "branches_pushed": self.branches_pushed,
            "tags_pushed": self.tags_pushed,
            "lfs_pushed": self.lfs_pushed,
            "forced": self.forced,
            "default_branch": self.default_branch,
            "protected": self.protected,
            "repository_size": self.repository_size,
            "rejected": self.rejected,
        }


class Publisher:
    def __init__(
        self,
        client: GitLabClient,
        target: TargetConfig,
        git: Git,
        cancel: Optional[Event] = None,
    ) -> None:
        self.client = client
        self.target = target
        self.git = git
        self.cancel = cancel

    # -- preflight -----------------------------------------------------------

    def preflight(self, default_branch: str, lfs_enabled: bool) -> Dict[str, object]:
        """Everything that can be checked before a single byte is uploaded."""
        access = self.client.check_access()
        checks: Dict[str, object] = {"access": access, "warnings": [], "blockers": []}

        size = self.git.repo_size_bytes()
        checks["repository_size"] = size
        if size > SAAS_REPO_SOFT_LIMIT_BYTES and "gitlab.com" in self.client.base_url:
            checks["warnings"].append(  # type: ignore[union-attr]
                f"the converted repository is {human_bytes(size)}; GitLab.com's default "
                "project size limit is 5 GiB and the push will be rejected above it. "
                "Enable Git LFS for large binaries or request a limit increase."
            )

        if not lfs_enabled:
            oversized = [(path, blob_size) for _sha, blob_size, path
                         in self.git.largest_blobs(limit=20, min_bytes=SAAS_FILE_LIMIT_BYTES)]
            if oversized:
                checks["blockers"].append(  # type: ignore[union-attr]
                    f"{len(oversized)} file(s) exceed {human_bytes(SAAS_FILE_LIMIT_BYTES)} and "
                    f"LFS is disabled (largest: {oversized[0][0]}, "
                    f"{human_bytes(oversized[0][1])}). GitLab will reject the push."
                )

        if self.target.namespace:
            namespace = self.client.find_namespace(self.target.namespace)
            checks["namespace_exists"] = bool(namespace)
            if not namespace and not self.target.create_namespace:
                checks["blockers"].append(  # type: ignore[union-attr]
                    f"namespace {self.target.namespace!r} does not exist and "
                    "`target.create_namespace` is false"
                )

        existing = self.client.find_project(self.target.full_path())
        checks["project_exists"] = bool(existing)
        if existing and not existing.empty_repo and not self.target.allow_non_empty_project:
            checks["blockers"].append(  # type: ignore[union-attr]
                f"project {existing.path_with_namespace} already contains commits"
            )
        if existing and lfs_enabled and not existing.lfs_enabled:
            checks["blockers"].append(  # type: ignore[union-attr]
                f"Git LFS is disabled on project {existing.path_with_namespace}; enable it "
                "under Settings > General > Visibility, project features, permissions"
            )
        return checks

    # -- publication ---------------------------------------------------------

    def publish(
        self,
        default_branch: str,
        lfs_enabled: bool,
        branches: Sequence[str] = (),
        tags: Sequence[str] = (),
        force: bool = False,
        on_progress: Optional[Callable[[float, str], None]] = None,
    ) -> PushResult:
        def step(fraction: float, message: str) -> None:
            if on_progress:
                on_progress(fraction, message)

        step(0.02, "resolving the GitLab project")
        project = self.client.ensure_project(self.target, default_branch, lfs_enabled)
        result = PushResult(
            project_path=project.path_with_namespace,
            project_url=project.web_url,
            project_id=project.id,
            default_branch=default_branch,
            forced=force,
        )

        url = self.client.push_url(project)
        self.git.set_remote(REMOTE_NAME, url, secrets=[self.target.token])

        if lfs_enabled:
            step(0.10, "uploading Git LFS objects")
            # LFS objects must exist server-side before the refs that point at them.
            push_objects(self.git, REMOTE_NAME, cancel=self.cancel,
                         on_progress=lambda f, m: step(0.10 + 0.35 * f, m))
            result.lfs_pushed = True

        if self.target.push_branches:
            step(0.50, "pushing branches")
            result.branches_pushed = self._push_refs("refs/heads/*", force, result,
                                                     lambda f, m: step(0.50 + 0.25 * f, m))
        if self.target.push_tags:
            step(0.78, "pushing tags")
            result.tags_pushed = self._push_refs("refs/tags/*", force, result,
                                                 lambda f, m: step(0.78 + 0.10 * f, m))

        if self.target.set_default_branch:
            step(0.90, f"setting the default branch to {default_branch}")
            try:
                self.client.set_default_branch(project.id, default_branch)
                result.default_branch = default_branch
            except GitLabError as exc:
                log.warning("could not set the default branch: %s", exc.message)

        if self.target.protect_default_branch:
            step(0.94, "applying branch protection")
            try:
                self.client.protect_branch(project.id, default_branch)
                result.protected = True
            except GitLabError as exc:
                log.warning("could not protect %s: %s", default_branch, exc.message)

        step(0.98, "reading project statistics")
        stats = self.client.project_statistics(project.id)
        result.repository_size = int(stats.get("repository_size") or 0)
        step(1.0, f"published to {project.web_url}")
        log.info("published %s (%d branches, %d tags)", project.path_with_namespace,
                 result.branches_pushed, result.tags_pushed)
        return result

    def _push_refs(self, refspec: str, force: bool, result: PushResult,
                   on_progress: Optional[Callable[[float, str], None]]) -> int:
        args = ["push", "--porcelain"]
        if force:
            args.append("--force")
        args += [REMOTE_NAME, f"{refspec}:{refspec}"]

        lines: List[str] = []

        def watch(line: str) -> None:
            lines.append(line)
            if on_progress and line.strip():
                on_progress(0.5, line.strip()[:120])

        proc = self.git.run(args, check=False, timeout=None,
                            on_stdout=watch, on_stderr=watch,
                            secrets=[self.target.token])
        output = "\n".join(lines)
        if not proc.ok:
            raise self._translate_push_failure(proc.returncode, output, force)

        pushed = 0
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(("*", "+", "=", "-")) and "\t" in stripped:
                pushed += 1
            elif re.match(r"^[*+=-]\s+refs/", stripped):
                pushed += 1
        rejected = [m.group("ref") for m in _REJECTED.finditer(output)]
        result.rejected.extend(rejected)
        return pushed

    def _translate_push_failure(self, code: int, output: str, forced: bool) -> GitLabError:
        lowered = output.lower()
        if "non-fast-forward" in lowered or "fetch first" in lowered:
            return GitLabError(
                "the push was rejected as a non-fast-forward", 0, output[:1000],
                "The GitLab project already has commits that this history does not contain. "
                "If this is a re-derived export after a history rewrite, re-run with "
                "`--force-push`; otherwise migrate into an empty project.",
            )
        if "pre-receive hook declined" in lowered:
            return GitLabError(
                "a GitLab push rule rejected the history", 0, output[:1000],
                "Check the project's or group's push rules: commit-message regex, "
                "'reject unsigned commits', author-email restrictions and maximum file size "
                "all reject imported history. Disable them for the migration, then re-enable.",
            )
        if "file is too large" in lowered or "exceeds the maximum" in lowered:
            return GitLabError(
                "GitLab rejected the push because a file exceeds the size limit", 0, output[:1000],
                "Enable `convert.lfs.enabled` and re-run from the export stage so the large "
                "objects become LFS pointers.",
            )
        if "authentication failed" in lowered or "403" in lowered:
            return GitLabError(
                "git push was not authorised", 0, output[:1000],
                "The token needs the `write_repository` scope and at least Developer access "
                "(Maintainer if the branch is protected).",
            )
        if "lfs" in lowered and ("missing" in lowered or "upload" in lowered):
            return GitLabError(
                "the push references LFS objects that are not on the server", 0, output[:1000],
                "Run the export stage again so `git lfs push --all` completes before the refs "
                "are pushed, and confirm the namespace has LFS storage available.",
            )
        return GitLabError(f"git push failed (exit {code})", 0, output[:1000])

    # -- teardown ------------------------------------------------------------

    def clear_remote(self) -> None:
        """Remove the credential-bearing remote once we are done with it."""
        self.git.run(["remote", "remove", REMOTE_NAME], check=False)
