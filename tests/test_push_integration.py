"""End-to-end migration including a real push over Git's smart HTTP protocol.

Everything here runs for real: a Subversion repository is built and converted with
git-svn, a GitLab-shaped API creates the namespace and project, and `git push` sends
the packfile to a bare repository through `git http-backend`.

This is the step that used to be untested — the one that fails at the very end of a
long job — so the assertions are about what actually arrived on the server, not
about which functions were called.
"""

from __future__ import annotations

import subprocess

import pytest

from svn2gitlab.config import load_config
from svn2gitlab.pipeline import build_pipelines
from svn2gitlab.state import StateStore

from conftest import requires_conversion
from fake_gitlab import FakeGitLab

pytestmark = pytest.mark.slow


def git_http_backend_available(git_exe: str) -> bool:
    try:
        exec_path = subprocess.run([git_exe, "--exec-path"], capture_output=True,
                                   text=True, check=True).stdout.strip()
    except Exception:
        return False
    from pathlib import Path
    return (Path(exec_path) / "git-http-backend").is_file()


@pytest.fixture
def gitlab(tmp_path, tools):
    if not tools.git.available or not git_http_backend_available(tools.git.path):
        pytest.skip("git-http-backend is required to serve the fake GitLab")
    with FakeGitLab(tmp_path / "gitlab", tools.git.path) as server:
        yield server


@pytest.fixture
def pushable_config(tmp_path, svn_repo, gitlab):
    config = tmp_path / "push.yaml"
    config.write_text(f"""
version: 1
name: pushtest
workdir: {(tmp_path / 'work').as_posix()}
source:
  local_path: {svn_repo.as_posix()}
authors:
  file: {(tmp_path / 'authors.txt').as_posix()}
  default_domain: example.com
convert:
  default_branch: main
  strip_svn_metadata: true
target:
  gitlab_url: {gitlab.url}
  token: {gitlab.token}
  namespace: acme/legacy
  project: core
  visibility: private
  create_namespace: true
  protect_default_branch: true
verify:
  mode: full
  verify_all_branches: true
""", encoding="utf-8")
    return config


def run_migration(config_path):
    config = load_config(config_path)
    state = StateStore(config.workdir_path() / "state.db")
    pipeline = build_pipelines(config, state, dry_run=False)[0]
    return pipeline, pipeline.run()


# --------------------------------------------------------------------------- #

@requires_conversion
def test_migration_pushes_real_refs_to_the_server(pushable_config, gitlab):
    pipeline, outcome = run_migration(pushable_config)
    assert outcome.ok, outcome.error

    refs = gitlab.refs("acme/legacy/core")
    assert "refs/heads/main" in refs
    assert "refs/heads/feature-a" in refs
    assert "refs/tags/v1.0" in refs

    # The SHAs on the server must match what we converted locally, not merely exist.
    local = pipeline.repo.export_dir
    local_main = subprocess.run(["git", "-C", str(local), "rev-parse", "main"],
                                capture_output=True, text=True, check=True).stdout.strip()
    assert refs["refs/heads/main"] == local_main


