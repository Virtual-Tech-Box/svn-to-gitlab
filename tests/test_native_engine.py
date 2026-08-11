"""The conversion engine: dump parsing, layout mapping and real conversions.

The engine reads an `svnadmin dump` and writes a `git fast-import` stream. Its
output was originally validated against `git svn` and is now pinned permanently by
`test_golden_master.py`, which needs no external reference implementation.
"""

from __future__ import annotations

import io
import subprocess

import pytest

from svn2gitlab.config import ConvertConfig, LayoutMode, SourceConfig
from svn2gitlab.convert.dumpstream import DumpReader, _parse_properties, svn_date_to_git
from svn2gitlab.convert.native import LayoutMapper, NativeConverter, TRUNK_BRANCH
from svn2gitlab.errors import ConversionError
from svn2gitlab.svn.analyze import DetectedLayout
from svn2gitlab.svn.authors import AuthorMap, Identity
from svn2gitlab.tools import ToolSet

from conftest import requires_svn

# --------------------------------------------------------------------------- #
# Dump stream parsing
# --------------------------------------------------------------------------- #

MINIMAL_DUMP = b"""SVN-fs-dump-format-version: 2

UUID: 11111111-2222-3333-4444-555555555555

Revision-number: 0
Prop-content-length: 10
Content-length: 10

PROPS-END

Revision-number: 1
Prop-content-length: 98
Content-length: 98

K 10
svn:author
V 6
jsmith
K 8
svn:date
V 27
2019-04-11T08:12:33.123456Z
K 7
svn:log
V 5
hello
PROPS-END

Node-path: trunk
Node-kind: dir
Node-action: add
Prop-content-length: 10
Content-length: 10

PROPS-END

Node-path: trunk/a.txt
Node-kind: file
Node-action: add
Prop-content-length: 10
Text-content-length: 6
Content-length: 16

PROPS-END
hello
"""


def test_dump_header_and_uuid_are_read():
    reader = DumpReader(io.BytesIO(MINIMAL_DUMP))
    revisions = list(reader.revisions())
    assert reader.format_version == 2
    assert reader.uuid == "11111111-2222-3333-4444-555555555555"
    assert [r.number for r in revisions] == [0, 1]


def test_revision_properties_are_decoded():
    revisions = list(DumpReader(io.BytesIO(MINIMAL_DUMP)).revisions())
    r1 = revisions[1]
    assert r1.author == "jsmith"
    assert r1.message == "hello"
    assert r1.date.startswith("2019-04-11")


def test_node_content_is_exact():
    revisions = list(DumpReader(io.BytesIO(MINIMAL_DUMP)).revisions())
    nodes = revisions[1].nodes
    assert [n.path for n in nodes] == ["trunk", "trunk/a.txt"]
    assert nodes[0].is_dir
    assert nodes[1].content == b"hello\n"


def test_property_block_parsing():
    block = (b"K 14\nsvn:executable\nV 1\n*\n"
             b"K 10\nsvn:ignore\nV 6\n*.pyc\n\nPROPS-END\n")
    props, deleted = _parse_properties(block)
    assert props["svn:executable"] == b"*"
    assert props["svn:ignore"] == b"*.pyc\n"
    assert deleted == []


def test_deleted_properties_are_reported():
    props, deleted = _parse_properties(b"D 14\nsvn:executable\nPROPS-END\n")
    assert deleted == ["svn:executable"]
    assert props == {}


def test_delta_dumps_are_refused_with_a_remedy():
    """Silently mis-applying a delta would corrupt content; refuse loudly instead."""
    dump = MINIMAL_DUMP.replace(b"Node-path: trunk/a.txt\nNode-kind: file\n",
                                b"Node-path: trunk/a.txt\nNode-kind: file\nText-delta: true\n")
    with pytest.raises(ConversionError, match="text delta"):
        list(DumpReader(io.BytesIO(dump)).revisions())


def test_truncated_dump_is_detected():
    with pytest.raises(ConversionError, match="ended early"):
        list(DumpReader(io.BytesIO(MINIMAL_DUMP[:-4])).revisions())


@pytest.mark.parametrize("raw,expected_prefix", [
    ("2019-04-11T08:12:33.123456Z", "1554"),
    ("", "0 +0000"),
])
def test_date_conversion(raw, expected_prefix):
    assert svn_date_to_git(raw).startswith(expected_prefix)


# --------------------------------------------------------------------------- #
# Layout mapping
# --------------------------------------------------------------------------- #

def standard_mapper():
    return LayoutMapper(
        DetectedLayout(mode=LayoutMode.STANDARD, trunk="trunk",
                       branches=["branches"], tags=["tags"]), "main")


