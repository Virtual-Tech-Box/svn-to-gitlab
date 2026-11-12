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


# --------------------------------------------------------------------------- #
# Paths Git cannot store
# --------------------------------------------------------------------------- #

from svn2gitlab.convert.fastimport import portable_path
from svn2gitlab.convert.mirrorfmt import escape_ref_name, unescape_ref_name


@pytest.mark.parametrize("path,windows,expected", [
    ("src/app.py", True, "src/app.py"),
    ("branches/https:/host/svn/x", True, "branches/https%3A/host/svn/x"),
    ("branches/https:/host/svn/x", False, "branches/https:/host/svn/x"),
    ("lib/.git/config", False, "lib/%2Egit/config"),
    ("lib/GIT~1/config", False, "lib/%67IT~1/config"),
    ("docs/notes.", True, "docs/notes%2E"),
    ("src/aux.c", True, "src/%61ux.c"),
    ("src/auxiliary.c", True, "src/auxiliary.c"),
])
def test_portable_path(path, windows, expected):
    assert portable_path(path, windows=windows) == expected


@pytest.mark.parametrize("name", ["https:", "release 1.0", "50%off", "v1.", ".hidden", "a..b"])
def test_ref_name_escaping_is_valid_and_reversible(name):
    escaped = escape_ref_name(name)
    check = subprocess.run(["git", "check-ref-format", f"refs/remotes/svn/{escaped}"])
    assert check.returncode == 0, escaped
    assert unescape_ref_name(escaped) == name


def _dump(*revisions) -> bytes:
    """A fulltext dump; each revision is a list of (path, kind, action, extras)."""
    out = [b"SVN-fs-dump-format-version: 2\n\nUUID: test-uuid\n\n"]
    for number, nodes in enumerate(revisions, 1):
        props = (b"K 10\nsvn:author\nV 5\nalice\nK 8\nsvn:date\nV 27\n"
                 b"2020-01-01T00:00:00.000000Z\nK 7\nsvn:log\nV 1\nx\nPROPS-END\n")
        out.append(b"Revision-number: %d\nProp-content-length: %d\nContent-length: %d\n\n"
                   % (number, len(props), len(props)) + props + b"\n")
        for path, kind, action, extras in nodes:
            head = f"Node-path: {path}\n"
            if kind:
                head += f"Node-kind: {kind}\n"
            head += f"Node-action: {action}\n"
            if "copyfrom" in extras:
                src_path, src_rev = extras["copyfrom"]
                head += f"Node-copyfrom-rev: {src_rev}\nNode-copyfrom-path: {src_path}\n"
            body = b""
            if "text" in extras:
                text = extras["text"]
                body = b"PROPS-END\n" + text
                head += (f"Prop-content-length: 10\nText-content-length: {len(text)}\n"
                         f"Content-length: {len(body)}\n")
            out.append(head.encode() + b"\n" + body + b"\n\n")
    return b"".join(out)


def _convert_dump(tmp_path, data, layout, windows, ignore_paths=None, root=""):
    target = tmp_path / "native"
    subprocess.run(["git", "init", "-q", "-b", "main", str(target)], check=True)
    converter = NativeConverter(
        git_exe="git", repo_dir=target, layout=layout,
        convert=ConvertConfig(default_branch="main"), authors=author_map(),
        emit_svn_id=False, ignore_paths=ignore_paths, root=root,
    )
    converter.windows = windows
    result = converter.convert_stream(io.BytesIO(data))
    return target, result


FLAT = DetectedLayout(mode=LayoutMode.FLAT, trunk=".")
URL_DIR = "P/branches/https:/10.0.0.1/svn/R/P/branches/v1.0/WebContent"

# r1 creates a branch, r2 copies it to a URL pasted as a path (TortoiseSVN "copy to"),
# r3 is the usual clean-up: move it somewhere sensible.
PASTED_URL_HISTORY = _dump(
    [("P", "dir", "add", {}), ("P/branches", "dir", "add", {}),
     ("P/branches/v1.0", "dir", "add", {}),
     ("P/branches/v1.0/WebContent", "dir", "add", {}),
     ("P/branches/v1.0/WebContent/index.jsp", "file", "add", {"text": b"<html/>\n"})],
    [("P/branches/https:", "dir", "add", {}),
     ("P/branches/https:/10.0.0.1", "dir", "add", {}),
     ("P/branches/https:/10.0.0.1/svn", "dir", "add", {}),
     ("P/branches/https:/10.0.0.1/svn/R", "dir", "add", {}),
     ("P/branches/https:/10.0.0.1/svn/R/P", "dir", "add", {}),
     ("P/branches/https:/10.0.0.1/svn/R/P/branches", "dir", "add", {}),
     ("P/branches/https:/10.0.0.1/svn/R/P/branches/v1.0", "dir", "add", {}),
     (URL_DIR, "dir", "add", {"copyfrom": ("P/branches/v1.0/WebContent", 1)})],
    [("P/branches/v1.1", "dir", "add", {"copyfrom": (URL_DIR, 2)}),
     ("P/branches/https:", "", "delete", {})],
)


