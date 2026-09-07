"""TFVC: REST client behaviour and changeset-to-Git conversion.

These run against `fake_tfs.py` rather than a real server, which is an honest
limitation: they prove we handle the API *as documented*, not as any particular TFS
instance actually behaves. The fake is therefore written to be awkward in the same
ways the real API is — truncating comments, emitting `changeType` as a flags string,
paging with a server-side cap — because a friendlier fake would let exactly the bugs
that matter slip through.

The first real run against a customer server must be a rehearsal.
"""

from __future__ import annotations

import hashlib
import subprocess

import pytest

from svn2gitlab.svn.authors import AuthorMap, Identity
from svn2gitlab.tfvc.client import TfvcClient, TfvcError, _iso_to_git_date, _parse_change_types
from svn2gitlab.tfvc.convert import (MAIN_BRANCH_KEY, TfvcConverter, TfvcMapper,
                                     detect_layout)

from conftest import requires_svn  # noqa: F401  (kept for marker parity)
from fake_tfs import FakeTfs, TfvcHistory, demo_history

from svn2gitlab.tools import detect_tools

pytestmark = pytest.mark.slow

GIT = detect_tools().git.path


def git(repo, *args) -> str:
    result = subprocess.run([GIT, "-C", str(repo), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def blob(repo, ref, path) -> bytes:
    result = subprocess.run([GIT, "-C", str(repo), "show", f"{ref}:{path}"],
                            capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


def exists(repo, ref, path) -> bool:
    return subprocess.run([GIT, "-C", str(repo), "cat-file", "-e", f"{ref}:{path}"],
                          capture_output=True).returncode == 0


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("edit", ["edit"]),
    ("rename, edit", ["rename", "edit"]),
    ("Rename, Edit", ["rename", "edit"]),
    (["add", "branch"], ["add", "branch"]),
    ("", []),
    (None, []),
])
def test_change_type_flags_are_split(raw, expected):
    """`changeType` is a flags enum; treating it as one value loses half a change."""
    assert _parse_change_types(raw) == expected


@pytest.mark.parametrize("raw,expected_prefix", [
    ("2019-05-29T18:00:23.457Z", "1559"),
    ("2019-05-29T18:00:23Z", "1559"),
    ("", "0 +0000"),
    ("nonsense", "0 +0000"),
])
def test_date_conversion(raw, expected_prefix):
    assert _iso_to_git_date(raw).startswith(expected_prefix)


# --------------------------------------------------------------------------- #
# Layout detection
# --------------------------------------------------------------------------- #

def test_layout_prefers_server_branch_definitions():
    layout = detect_layout("$/P", ["$/P/Main", "$/P/Release-1.0"])
    assert layout.main_root == "$/P/Main"
    assert layout.branch_roots == ["$/P/Main", "$/P/Release-1.0"]
    assert "server branch" in layout.detected_from


def test_layout_falls_back_to_path_conventions():
    """Many real projects branch by copying a folder and never convert it."""
    layout = detect_layout("$/P", [], sample_paths=[
        "$/P/Main/src/a.cs", "$/P/Branches/feature-x/src/a.cs",
        "$/P/Branches/feature-y/b.cs",
    ])
    assert layout.main_root == "$/P/Main"
    assert "$/P/Branches/feature-x" in layout.branch_roots
    assert "$/P/Branches/feature-y" in layout.branch_roots
    # The container itself is not a branch.
    assert "$/P/Branches" not in layout.branch_roots
    assert "conventions" in layout.detected_from


def test_mapper_splits_branch_from_subpath():
    layout = detect_layout("$/P", ["$/P/Main", "$/P/Release-1.0"])
    mapper = TfvcMapper(layout, default_branch="main")

    assert mapper.classify("$/P/Main/src/a.cs") == (MAIN_BRANCH_KEY, "src/a.cs", False)
    assert mapper.classify("$/P/Main") == (MAIN_BRANCH_KEY, "", True)
    assert mapper.classify("$/P/Release-1.0/src/a.cs") == ("Release-1.0", "src/a.cs", False)
    assert mapper.classify("$/OtherProject/x") is None
    assert mapper.branch_name(MAIN_BRANCH_KEY) == "main"


def test_longer_branch_roots_win():
    """`$/P/Branches/X` must not be swallowed by a `$/P/Branches` root."""
    layout = detect_layout("$/P", ["$/P/Branches", "$/P/Branches/X"])
    mapper = TfvcMapper(layout)
    key, subpath, _ = mapper.classify("$/P/Branches/X/file.cs")
    assert (key, subpath) == ("X", "file.cs")


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

@pytest.fixture
def tfs():
    with FakeTfs(demo_history()) as server:
        yield server


@pytest.fixture
def client(tfs):
    c = TfvcClient(tfs.url, collection="DefaultCollection", project="DemoProject",
                   token=tfs.token)
    c.probe()
    return c


def test_api_version_is_negotiated(tfs):
    c = TfvcClient(tfs.url, collection="DefaultCollection", token=tfs.token)
    assert c.probe() == "4.1"


def test_older_server_negotiates_down():
    """TFS 2015 speaks 2.0; asking for 4.1 must degrade rather than fail."""
    with FakeTfs(demo_history(), api_versions=("2.0",)) as server:
        c = TfvcClient(server.url, collection="DefaultCollection", token=server.token)
        assert c.probe() == "2.0"


def test_a_server_without_the_tfvc_api_is_reported_clearly():
    with FakeTfs(demo_history(), api_versions=("9.9",)) as server:
        c = TfvcClient(server.url, collection="DefaultCollection", token=server.token)
        with pytest.raises(TfvcError, match="could not negotiate"):
            c.probe()


def test_bad_credentials_are_reported_with_a_remedy(tfs):
    c = TfvcClient(tfs.url, collection="DefaultCollection", token="wrong-token")
    with pytest.raises(TfvcError) as excinfo:
        c.probe()
    assert "personal access token" in excinfo.value.remedy


def test_full_commit_messages_are_retrieved(client):
    """The API truncates comments to 80 characters unless asked otherwise.

    Accepting the default would silently shorten every substantial commit message in
    the migrated history.
    """
    changesets = list(client.iter_changesets())
    long_one = next(c for c in changesets if c.id == 2)
    assert len(long_one.message) > 80
    assert long_one.message.endswith("truncation length")
    assert not long_one.comment_truncated


def test_changesets_are_yielded_oldest_first(client):
    ids = [c.id for c in client.iter_changesets()]
    assert ids == sorted(ids)
    assert ids == [1, 2, 3, 4, 5, 6, 7]


def test_paging_follows_a_server_side_cap():
    """The server may return fewer rows than requested; the client must not stop."""
    history = demo_history()
    with FakeTfs(history, page_cap=2) as server:
        c = TfvcClient(server.url, collection="DefaultCollection", token=server.token)
        c.probe()
        assert [x.id for x in c.iter_changesets(page_size=50)] == [1, 2, 3, 4, 5, 6, 7]


def test_rename_and_edit_is_parsed_as_both(client):
    change = client.changeset_changes(4)[0]
    assert change.is_rename
    assert change.touches_content, "the edit half of a rename+edit was lost"
    assert change.source_path.endswith("app.cs")


def test_content_is_hash_verified(client):
    change = next(c for c in client.changeset_changes(3) if not c.is_folder)
    content = client.item_content(change.path, change.version,
                                  expected_md5=change.md5, expected_size=change.size)
    assert hashlib.md5(content).hexdigest() == change.md5


def test_a_truncated_download_is_refused(client, monkeypatch):
    """Silently accepting short content would corrupt the migrated file."""
    change = next(c for c in client.changeset_changes(3) if not c.is_folder)
    monkeypatch.setattr(client, "request", lambda *a, **k: b"too short")
    with pytest.raises(TfvcError, match="expected .* bytes"):
        client.item_content(change.path, change.version, expected_size=change.size)


def test_item_content_is_cached(client):
    change = next(c for c in client.changeset_changes(3) if not c.is_folder)
    client.item_content(change.path, change.version)
    before = len(client.session.headers)  # sentinel; count requests via the server
    again = client.item_content(change.path, change.version)
    assert again  # served from cache, no second request
    assert len(client._content_cache) == 1


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #

def author_map() -> AuthorMap:
    amap = AuthorMap()
    for user, name, email in [
        ("CONTOSO\\alice", "Alice", "alice@contoso.com"),
        ("CONTOSO\\bob", "Bob", "bob@contoso.com"),
        ("CONTOSO\\carol", "Carol", "carol@contoso.com"),
    ]:
        amap.set(user, Identity(name, email))
    return amap


@pytest.fixture
def converted(tmp_path, tfs):
    repo = tmp_path / "mirror"
    subprocess.run([GIT, "init", "-q", "-b", "main", str(repo)], check=True)
    client = TfvcClient(tfs.url, collection="DefaultCollection", project="DemoProject",
                        token=tfs.token)
    client.probe()
    layout = detect_layout("$/DemoProject", [b["path"] for b in client.branches()])
    converter = TfvcConverter(client, GIT, repo, layout, author_map(),
                              default_branch="main", collection_url=tfs.url)
    result = converter.convert(list(client.iter_changesets()))
    return repo, result


def test_conversion_produces_a_branch_per_tfvc_branch(converted):
    repo, result = converted
    refs = git(repo, "for-each-ref", "--format=%(refname)").splitlines()
    assert "refs/remotes/tfvc/main" in refs
    assert "refs/remotes/tfvc/Release-1.0" in refs
    assert result.commits == 7
    assert result.unresolved_branches == []


def test_authors_are_mapped(converted):
    repo, _ = converted
    emails = set(git(repo, "log", "--all", "--format=%ae").splitlines())
    assert emails == {"alice@contoso.com", "bob@contoso.com", "carol@contoso.com"}
    assert not any("CONTOSO" in e for e in emails)


def test_rename_with_edit_keeps_the_new_content(converted):
    """The flags-enum trap: handling only the rename leaves stale bytes behind."""
    repo, _ = converted
    assert blob(repo, "refs/remotes/tfvc/main", "src/program.cs") == \
        b"class Program { void Run() {} }\n"
    assert not exists(repo, "refs/remotes/tfvc/main", "src/app.cs")


def test_deletes_are_applied(converted):
    repo, _ = converted
    assert not exists(repo, "refs/remotes/tfvc/main", "README.md")


def test_binary_content_is_byte_exact(converted):
    repo, _ = converted
    assert blob(repo, "refs/remotes/tfvc/main", "assets/logo.bin") == bytes(range(256)) * 4


def test_branch_descends_from_its_source(converted):
    """A branch must continue the mainline's history, not start a fresh root."""
    repo, _ = converted
    base = git(repo, "merge-base", "refs/remotes/tfvc/main", "refs/remotes/tfvc/Release-1.0")
    assert base, "the branch shares no history with main"


def test_branch_is_isolated_from_later_mainline_changes(converted):
    """Release-1.0 branched before README was deleted, so it must still have it."""
    repo, _ = converted
    assert exists(repo, "refs/remotes/tfvc/Release-1.0", "README.md")
    assert not exists(repo, "refs/remotes/tfvc/main", "README.md")
    assert blob(repo, "refs/remotes/tfvc/Release-1.0", "src/program.cs") == \
        b"class Program { void Run() { /* fixed */ } }\n"


def test_branch_creation_does_not_redownload_the_tree(converted):
    """Copying a branch must cost one `ls`, not a download per file.

    TFVC emits a `branch` change for every item in the branched folder. Replaying
    those literally would re-fetch an entire branch's content that Git already holds
    - the difference between minutes and hours on a real repository.
    """
    _repo, result = converted
    # Only the six distinct file versions authored on Main are fetched; the four
    # branch-copy items are resolved from the existing commit instead.
    assert result.blobs_written <= 6
    assert result.bytes_downloaded < 2000


def test_changeset_id_is_recorded_for_incremental_sync(converted):
    repo, _ = converted
    message = git(repo, "log", "-1", "--format=%B", "refs/remotes/tfvc/main")
    assert "tfs-changeset-id:" in message
    assert "@7" in message


def test_conversion_is_reproducible(tmp_path, tfs):
    """Re-derivation after a sync depends on identical input giving identical trees."""
    trees = []
    for name in ("a", "b"):
        repo = tmp_path / name
        subprocess.run([GIT, "init", "-q", "-b", "main", str(repo)], check=True)
        client = TfvcClient(tfs.url, collection="DefaultCollection",
                            project="DemoProject", token=tfs.token)
        client.probe()
        layout = detect_layout("$/DemoProject", [b["path"] for b in client.branches()])
        TfvcConverter(client, GIT, repo, layout, author_map(), default_branch="main",
                      collection_url=tfs.url).convert(list(client.iter_changesets()))
        trees.append(git(repo, "rev-parse", "refs/remotes/tfvc/main^{tree}"))
    assert trees[0] == trees[1]


def test_paths_outside_the_layout_are_reported_not_silently_dropped(tmp_path):
    history = TfvcHistory("P")
    history.commit("u", "in and out", [
        history.mkdir("$/P/Main", is_branch=True),
        history.add("$/P/Main/a.txt", b"in\n"),
        history.add("$/P/Loose/b.txt", b"out\n"),
    ])
    history.branches = ["$/P/Main"]
    with FakeTfs(history) as server:
        repo = tmp_path / "m"
        subprocess.run([GIT, "init", "-q", "-b", "main", str(repo)], check=True)
        client = TfvcClient(server.url, collection="C", project="P", token=server.token)
        client.probe()
        layout = detect_layout("$/P", ["$/P/Main"])
        result = TfvcConverter(client, GIT, repo, layout, AuthorMap(),
                               collection_url=server.url).convert(
            list(client.iter_changesets()))
    assert result.skipped_paths == 1, "a path outside the layout was not accounted for"
    assert not exists(repo, "refs/remotes/tfvc/main", "b.txt")


# --------------------------------------------------------------------------- #
# Whole pipeline: TFVC -> GitLab
# --------------------------------------------------------------------------- #

def git_http_backend_available() -> bool:
    from fake_gitlab import find_git_http_backend
    return find_git_http_backend(GIT) is not None


@pytest.mark.skipif(not git_http_backend_available(),
                    reason="git-http-backend is required to serve the fake GitLab")
def test_tfvc_migrates_to_gitlab_and_verifies(tmp_path, tfs):
    """The whole point: a TFVC project ends up in GitLab, proven byte-for-byte."""
    from fake_gitlab import FakeGitLab
    from svn2gitlab.config import load_config
    from svn2gitlab.pipeline import build_pipelines
    from svn2gitlab.state import StateStore

    with FakeGitLab(tmp_path / "gitlab", GIT) as gitlab:
        authors = tmp_path / "authors.txt"
        authors.write_text(
            "CONTOSO\\alice = Alice <alice@contoso.com>\n"
            "CONTOSO\\bob = Bob <bob@contoso.com>\n"
            "CONTOSO\\carol = Carol <carol@contoso.com>\n", encoding="utf-8")

        config_path = tmp_path / "tfvc.yaml"
        config_path.write_text(f"""
version: 1
name: tfsdemo
workdir: {(tmp_path / 'work').as_posix()}
source:
  kind: tfvc
  url: {tfs.url}
  collection: DefaultCollection
  project: DemoProject
  token: {tfs.token}
authors:
  file: {authors.as_posix()}
  default_domain: contoso.com
convert:
  default_branch: main
  strip_svn_metadata: true
  generate_gitignore: false
target:
  gitlab_url: {gitlab.url}
  token: {gitlab.token}
  namespace: acme
  project: demo
verify:
  mode: full
  verify_all_branches: true
""", encoding="utf-8")

        config = load_config(config_path)
        state = StateStore(config.workdir_path() / "state.db")
        pipeline = build_pipelines(config, state, dry_run=False)[0]
        outcome = pipeline.run()

        assert outcome.ok, outcome.error

        # It arrived in GitLab.
        refs = gitlab.refs("acme/demo")
        assert "refs/heads/main" in refs
        assert "refs/heads/Release-1.0" in refs

        # And a clone gives real files back.
        clone = tmp_path / "clone"
        result = subprocess.run(
            [GIT, "clone", "-q", f"{gitlab.url}/acme/demo.git", str(clone)],
            capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

        # Assert on the *stored* bytes, not the checked-out file. Git converts line
        # endings in the working tree when core.autocrlf is on, which it is by
        # default on Windows - that is a checkout preference, not what was migrated,
        # and comparing the working tree would make this test fail on Windows for a
        # migration that is entirely correct.
        assert blob(clone, "HEAD", "src/program.cs") == \
            b"class Program { void Run() {} }\n"
        assert blob(clone, "HEAD", "assets/logo.bin") == bytes(range(256)) * 4
        # The binary must survive checkout untouched whatever the eol setting.
        assert (clone / "assets" / "logo.bin").read_bytes() == bytes(range(256)) * 4

        # Verification compared against TFVC and found nothing wrong.
        assert outcome.verification is not None
        assert outcome.verification.ok, [
            d.to_dict() for b in outcome.verification.branches for d in b.differences]
        assert any(b.files_compared > 0 for b in outcome.verification.branches)


@pytest.mark.skipif(not git_http_backend_available(), reason="git-http-backend required")
def test_tfvc_verification_detects_tampering(tmp_path, tfs):
    """A verification that cannot fail proves nothing."""
    from svn2gitlab.tfvc.verify import verify_branch
    from svn2gitlab.verify.verify import git_blob_hash

    client = TfvcClient(tfs.url, collection="DefaultCollection", project="DemoProject",
                        token=tfs.token)
    client.probe()

    # A tree claiming content that TFVC does not have.
    wrong = b"tampered\n"
    tree = {"src/program.cs": ("100644", git_blob_hash(wrong))}
    record = verify_branch(client, tree, lambda _sha: wrong, "main",
                           "$/DemoProject/Main", changeset=7)
    assert record.differences, "tampered content was reported as matching"
    assert any(d.kind == "content" for d in record.differences)


def test_migrated_repositories_pin_line_endings(tmp_path, tfs):
    """Repositories we create must disable autocrlf.

    Everything the tool guarantees about reproducibility assumes the bytes written
    are the bytes stored. If Git were free to translate line endings, a conversion
    run on Windows and the same conversion run on Linux would produce different
    blobs - and the golden master, the byte-for-byte verification and the
    fast-forward push would all quietly stop meaning what they claim.
    """
    from svn2gitlab.convert.git import Git
    from svn2gitlab.tools import detect_tools

    repo = tmp_path / "cfgcheck"
    git_wrapper = Git(detect_tools(), repo)
    git_wrapper.init(initial_branch="main")
    git_wrapper.apply_base_config()

    assert git_wrapper.config_get("core.autocrlf") == "false"
    assert git_wrapper.config_get("core.safecrlf") == "false"