@pytest.mark.parametrize("path,kind,name,subpath", [
    ("trunk/src/app.py", "branch", "", "src/app.py"),
    ("trunk", "branch", "", ""),
    ("branches/release-1.x/src/app.py", "branch", "release-1.x", "src/app.py"),
    ("branches/release-1.x", "branch", "release-1.x", ""),
    ("tags/v1.0/README", "tag", "v1.0", "README"),
    ("tags/v1.0", "tag", "v1.0", ""),
])
def test_standard_layout_mapping(path, kind, name, subpath):
    cls = standard_mapper().classify(path)
    assert (cls.kind, cls.name, cls.subpath) == (kind, name, subpath)


def test_container_directories_are_not_branches():
    """`branches` itself is not a branch, and must not create a ref."""
    mapper = standard_mapper()
    assert mapper.classify("branches").kind == "outside"
    assert mapper.classify("tags").kind == "outside"


def test_paths_outside_the_layout_are_excluded():
    assert standard_mapper().classify("vendor/thing.txt").kind == "outside"


def test_flat_layout_puts_everything_on_the_default_branch():
    mapper = LayoutMapper(DetectedLayout(mode=LayoutMode.FLAT, trunk="."), "main")
    cls = mapper.classify("src/app.py")
    assert cls.kind == "branch" and cls.name == "" and cls.subpath == "src/app.py"


def test_nested_branches_use_two_levels():
    mapper = LayoutMapper(
        DetectedLayout(mode=LayoutMode.STANDARD, trunk="trunk",
                       branches=["branches/*"], tags=["tags"]), "main")
    cls = mapper.classify("branches/team-a/feature-1/src/app.py")
    assert cls.name == "team-a/feature-1"
    assert cls.subpath == "src/app.py"


def test_ref_keys_separate_tags_from_branches():
    mapper = standard_mapper()
    assert mapper.ref_for(mapper.classify("trunk/x")) == TRUNK_BRANCH
    assert mapper.ref_for(mapper.classify("tags/v1/x")).startswith("tag:")
    assert mapper.ref_for(mapper.classify("branches/b/x")) == "b"


# --------------------------------------------------------------------------- #
# Conversion against a real repository
# --------------------------------------------------------------------------- #

def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def author_map() -> AuthorMap:
    amap = AuthorMap()
    for user, name, email in [
        ("alice", "Alice", "alice@example.com"),
        ("bob", "Bob", "bob@example.com"),
        ("CONTOSO\\carol", "Carol", "carol@example.com"),
        ("(no author)", "svn", "svn@example.com"),
    ]:
        amap.set(user, Identity(name, email))
    return amap


def convert_natively(tmp_path, svn_repo, tools, keep_trailer=False):
    dump = tmp_path / "repo.dump"
    with dump.open("wb") as handle:
        subprocess.run([tools.svnadmin.path, "dump", str(svn_repo), "--quiet"],
                       stdout=handle, check=True)

    target = tmp_path / "native"
    subprocess.run([tools.git.path, "init", "-q", "-b", "main", str(target)], check=True)
    converter = NativeConverter(
        git_exe=tools.git.path, repo_dir=target,
        layout=DetectedLayout(mode=LayoutMode.STANDARD, trunk="trunk",
                              branches=["branches"], tags=["tags"]),
        convert=ConvertConfig(default_branch="main", keep_revision_trailer=keep_trailer),
        authors=author_map(), default_domain="example.com",
        svn_url=svn_repo.as_uri(), uuid="test-uuid", emit_svn_id=False,
    )
    with dump.open("rb") as handle:
        result = converter.convert_stream(handle)
    return target, result, converter


@requires_svn
def test_native_conversion_produces_the_expected_refs(tmp_path, svn_repo, tools):
    target, result, _converter = convert_natively(tmp_path, svn_repo, tools)
    refs = git(target, "for-each-ref", "--format=%(refname)").splitlines()
    assert "refs/remotes/svn/trunk" in refs
    assert "refs/remotes/svn/feature-a" in refs
    assert "refs/remotes/svn/tags/v1.0" in refs
    assert result.commits > 0
    assert result.unresolved_copies == []


@requires_svn
def test_native_conversion_maps_authors(tmp_path, svn_repo, tools):
    target, _result, _c = convert_natively(tmp_path, svn_repo, tools)
    emails = set(git(target, "log", "--all", "--format=%ae").splitlines())
    assert "carol@example.com" in emails
    assert not any("CONTOSO" in e for e in emails)