def test_a_pasted_url_path_is_renamed_rather_than_crashing_fast_import(tmp_path):
    """Git for Windows refuses `https:` as a path component: `fatal: invalid path`."""
    target, result = _convert_dump(tmp_path, PASTED_URL_HISTORY, FLAT, windows=True)
    assert result.renamed_paths == ["P/branches/https: -> P/branches/https%3A"]

    at_r2 = git(target, "ls-tree", "-r", "--name-only", "refs/remotes/svn/trunk~1")
    assert "P/branches/https%3A/10.0.0.1/svn/R/P/branches/v1.0/WebContent/index.jsp" in at_r2

    # The clean-up copy *from* the renamed path must still find its content.
    at_head = git(target, "ls-tree", "-r", "--name-only", "refs/remotes/svn/trunk")
    assert at_head.splitlines() == ["P/branches/v1.0/WebContent/index.jsp",
                                    "P/branches/v1.1/index.jsp"]
    assert result.unresolved_copies == []


def test_ignore_paths_leaves_matching_paths_out(tmp_path):
    target, result = _convert_dump(tmp_path, PASTED_URL_HISTORY, FLAT, windows=True,
                                   ignore_paths=r"/https:(/|$)")
    assert result.renamed_paths == []
    assert result.ignored_paths > 0
    at_r2 = git(target, "ls-tree", "-r", "--name-only", "refs/remotes/svn/trunk~1")
    assert "https" not in at_r2


def test_a_committed_git_directory_is_renamed_on_every_platform(tmp_path):
    data = _dump([("lib", "dir", "add", {}), ("lib/.git", "dir", "add", {}),
                  ("lib/.git/HEAD", "file", "add", {"text": b"ref: x\n"})])
    target, result = _convert_dump(tmp_path, data, FLAT, windows=False)
    assert git(target, "ls-tree", "-r", "--name-only",
               "refs/remotes/svn/trunk") == "lib/%2Egit/HEAD"
    assert result.renamed_paths == ["lib/.git -> lib/%2Egit"]


def test_a_branch_named_like_a_url_gets_a_valid_ref(tmp_path):
    layout = DetectedLayout(mode=LayoutMode.STANDARD, trunk="trunk",
                            branches=["branches"], tags=["tags"])
    data = _dump(
        [("trunk", "dir", "add", {}), ("branches", "dir", "add", {}),
         ("trunk/a.txt", "file", "add", {"text": b"a\n"})],
        [("branches/https:", "dir", "add", {"copyfrom": ("trunk", 1)})],
    )
    target, result = _convert_dump(tmp_path, data, layout, windows=False)
    assert "refs/remotes/svn/https%3A" in git(target, "for-each-ref", "--format=%(refname)")
    assert "https:" in result.branches


# --------------------------------------------------------------------------- #
# Migrating one folder of a shared repository
# --------------------------------------------------------------------------- #

def _tree(target, ref="refs/remotes/svn/trunk"):
    return git(target, "ls-tree", "-r", "--name-only", ref).splitlines()


def test_the_layout_is_relative_to_the_migrated_folder():
    mapper = LayoutMapper(FLAT, "main", root="cms/main")
    assert mapper.classify("cms/main/src/App.java").subpath == "src/App.java"
    assert mapper.classify("cms/main").is_root
    assert mapper.classify("cms/mainline/x").kind == "outside"
    assert mapper.classify("ITFS/Portal/.git/HEAD").kind == "outside"


def test_only_the_migrated_folder_is_published(tmp_path):
    """Other projects - and the names Git refuses in them - stay unpublished."""
    data = _dump(
        [("ITFS", "dir", "add", {}), ("ITFS/.git", "dir", "add", {}),
         ("ITFS/.git/HEAD", "file", "add", {"text": b"ref\n"}),
         ("TReDS", "dir", "add", {}), ("TReDS/https:", "dir", "add", {}),
         ("TReDS/https:/a.txt", "file", "add", {"text": b"a\n"}),
         ("cms", "dir", "add", {}), ("cms/main", "dir", "add", {}),
         ("cms/main/App.java", "file", "add", {"text": b"class App {}\n"})],
    )
    target, result = _convert_dump(tmp_path, data, FLAT, windows=True, root="cms/main")
    assert _tree(target) == ["App.java"]
    published = git(target, "for-each-ref", "--format=%(refname)", "refs/remotes/")
    assert published.splitlines() == ["refs/remotes/svn/trunk"]
    assert result.renamed_paths == []        # outside renames are not reported


def test_a_folder_moved_into_place_keeps_its_files(tmp_path):
    """`svn mv Old cms` with the folder set to cms/main: the files come along."""
    data = _dump(
        [("Old", "dir", "add", {}), ("Old/main", "dir", "add", {}),
         ("Old/main/a.txt", "file", "add", {"text": b"from old\n"})],
        [("cms", "dir", "add", {"copyfrom": ("Old", 1)}),
         ("Old", "", "delete", {})],
    )
    target, result = _convert_dump(tmp_path, data, FLAT, windows=False, root="cms/main")
    assert _tree(target) == ["a.txt"]
    assert git(target, "show", "refs/remotes/svn/trunk:a.txt") == "from old"
    assert not result.unresolved_copies


