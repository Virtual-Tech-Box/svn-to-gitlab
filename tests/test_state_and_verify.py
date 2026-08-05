"""Job state persistence and the verification primitives."""

from __future__ import annotations

import hashlib
import threading

import pytest

from svn2gitlab.state import JobStatus, StageStatus, StateStore
from svn2gitlab.verify.verify import (_parse_batch, git_blob_hash, git_blob_hash_file,
                                      looks_binary, normalised_blob_hash, sha256_file)

# --------------------------------------------------------------------------- #
# State store
# --------------------------------------------------------------------------- #

@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state.db")


def test_job_lifecycle(store):
    job_id = store.create_job("mig", "repo-a", workdir="/tmp/x")
    assert store.get_job(job_id)["status"] == JobStatus.PENDING.value
    store.start_job(job_id)
    assert store.get_job(job_id)["status"] == JobStatus.RUNNING.value
    store.finish_job(job_id, JobStatus.SUCCEEDED, summary={"ok": True})
    job = store.get_job(job_id)
    assert job["status"] == JobStatus.SUCCEEDED.value
    assert job["summary"] == {"ok": True}
    assert job["finished_at"]


def test_stage_progress_and_results(store):
    job_id = store.create_job("mig", "repo-a")
    store.stage_start(job_id, "convert")
    store.stage_progress(job_id, "convert", 0.5, "halfway")
    stage = store.get_stage(job_id, "convert")
    assert stage.status == StageStatus.RUNNING
    assert stage.progress == 0.5
    assert stage.message == "halfway"

    store.stage_finish(job_id, "convert", StageStatus.SUCCEEDED, result={"revisions": 42})
    assert store.stage_succeeded(job_id, "convert")
    assert store.stage_result(job_id, "convert") == {"revisions": 42}
    assert store.get_stage(job_id, "convert").progress == 1.0


def test_progress_is_clamped(store):
    job_id = store.create_job("mig", "repo-a")
    store.stage_start(job_id, "s")
    store.stage_progress(job_id, "s", 5.0)
    assert store.get_stage(job_id, "s").progress == 1.0
    store.stage_progress(job_id, "s", -1)
    assert store.get_stage(job_id, "s").progress == 0.0


def test_restarting_a_stage_discards_it_and_everything_after(store):
    job_id = store.create_job("mig", "repo-a")
    for name in ("analyze", "convert", "export", "push"):
        store.stage_start(job_id, name)
        store.stage_finish(job_id, name, StageStatus.SUCCEEDED)

    store.reset_stages_from(job_id, "export")
    remaining = [s.name for s in store.list_stages(job_id)]
    assert remaining == ["analyze", "convert"]
    assert not store.stage_succeeded(job_id, "export")
    assert store.stage_succeeded(job_id, "convert")


def test_rerunning_a_stage_clears_its_previous_error(store):
    job_id = store.create_job("mig", "repo-a")
    store.stage_start(job_id, "convert")
    store.stage_finish(job_id, "convert", StageStatus.FAILED, error="network died")
    store.stage_start(job_id, "convert")
    stage = store.get_stage(job_id, "convert")
    assert stage.status == StageStatus.RUNNING
    assert not stage.error


def test_sync_state_defaults_and_updates(store):
    initial = store.get_sync_state("repo-a")
    assert initial["last_svn_rev"] is None
    assert initial["consecutive_failures"] == 0

    store.update_sync_state("repo-a", last_svn_rev=99, pushed_head="abc")
    updated = store.get_sync_state("repo-a")
    assert updated["last_svn_rev"] == 99
    assert updated["pushed_head"] == "abc"

    with pytest.raises(ValueError, match="unknown"):
        store.update_sync_state("repo-a", nonsense=1)


def test_latest_job_is_the_most_recent(store):
    first = store.create_job("mig", "repo-a")
    second = store.create_job("mig", "repo-a")
    assert store.latest_job("repo-a")["id"] in (first, second)
    assert store.latest_job("repo-a")["id"] == second