@requires_svn
def test_native_conversion_preserves_content_and_modes(tmp_path, svn_repo, tools):
    target, _result, _c = convert_natively(tmp_path, svn_repo, tools)
    listing = git(target, "ls-tree", "-r", "refs/remotes/svn/trunk")
    assert "src/app.py" in listing
    assert "binary.dat" in listing
    blob = git(target, "show", "refs/remotes/svn/trunk:src/app.py")
    assert blob == "print('hello, world')"


@requires_svn
def test_branch_history_is_connected_to_its_copy_source(tmp_path, svn_repo, tools):
    """A branch must descend from the commit it was copied from, not be an orphan."""
    target, _result, _c = convert_natively(tmp_path, svn_repo, tools)
    merge_base = git(target, "merge-base", "refs/remotes/svn/trunk",
                     "refs/remotes/svn/feature-a")
    assert merge_base, "the branch shares no history with trunk"


@requires_svn
def test_svn_ignore_is_captured_for_gitignore_generation(tmp_path, svn_repo, tools):
    _target, _result, converter = convert_natively(tmp_path, svn_repo, tools)
    content = converter.gitignore_content()
    assert "*.pyc" in content
    assert "build/" in content




# --------------------------------------------------------------------------- #
# Incremental behaviour
# --------------------------------------------------------------------------- #

@pytest.mark.slow
@requires_svn
def test_svn_ignore_survives_an_incremental_conversion(tmp_path, svn_repo, tools):
    """An incremental dump slice contains no historical svn:ignore nodes.

    Without persisted state the generated .gitignore silently vanished from the
    published repository on the first sync after the migration - the export stage
    asked the mirror for ignore content, got nothing, and dropped the file.
    """
    from svn2gitlab.config import SourceConfig
    from svn2gitlab.convert.engine import NativeMirror

    layout = DetectedLayout(mode=LayoutMode.STANDARD, trunk="trunk",
                            branches=["branches"], tags=["tags"])
    mirror = NativeMirror(
        tools=tools, repo_dir=tmp_path / "mirror", source_url=svn_repo.as_uri(),
        layout=layout, convert=ConvertConfig(default_branch="main"),
        source=SourceConfig(local_path=str(svn_repo)), local_repo=svn_repo,
        authors=author_map(), uuid="test-uuid",
    )
    mirror.init()
    mirror.fetch(head_revision=0)
    first = mirror.svn_ignore_content()
    assert "*.pyc" in first

    # A second pass converts nothing new, exactly as a no-op sync would.
    mirror.fetch(head_revision=0)
    assert mirror.svn_ignore_content() == first, "ignore state was lost on re-conversion"


@pytest.mark.slow
@requires_svn
def test_incremental_conversion_extends_history(tmp_path, svn_repo, tools):
    from svn2gitlab.config import SourceConfig
    from svn2gitlab.convert.engine import NativeMirror

    layout = DetectedLayout(mode=LayoutMode.STANDARD, trunk="trunk",
                            branches=["branches"], tags=["tags"])
    mirror = NativeMirror(
        tools=tools, repo_dir=tmp_path / "mirror", source_url=svn_repo.as_uri(),
        layout=layout, convert=ConvertConfig(default_branch="main"),
        source=SourceConfig(local_path=str(svn_repo)), local_repo=svn_repo,
        authors=author_map(), uuid="test-uuid",
    )
    mirror.init()
    mirror.fetch(head_revision=0)
    before = git(tmp_path / "mirror", "rev-list", "--count", "refs/remotes/svn/trunk")

    work = svn_repo.parent / "wc"
    (work / "LATER.txt").write_text("later\n", encoding="utf-8")
    subprocess.run([tools.svn.path, "add", "-q", "LATER.txt"], cwd=work, check=True)
    subprocess.run([tools.svn.path, "commit", "-q", "-m", "a later commit"],
                   cwd=work, check=True)

    result = mirror.fetch(head_revision=0)
    after = git(tmp_path / "mirror", "rev-list", "--count", "refs/remotes/svn/trunk")
    assert int(after) == int(before) + 1, "the incremental pass did not extend trunk"
    assert result.revisions_fetched >= 1
    listing = git(tmp_path / "mirror", "ls-tree", "-r", "--name-only", "refs/remotes/svn/trunk")
    assert "LATER.txt" in listing




@pytest.mark.slow
@requires_svn
def test_pipeline_converts_a_repository_end_to_end(tmp_path, svn_repo, migration_config):
    """The whole pipeline, driven by the built-in engine."""
    from svn2gitlab.config import load_config
    from svn2gitlab.pipeline import build_pipelines
    from svn2gitlab.state import StateStore

    config = load_config(migration_config)
    state = StateStore(config.workdir_path() / "state.db")
    pipeline = build_pipelines(config, state, dry_run=True)[0]
    outcome = pipeline.run()
    assert outcome.ok, outcome.error