def test_a_file_copied_in_from_another_project_keeps_its_content(tmp_path):
    data = _dump(
        [("ITFS", "dir", "add", {}),
         ("ITFS/readme.txt", "file", "add", {"text": b"shared readme\n"}),
         ("cms", "dir", "add", {}), ("cms/main", "dir", "add", {}),
         ("cms/main/App.java", "file", "add", {"text": b"class App {}\n"})],
        [("cms/main/readme.txt", "file", "add", {"copyfrom": ("ITFS/readme.txt", 1)})],
    )
    target, result = _convert_dump(tmp_path, data, FLAT, windows=False, root="cms/main")
    assert _tree(target) == ["App.java", "readme.txt"]
    assert git(target, "show", "refs/remotes/svn/trunk:readme.txt") == "shared readme"
    assert not result.unresolved_copies


def test_paths_differing_only_by_an_unsafe_character_stay_apart(tmp_path):
    data = _dump(
        [("p", "dir", "add", {}), ("p/https:", "dir", "add", {}),
         ("p/https:/a.txt", "file", "add", {"text": b"colon\n"}),
         ("p/https_", "dir", "add", {}),
         ("p/https_/a.txt", "file", "add", {"text": b"underscore\n"})],
    )
    target, _result = _convert_dump(tmp_path, data, FLAT, windows=True)
    assert git(target, "show", "refs/remotes/svn/trunk:p/https%3A/a.txt") == "colon"
    assert git(target, "show", "refs/remotes/svn/trunk:p/https_/a.txt") == "underscore"


def test_branches_differing_only_by_escaping_stay_apart(tmp_path):
    layout = DetectedLayout(mode=LayoutMode.STANDARD, trunk="trunk",
                            branches=["branches"], tags=["tags"])
    data = _dump(
        [("trunk", "dir", "add", {}), ("trunk/f", "file", "add", {"text": b"f\n"}),
         ("branches", "dir", "add", {})],
        [("branches/release 1", "dir", "add", {"copyfrom": ("trunk", 1)}),
         ("branches/release 1/one", "file", "add", {"text": b"1\n"})],
        [("branches/release-1", "dir", "add", {"copyfrom": ("trunk", 1)}),
         ("branches/release-1/dash", "file", "add", {"text": b"2\n"})],
    )
    target, result = _convert_dump(tmp_path, data, layout, windows=False)
    assert _tree(target, "refs/remotes/svn/release%201") == ["f", "one"]
    assert _tree(target, "refs/remotes/svn/release-1") == ["dash", "f"]
    assert sorted(result.branches) == ["main", "release 1", "release-1"]


def test_an_unfollowable_copy_names_its_revision(tmp_path):
    layout = DetectedLayout(mode=LayoutMode.STANDARD, trunk="trunk",
                            branches=["branches"], tags=["tags"])
    data = _dump(
        [("trunk", "dir", "add", {}), ("vendor", "dir", "add", {}),
         ("vendor/x.txt", "file", "add", {"text": b"x\n"})],
        [("trunk/x.txt", "file", "add", {"copyfrom": ("vendor/x.txt", 1)})],
    )
    _target, result = _convert_dump(tmp_path, data, layout, windows=False)
    assert result.unresolved_copies == ["r2: trunk/x.txt <- vendor/x.txt@1"]


def test_a_fast_import_failure_reports_git_s_own_reason(tmp_path):
    """Not `[Errno 22] Invalid argument`, and not "resolving a copy"."""
    from svn2gitlab.convert.fastimport import FastImport

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(ConversionError) as caught:
        with FastImport("git", tmp_path) as importer:
            blob = importer.blob(b"x")
            importer.begin_commit("refs/heads/main", ("a", "a@x", "0 +0000"),
                                  ("a", "a@x", "0 +0000"), b"m\n")
            importer.filemodify("100644", f":{blob}", "lib/.git/HEAD")
            importer.end_commit()
            importer.ls("refs/heads/main", "lib")
    assert "invalid path" in str(caught.value)


@pytest.mark.parametrize("help_text,expected", [
    ("--source-trust-server-cert-failures ARG", "--source-trust-server-cert-failures="),
    ("--source-trust-server-cert : accept", "--source-trust-server-cert"),
    ("--trust-server-cert : deprecated", "--trust-server-cert"),
    ("nothing relevant", None),
])
def test_svnsync_is_given_a_trust_option_it_knows(monkeypatch, help_text, expected):
    from svn2gitlab.svn import acquire

    monkeypatch.setattr(acquire, "_help_text", lambda exe, sub: help_text)
    args = acquire._trust_args("svnsync", "initialize", "source")
    if expected is None:
        assert args == []
    else:
        assert len(args) == 1 and args[0].startswith(expected)