def test_latest_job_is_deterministic_when_timestamps_collide(store, monkeypatch):
    """Windows' clock advances in ~15 ms steps, so jobs share a created_at.

    `latest_job` decides which job a `migrate` re-run resumes, so an ambiguous
    ordering means resuming the wrong one and redoing hours of conversion.
    """
    import svn2gitlab.state as state_module

    monkeypatch.setattr(state_module.time, "time", lambda: 1000.0)
    ids = [store.create_job("mig", "repo-a") for _ in range(5)]

    assert store.latest_job("repo-a")["id"] == ids[-1]
    assert [j["id"] for j in store.list_jobs(repo="repo-a")] == list(reversed(ids))


def test_concurrent_writes_do_not_corrupt_state(store):
    """The web UI writes progress from job threads while the API reads it."""
    job_id = store.create_job("mig", "repo-a")
    errors = []

    def worker(index):
        try:
            for i in range(25):
                store.stage_start(job_id, f"stage-{index}")
                store.stage_progress(job_id, f"stage-{index}", i / 25)
                store.add_event(job_id, "INFO", f"worker {index} step {i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    assert len(store.list_stages(job_id)) == 6
    assert len(store.list_events(job_id, limit=1000)) == 150


def test_artifacts_are_recorded(store):
    job_id = store.create_job("mig", "repo-a")
    store.add_artifact(job_id, "report-html", "/tmp/report.html", meta={"size": 10})
    artifacts = store.list_artifacts(job_id)
    assert artifacts[0]["kind"] == "report-html"
    assert artifacts[0]["meta"] == {"size": 10}


# --------------------------------------------------------------------------- #
# Verification primitives
# --------------------------------------------------------------------------- #

def test_git_blob_hash_matches_gits_own_algorithm():
    """This is the whole basis of content verification, so pin it exactly."""
    content = b"hello world\n"
    expected = hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()
    assert git_blob_hash(content) == expected
    # Known-good value for "hello world\n" from `git hash-object`.
    assert git_blob_hash(content) == "3b18e512dba79e4c8300dd08aeb37f8e728b8dad"


def test_git_blob_hash_of_a_file_matches_the_in_memory_hash(tmp_path):
    path = tmp_path / "f.bin"
    payload = bytes(range(256)) * 500
    path.write_bytes(payload)
    assert git_blob_hash_file(path) == git_blob_hash(payload)


def test_empty_file_hash(tmp_path):
    path = tmp_path / "empty"
    path.write_bytes(b"")
    assert git_blob_hash_file(path) == git_blob_hash(b"")


def test_sha256_file_matches_hashlib(tmp_path):
    path = tmp_path / "f"
    payload = b"lfs content" * 1000
    path.write_bytes(payload)
    assert sha256_file(path) == hashlib.sha256(payload).hexdigest()


def test_eol_normalised_hash_ignores_crlf(tmp_path):
    crlf = tmp_path / "crlf.txt"
    crlf.write_bytes(b"line one\r\nline two\r\n")
    assert normalised_blob_hash(crlf) == git_blob_hash(b"line one\nline two\n")


def test_binary_detection():
    assert looks_binary(b"text\0with a nul")
    assert not looks_binary(b"pure text\n")


def test_cat_file_batch_parsing():
    """Content may contain newlines, so framing must be driven by the byte count."""
    body_a = b"line1\nline2\n"
    body_b = b"short"
    raw = (b"aaa blob %d\n" % len(body_a)) + body_a + b"\n" \
          + (b"bbb blob %d\n" % len(body_b)) + body_b + b"\n"
    parsed = _parse_batch(raw)
    assert parsed == {"aaa": body_a, "bbb": body_b}


def test_cat_file_batch_ignores_missing_objects():
    raw = b"deadbeef missing\n"
    assert _parse_batch(raw) == {}
