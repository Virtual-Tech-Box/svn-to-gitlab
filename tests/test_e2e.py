"""End-to-end conversion against a real Subversion repository.

These are the tests that matter most: they convert an actual repository with git-svn
and assert on the resulting Git objects. Everything up to (but not including) the
GitLab push is exercised, since a push needs a live instance.
"""

from __future__ import annotations

import subprocess
import threading

import pytest

from svn2gitlab.config import load_config
from svn2gitlab.pipeline import MigrationPipeline, build_pipelines
from svn2gitlab.state import StateStore
from svn2gitlab.sync.cutover import is_locked, lock_repository, unlock_repository

from conftest import requires_conversion, requires_svn

pytestmark = pytest.mark.slow


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def run_migration(config_path, **kwargs):
    config = load_config(config_path)
    state = StateStore(config.workdir_path() / "state.db")
    pipelines = build_pipelines(config, state, dry_run=True, **kwargs)
    outcome = pipelines[0].run()
    return pipelines[0], outcome


@requires_conversion
def test_full_conversion_produces_the_expected_git_repository(migration_config):
    pipeline, outcome = run_migration(migration_config)
    assert outcome.ok, outcome.error

    export = pipeline.repo.export_dir
    branches = git(export, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split()
    tags = git(export, "tag", "-l").split()

    assert "main" in branches
    assert "feature-a" in branches
    assert "v1.0" in tags

    # trunk became the default branch and carries the real history
    subjects = git(export, "log", "--format=%s", "main").splitlines()
    assert "Add the licence" in subjects
    assert "Initial import" in subjects

    # Content is present, including the binary file
    tracked = git(export, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert "src/app.py" in tracked
    assert "binary.dat" in tracked


@requires_conversion
def test_authors_are_mapped_not_left_as_raw_usernames(migration_config):
    pipeline, outcome = run_migration(migration_config)
    assert outcome.ok

    authors = set(git(pipeline.repo.export_dir, "log", "--all", "--format=%an <%ae>").splitlines())
    emails = {a.split("<")[1].rstrip(">") for a in authors if "<" in a}
    assert any(e.endswith("@example.com") for e in emails)
    # The domain-qualified account was normalised, not carried through verbatim.
    assert not any("CONTOSO" in e for e in emails)
    assert "carol@example.com" in emails


@requires_conversion
def test_svn_metadata_is_stripped_and_revisions_stay_traceable(migration_config):
    pipeline, outcome = run_migration(migration_config)
    assert outcome.ok

    messages = git(pipeline.repo.export_dir, "log", "--all", "--format=%B")
    assert "git-svn-id" not in messages
    assert "Svn-Revision:" in messages


@requires_conversion
def test_gitignore_is_generated_from_svn_ignore(migration_config):
    pipeline, outcome = run_migration(migration_config)
    assert outcome.ok
    content = git(pipeline.repo.export_dir, "show", "main:.gitignore")
    assert "*.pyc" in content
    assert "build/" in content


@requires_conversion
def test_verification_reports_content_as_identical(migration_config):
    pipeline, outcome = run_migration(migration_config)
    assert outcome.ok
    report = outcome.verification
    assert report is not None
    assert report.ok, [d.to_dict() for b in report.branches for d in b.differences]
    assert all(b.files_compared > 0 for b in report.branches if not b.skipped)
    # Both trunk and the branch were compared.
    assert {b.branch for b in report.branches} >= {"main", "feature-a"}


@requires_conversion
def test_verification_detects_a_real_difference(migration_config):
    """A verification that cannot fail proves nothing; make it fail on purpose."""
    pipeline, outcome = run_migration(migration_config)
    assert outcome.ok

    export = pipeline.repo.export_dir
    (export / "src" / "app.py").write_text("tampered\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(export), "commit", "-am", "tamper",
                    "--author=T <t@x.com>"],
                   capture_output=True, check=True,
                   env={"GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x.com",
                        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})

    report = pipeline.verify()
    assert not report.ok
    differing = [d.path for b in report.branches for d in b.differences]
    assert "src/app.py" in differing


@requires_conversion
def test_rerunning_resumes_instead_of_redoing_work(migration_config):
    config = load_config(migration_config)
    state = StateStore(config.workdir_path() / "state.db")

    first = build_pipelines(config, state, dry_run=True)[0]
    assert first.run().ok

    second = build_pipelines(config, state, dry_run=True, resume=True)[0]
    outcome = second.run()
    assert outcome.ok
    skipped = {s.name for s in outcome.stages if s.status == "skipped"}
    # The expensive stages must not run twice.
    assert {"analyze", "convert", "export"} <= skipped


@requires_conversion
def test_restart_from_forces_a_stage_to_run_again(migration_config):
    config = load_config(migration_config)
    state = StateStore(config.workdir_path() / "state.db")
    assert build_pipelines(config, state, dry_run=True)[0].run().ok

    pipeline = build_pipelines(config, state, dry_run=True, resume=True)[0]
    outcome = pipeline.run(restart_from="export")
    assert outcome.ok
    statuses = {s.name: s.status for s in outcome.stages}
    # export ran again from scratch...
    assert statuses["export"] == "succeeded"
    # ...and the stages before it were not touched at all.
    assert "convert" not in statuses
    assert "analyze" not in statuses


@requires_conversion
def test_incremental_sync_picks_up_new_revisions(migration_config, svn_repo, tools):
    from svn2gitlab.sync.incremental import SyncRunner

    config = load_config(migration_config)
    state = StateStore(config.workdir_path() / "state.db")
    pipeline = build_pipelines(config, state, dry_run=True)[0]
    assert pipeline.run().ok
    before = git(pipeline.repo.export_dir, "rev-list", "--count", "main")

    # Commit something new to Subversion.
    work = svn_repo.parent / "wc"
    (work / "NEWFILE.txt").write_text("added after migration\n", encoding="utf-8")
    subprocess.run([tools.svn.path, "add", "-q", "NEWFILE.txt"], cwd=work, check=True)
    subprocess.run([tools.svn.path, "commit", "-q", "-m", "Post-migration commit"],
                   cwd=work, check=True)

    runner = SyncRunner(pipeline, state)
    result = runner.run_once(push=False)
    assert result.ok, result.error
    assert result.new_revisions >= 1

    after = git(pipeline.repo.export_dir, "rev-list", "--count", "main")
    assert int(after) > int(before)
    assert "NEWFILE.txt" in git(pipeline.repo.export_dir, "ls-tree", "-r", "--name-only", "main")


@requires_conversion
def test_sync_reports_up_to_date_when_nothing_changed(migration_config):
    from svn2gitlab.sync.incremental import SyncRunner

    config = load_config(migration_config)
    state = StateStore(config.workdir_path() / "state.db")
    pipeline = build_pipelines(config, state, dry_run=True)[0]
    assert pipeline.run().ok

    runner = SyncRunner(pipeline, state)
    runner.run_once(push=False)
    second = runner.run_once(push=False)
    assert second.ok
    assert second.up_to_date or second.new_revisions == 0


@requires_conversion
def test_cancellation_stops_the_run_without_corrupting_state(migration_config):
    config = load_config(migration_config)
    state = StateStore(config.workdir_path() / "state.db")
    cancel = threading.Event()
    cancel.set()  # already cancelled: the first stage boundary must abort

    pipeline = build_pipelines(config, state, cancel=cancel, dry_run=True)[0]
    outcome = pipeline.run()
    assert outcome.status == "cancelled"

    # A fresh, uncancelled run then completes.
    pipeline2 = build_pipelines(config, state, dry_run=True, resume=True)[0]
    assert pipeline2.run().ok


# --------------------------------------------------------------------------- #
# Cutover
# --------------------------------------------------------------------------- #

@requires_svn
def test_lock_blocks_commits_and_unlock_restores_them(svn_repo, tools):
    work = svn_repo.parent / "wc"
    result = lock_repository(svn_repo, "Migrated to GitLab; this repository is read-only.")
    assert result.locked
    assert is_locked(svn_repo)

    (work / "blocked.txt").write_text("nope\n", encoding="utf-8")
    subprocess.run([tools.svn.path, "add", "-q", "blocked.txt"], cwd=work, check=True)
    attempt = subprocess.run([tools.svn.path, "commit", "-m", "should fail"],
                             cwd=work, capture_output=True, text=True)
    assert attempt.returncode != 0
    assert "read-only" in (attempt.stdout + attempt.stderr)

    undone = unlock_repository(svn_repo)
    assert not is_locked(svn_repo)
    assert "allowed again" in undone.message

    ok = subprocess.run([tools.svn.path, "commit", "-q", "-m", "allowed now"],
                        cwd=work, capture_output=True, text=True)
    assert ok.returncode == 0


@requires_svn
def test_locking_preserves_an_existing_pre_commit_hook(svn_repo):
    import os

    hooks = svn_repo / "hooks"
    existing = hooks / ("pre-commit.bat" if os.name == "nt" else "pre-commit")
    existing.write_text("#!/bin/sh\nexit 0\n" if os.name != "nt" else "@ECHO OFF\r\nEXIT /B 0\r\n",
                        encoding="ascii")
    if os.name != "nt":
        existing.chmod(0o755)

    result = lock_repository(svn_repo, "locked")
    assert result.backup_path
    assert result.warnings

    unlock_repository(svn_repo)
    assert existing.is_file()
    assert "svn2gitlab" not in existing.read_text(encoding="utf-8")


@requires_svn
def test_unlock_leaves_a_third_party_hook_alone(svn_repo):
    import os

    hooks = svn_repo / "hooks"
    hook = hooks / ("pre-commit.bat" if os.name == "nt" else "pre-commit")
    hook.write_text("#!/bin/sh\nexit 1\n" if os.name != "nt" else "@ECHO OFF\r\nEXIT /B 1\r\n",
                    encoding="ascii")
    result = unlock_repository(svn_repo)
    assert hook.is_file()
    assert result.warnings


@requires_svn
def test_locking_a_non_repository_is_rejected(tmp_path):
    from svn2gitlab.errors import SvnError

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(SvnError, match="not a Subversion repository"):
        lock_repository(plain, "nope")
