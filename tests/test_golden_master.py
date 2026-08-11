"""Golden-master trees for the native engine.

The equivalence tests prove the native engine agrees with `git svn`. That proof is
worth having, but it cannot be the *ongoing* guarantee: Git for Windows removed
`git svn` in v2.54.0, so on a growing share of machines there is nothing to compare
against, and keeping git-svn installable in CI meant holding git back to an old
version — testing against a git nobody runs.

So the reference behaviour is recorded here instead, as the exact Git tree objects
the fixture must produce. The constants were taken from output verified identical to
git-svn (see `test_native_engine.py`), so they encode the reference implementation's
behaviour permanently, in a form that needs neither git-svn nor an old git.

A tree object hashes paths, contents and file modes, and nothing else — no dates, no
authors, no commit metadata — so these are stable and reproducible. If one changes,
the conversion changed what it writes to disk, and that is either a bug or a
deliberate change that needs this file updated in the same commit.
"""

from __future__ import annotations

import subprocess

import pytest

from svn2gitlab.config import ConvertConfig, LayoutMode
from svn2gitlab.convert.native import NativeConverter
from svn2gitlab.svn.analyze import DetectedLayout
from svn2gitlab.svn.authors import AuthorMap, Identity

from conftest import requires_svn

pytestmark = pytest.mark.slow

# Verified byte-identical to `git svn` on the same fixture, and reproducible across
# runs. Do not edit without re-running that comparison.
GOLDEN_TREES = {
    "refs/remotes/svn/trunk": "959c3f24c6683ed75b5504b4eee0206d8279ac2f",
    "refs/remotes/svn/feature-a": "259c70b57ee46aba6aa700f8c80cb3ace0f95e71",
    "refs/remotes/svn/tags/v1.0": "259c70b57ee46aba6aa700f8c80cb3ace0f95e71",
}


def fixture_authors() -> AuthorMap:
    amap = AuthorMap()
    for user, name, email in [
        ("alice", "Alice", "alice@example.com"),
        ("bob", "Bob", "bob@example.com"),
        ("CONTOSO\\carol", "Carol", "carol@example.com"),
        ("(no author)", "svn", "svn@example.com"),
    ]:
        amap.set(user, Identity(name, email))
    return amap


def convert(tmp_path, svn_repo, tools):
    tmp_path.mkdir(parents=True, exist_ok=True)
    dump = tmp_path / "golden.dump"
    with dump.open("wb") as handle:
        subprocess.run([tools.svnadmin.path, "dump", str(svn_repo), "--quiet"],
                       stdout=handle, check=True)

    target = tmp_path / "golden"
    subprocess.run([tools.git.path, "init", "-q", "-b", "main", str(target)], check=True)
    converter = NativeConverter(
        git_exe=tools.git.path, repo_dir=target,
        layout=DetectedLayout(mode=LayoutMode.STANDARD, trunk="trunk",
                              branches=["branches"], tags=["tags"]),
        convert=ConvertConfig(default_branch="main"),
        authors=fixture_authors(), default_domain="example.com",
        svn_url=svn_repo.as_uri(), uuid="golden", emit_svn_id=False,
    )
    with dump.open("rb") as handle:
        converter.convert_stream(handle)
    return target


def tree_of(repo, ref, tools) -> str:
    result = subprocess.run([tools.git.path, "-C", str(repo), "rev-parse", f"{ref}^{{tree}}"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


@requires_svn
def test_converted_trees_match_the_golden_master(tmp_path, svn_repo, tools):
    repo = convert(tmp_path, svn_repo, tools)
    actual = {ref: tree_of(repo, ref, tools) for ref in GOLDEN_TREES}
    assert actual == GOLDEN_TREES, (
        "the native engine now writes different content.\n"
        "A tree SHA covers paths, contents and modes only, so this is a real change "
        "in what gets migrated - not a timestamp or metadata difference.\n"
        f"expected: {GOLDEN_TREES}\nactual:   {actual}"
    )


@requires_svn
def test_conversion_is_reproducible(tmp_path, svn_repo, tools):
    """Two conversions of the same input must be identical.

    Re-derivation after every sync depends on this: if it were not reproducible,
    each sync would rewrite history and force-push over the published repository.
    """
    first = convert(tmp_path / "a", svn_repo, tools)
    second = convert(tmp_path / "b", svn_repo, tools)
    for ref in GOLDEN_TREES:
        assert tree_of(first, ref, tools) == tree_of(second, ref, tools), \
            f"{ref} differed between two identical conversions"


@requires_svn
def test_golden_master_would_notice_a_content_change(tmp_path, svn_repo, tools):
    """A golden master that cannot fail is decoration, so prove it fails."""
    work = svn_repo.parent / "wc"
    (work / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    subprocess.run([tools.svn.path, "commit", "-q", "-m", "change content"],
                   cwd=work, check=True)

    repo = convert(tmp_path, svn_repo, tools)
    assert tree_of(repo, "refs/remotes/svn/trunk", tools) != \
        GOLDEN_TREES["refs/remotes/svn/trunk"]