@requires_conversion
def test_pushed_content_is_readable_by_a_fresh_clone(pushable_config, gitlab, tmp_path, tools):
    """The real proof: someone can clone it and get their files back."""
    _pipeline, outcome = run_migration(pushable_config)
    assert outcome.ok, outcome.error

    clone = tmp_path / "clone"
    result = subprocess.run(
        [tools.git.path, "clone", "-q", f"{gitlab.url}/acme/legacy/core.git", str(clone)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    assert (clone / "src" / "app.py").read_text(encoding="utf-8") == "print('hello, world')\n"
    assert (clone / "binary.dat").read_bytes() == bytes(range(256)) * 64
    assert (clone / ".gitignore").is_file()

    log = subprocess.run([tools.git.path, "-C", str(clone), "log", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    assert "Initial import" in log
    assert "git-svn-id" not in subprocess.run(
        [tools.git.path, "-C", str(clone), "log", "--format=%B"],
        capture_output=True, text=True, check=True).stdout


@requires_conversion
def test_namespace_and_project_are_created_through_the_api(pushable_config, gitlab):
    _pipeline, outcome = run_migration(pushable_config)
    assert outcome.ok

    # Nested group path created one level at a time.
    assert "acme" in gitlab.state.namespaces
    assert "acme/legacy" in gitlab.state.namespaces
    assert "acme/legacy/core" in gitlab.state.projects
    assert gitlab.called("POST", "/api/v4/projects")


@requires_conversion
def test_default_branch_and_protection_are_applied_after_the_push(pushable_config, gitlab):
    _pipeline, outcome = run_migration(pushable_config)
    assert outcome.ok

    project = gitlab.state.projects["acme/legacy/core"]
    assert project["default_branch"] == "main"
    # Not just the API field: the repository's HEAD must point at it, or every clone
    # of the migrated project arrives with an empty working tree.
    assert gitlab.state.head("acme/legacy/core", gitlab.git_exe) == "refs/heads/main"

    protections = gitlab.state.protected.get(project["id"], [])
    assert any(p.get("name") == "main" for p in protections), protections

    # Protection must not be applied before the refs land, or it blocks our own push.
    order = [f"{m} {p}" for m, p in gitlab.state.requests]
    push_index = next(i for i, entry in enumerate(order) if "git-receive-pack" in entry)
    protect_index = next(i for i, entry in enumerate(order)
                         if entry.startswith("POST") and "protected_branches" in entry)
    assert push_index < protect_index, "branch protection was applied before the push"


@requires_conversion
def test_the_token_is_sent_and_never_leaks_into_the_repo_config(pushable_config, gitlab):
    _pipeline, outcome = run_migration(pushable_config)
    assert outcome.ok
    assert gitlab.state.auth_failures == 0, "an API call was made without the token"

    # The credential-bearing remote must be gone once the push is done.
    export = _pipeline.repo.export_dir
    config_text = (export / ".git" / "config").read_text(encoding="utf-8")
    assert gitlab.token not in config_text
    assert "gitlab" not in config_text.lower() or "url" not in config_text.lower().split("gitlab")[-1][:80]


@requires_conversion
def test_verification_runs_against_the_pushed_result(pushable_config):
    _pipeline, outcome = run_migration(pushable_config)
    assert outcome.ok
    assert outcome.verification is not None
    assert outcome.verification.ok, [
        d.to_dict() for b in outcome.verification.branches for d in b.differences]
    assert outcome.push is not None
    assert outcome.push.project_path == "acme/legacy/core"
    assert outcome.push.branches_pushed >= 1


@requires_conversion
def test_second_migration_into_a_non_empty_project_is_refused(pushable_config, gitlab):
    """Guards against clobbering a project that already holds history."""
    from svn2gitlab.errors import GitLabError
    from svn2gitlab.gitlab.api import GitLabClient

    _pipeline, outcome = run_migration(pushable_config)
    assert outcome.ok

    config = load_config(pushable_config)
    target = config.all_resolved()[0].target
    client = GitLabClient(target)
    with pytest.raises(GitLabError, match="already contains commits"):
        client.ensure_project(target, "main")

    target.allow_non_empty_project = True
    assert client.ensure_project(target, "main").path_with_namespace == "acme/legacy/core"


@requires_conversion
def test_incremental_sync_pushes_new_revisions_to_the_server(pushable_config, gitlab,
                                                             svn_repo, tools):
    from svn2gitlab.sync.incremental import SyncRunner

    pipeline, outcome = run_migration(pushable_config)
    assert outcome.ok
    before = gitlab.refs("acme/legacy/core")["refs/heads/main"]

    work = svn_repo.parent / "wc"
    (work / "AFTER.txt").write_text("added after the migration\n", encoding="utf-8")
    subprocess.run([tools.svn.path, "add", "-q", "AFTER.txt"], cwd=work, check=True)
    subprocess.run([tools.svn.path, "commit", "-q", "-m", "Post-migration commit"],
                   cwd=work, check=True)

    config = load_config(pushable_config)
    state = StateStore(config.workdir_path() / "state.db")
    result = SyncRunner(pipeline, state).run_once(push=True)
    assert result.ok, result.error
    assert result.new_revisions >= 1

    after = gitlab.refs("acme/legacy/core")["refs/heads/main"]
    assert after != before, "the sync did not update the server"
