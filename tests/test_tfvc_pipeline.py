"""The TFVC path through the migration pipeline, end to end against a fake TFS.

`tests/test_tfvc.py` exercises the client and the converter directly. Nothing
exercised the *pipeline*, which is its own wiring: it builds the layout, the mirror
and the exporter from the config rather than from arguments a test hand-picked. Two
production defects lived in exactly that gap:

* the conversion asked for the layout without `explicitly_configured`, so it picked
  the mainline alphabetically while `analyze` honoured the operator's ordering — the
  report named one branch as trunk and the export published a different one as
  `main`, and no verification could catch it, because verification compares `main`
  against whatever the conversion decided `main` meant;
* branch creation copied the source branch's converted head instead of the per-file
  records TFVC supplies, silently staling every branched file.

Both are asserted here, through the same entry point the CLI uses.
"""

from __future__ import annotations

import subprocess

import pytest

from svn2gitlab.config import load_config
from svn2gitlab.pipeline import build_pipelines
from svn2gitlab.state import StateStore
from svn2gitlab.tools import detect_tools

from fake_tfs import FakeTfs, TfvcHistory

pytestmark = pytest.mark.slow

GIT = detect_tools().git.path

CONFIG_TEMPLATE = """
version: 1
name: tfvcrepo
workdir: {work}
source:
  kind: tfvc
  url: {url}
  collection: DefaultCollection
  project: P
  token: {token}
  branch_roots:
    - $/P/Zeta-Main
    - $/P/Alpha-Old
    - $/P/Beta-Rel
authors:
  file: {authors}
  default_domain: example.com
  policy: generate
convert:
  default_branch: main
target:
  gitlab_url: https://gitlab.example.com
  token: not-a-real-token
  namespace: testgroup
  project: tfvcrepo
verify:
  mode: full
  verify_all_branches: true
"""


def git(repo, *args) -> str:
    result = subprocess.run([GIT, "-C", str(repo), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def blob(repo, ref, path) -> bytes:
    result = subprocess.run([GIT, "-C", str(repo), "show", f"{ref}:{path}"],
                            capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


def build(tmp_path, tfs):
    config_path = tmp_path / "migration.yaml"
    config_path.write_text(CONFIG_TEMPLATE.format(
        work=(tmp_path / "work").as_posix(),
        url=tfs.url,
        token=tfs.token,
        authors=(tmp_path / "authors.txt").as_posix(),
    ), encoding="utf-8")
    config = load_config(config_path)
    state = StateStore(config.workdir_path() / "state.db")
    return build_pipelines(config, state, dry_run=True)[0]


@pytest.fixture
def history() -> TfvcHistory:
    """Mainline that does not sort first, and a branch cut from an older version.

    `Zeta-Main` is deliberately last alphabetically: a converter that picks the
    mainline by sort order will choose `Alpha-Old` instead and publish it as `main`.
    """
    root = "$/P"
    h = TfvcHistory("P")
    h.commit("CONTOSO\\alice", "start the mainline", [
        h.mkdir(f"{root}/Zeta-Main", is_branch=True),
        h.add(f"{root}/Zeta-Main/a.txt", b"version one\n"),
    ])
    h.commit("CONTOSO\\alice", "cut an early branch", [
        h.branch(f"{root}/Alpha-Old", f"{root}/Zeta-Main", is_folder=True),
        h.branch(f"{root}/Alpha-Old/a.txt", f"{root}/Zeta-Main/a.txt", b"version one\n"),
    ])
    h.commit("CONTOSO\\alice", "move the mainline on", [
        h.edit(f"{root}/Zeta-Main/a.txt", b"version two\n"),
    ])
    # Branched from changeset 1, so it holds "version one" even though the mainline
    # has already moved to "version two". Copying the mainline's head gets this wrong.
    h.commit("CONTOSO\\alice", "Branched from $/P/Zeta-Main", [
        h.branch(f"{root}/Beta-Rel", f"{root}/Zeta-Main", is_folder=True),
        h.branch(f"{root}/Beta-Rel/a.txt", f"{root}/Zeta-Main/a.txt", b"version one\n"),
    ])
    h.branches = [f"{root}/Zeta-Main", f"{root}/Alpha-Old", f"{root}/Beta-Rel"]
    return h


@pytest.fixture
def migrated(tmp_path, history):
    with FakeTfs(history) as tfs:
        pipeline = build(tmp_path, tfs)
        yield pipeline, pipeline.run()


def test_the_configured_mainline_becomes_main(migrated):
    """The first `branch_roots` entry is an instruction, not a suggestion.

    Publishing a side branch as the default branch is the kind of mistake nobody
    notices until everyone has cloned it.
    """
    pipeline, outcome = migrated
    assert outcome.ok, outcome.error

    export = pipeline.repo.export_dir
    branches = git(export, "for-each-ref", "--format=%(refname:short)",
                   "refs/heads/").split()

    assert "main" in branches
    # `main` is Zeta-Main's history, so Zeta-Main must not also exist separately, and
    # the alphabetically-first branch must still be present under its own name.
    assert "Zeta-Main" not in branches
    assert "Alpha-Old" in branches
    assert blob(export, "main", "a.txt") == b"version two\n"


def test_a_branch_keeps_the_version_it_was_cut_from(migrated):
    """Branch content comes from TFVC's per-file records, not the source's head."""
    pipeline, _ = migrated
    export = pipeline.repo.export_dir
    assert blob(export, "Beta-Rel", "a.txt") == b"version one\n"
    assert blob(export, "Alpha-Old", "a.txt") == b"version one\n"


def test_verification_passes_against_the_fake_server(migrated):
    """The whole point: a clean run must verify byte-for-byte, every branch."""
    _pipeline, outcome = migrated
    report = outcome.verification
    assert report is not None
    assert report.total_differences == 0, [
        (b.branch, [d.path for d in b.differences]) for b in report.branches if not b.ok
    ]
    assert {b.branch for b in report.branches} >= {"main", "Alpha-Old", "Beta-Rel"}


def test_a_mainline_disagreement_stops_the_run(tmp_path, history, monkeypatch):
    """The two layouts must agree, and the run must stop when they do not.

    This is the one failure verification is blind to, so the check has to be its own
    thing rather than something a later stage would notice. `tfvc/analyze.py` binds
    `detect_layout` at import time while the pipeline imports it per call, so patching
    the module attribute here changes the conversion's answer and not the analysis's -
    which is exactly the divergence being guarded against.
    """
    import svn2gitlab.tfvc.convert as convert_mod
    real = convert_mod.detect_layout

    def disagrees(*args, **kwargs):
        layout = real(*args, **kwargs)
        layout.main_root = "$/P/Alpha-Old"
        return layout

    monkeypatch.setattr(convert_mod, "detect_layout", disagrees)

    with FakeTfs(history) as tfs:
        outcome = build(tmp_path, tfs).run()

    assert not outcome.ok
    assert "mainline" in outcome.error
    assert "Alpha-Old" in outcome.error and "Zeta-Main" in outcome.error
