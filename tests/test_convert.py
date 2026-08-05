"""Ref sanitisation, commit-message rewriting and LFS planning."""

from __future__ import annotations

import io

import pytest

from svn2gitlab.convert.fastfilter import FilterStats, _StreamFilter, rewrite_message
from svn2gitlab.convert.lfs import _batch_patterns, _derive_patterns, parse_pointer
from svn2gitlab.svn.analyze import sanitize_ref_name

# --------------------------------------------------------------------------- #
# Ref names
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("feature/login", "feature/login"),
    ("release 1.0", "release-1.0"),
    ("bad~name", "bad-name"),
    ("bad^name", "bad-name"),
    ("bad:name", "bad-name"),
    ("with..dots", "with-dots"),
    ("trailing.lock", "trailing-lock"),
    ("//double//slash//", "double/slash"),
    (".hidden", "hidden"),
    ("   ", "unnamed"),
])
def test_sanitize_ref_name(raw, expected):
    assert sanitize_ref_name(raw) == expected


def test_sanitized_names_are_valid_git_refs():
    """Every sanitised name must survive git's own rules."""
    for raw in ["a b", "x~1", "q?", "*star", "back\\slash", "end.lock", "..", "@{now}"]:
        cleaned = sanitize_ref_name(raw)
        assert cleaned
        assert not any(c in cleaned for c in " ~^:?*[\\")
        assert ".." not in cleaned
        assert not cleaned.endswith(".lock")
        assert not cleaned.startswith(".")


# --------------------------------------------------------------------------- #
# Commit message rewriting
# --------------------------------------------------------------------------- #

SVN_ID = (b"Fix the thing\n\n"
          b"git-svn-id: https://svn.example.com/repo/trunk@1234 "
          b"deadbeef-1234-0410-9876-abcdef123456\n")


def test_svn_id_is_removed_and_replaced_with_a_revision_trailer():
    stats = FilterStats()
    result = rewrite_message(SVN_ID, strip_svn_id=True, revision_trailer=True, stats=stats)
    assert b"git-svn-id" not in result
    assert b"Svn-Revision: 1234" in result
    assert result.startswith(b"Fix the thing")
    assert stats.messages_rewritten == 1
    assert stats.trailers_added == 1


def test_trailer_can_be_omitted():
    stats = FilterStats()
    result = rewrite_message(SVN_ID, strip_svn_id=True, revision_trailer=False, stats=stats)
    assert b"Svn-Revision" not in result
    assert result.strip() == b"Fix the thing"


def test_messages_without_svn_id_are_untouched():
    stats = FilterStats()
    original = b"A normal message\n"
    assert rewrite_message(original, True, True, stats) is original
    assert stats.messages_rewritten == 0


def test_a_message_that_was_only_an_svn_id_does_not_become_empty():
    """fast-import rejects a zero-length commit message."""
    stats = FilterStats()
    only_id = b"git-svn-id: https://svn.example.com/repo/trunk@7 uuid-here\n"
    result = rewrite_message(only_id, strip_svn_id=True, revision_trailer=False, stats=stats)
    assert result.strip()


def test_disabling_the_strip_leaves_the_message_alone():
    stats = FilterStats()
    assert rewrite_message(SVN_ID, strip_svn_id=False, revision_trailer=True, stats=stats) is SVN_ID


# --------------------------------------------------------------------------- #
# fast-import stream filtering
# --------------------------------------------------------------------------- #

def run_filter(stream: bytes, strip: bool = True, trailer: bool = True):
    filt = _StreamFilter(strip_svn_id=strip, revision_trailer=trailer)
    source = io.BytesIO(stream)
    sink = io.BytesIO()
    filt.run(source, sink)
    return sink.getvalue(), filt.stats


def build_commit(message: bytes) -> bytes:
    return (b"commit refs/heads/main\n"
            b"mark :1\n"
            b"author A <a@x> 1600000000 +0000\n"
            b"committer A <a@x> 1600000000 +0000\n"
            b"data %d\n" % len(message) + message +
            b"M 100644 abc123 file.txt\n\n")


def test_stream_filter_rewrites_commit_messages():
    out, stats = run_filter(build_commit(SVN_ID))
    assert b"git-svn-id" not in out
    assert b"Svn-Revision: 1234" in out
    assert stats.commits == 1
    # The byte count must be updated to match the new payload.
    length = int(out.split(b"data ")[1].split(b"\n")[0])
    payload_start = out.index(b"data ") + len(b"data %d\n" % length)
    assert len(out[payload_start:payload_start + length]) == length


def test_stream_filter_preserves_structure_and_trailing_commands():
    out, _stats = run_filter(build_commit(SVN_ID))
    assert out.startswith(b"commit refs/heads/main\n")
    assert b"M 100644 abc123 file.txt\n" in out
    assert b"author A <a@x> 1600000000 +0000\n" in out


def test_stream_filter_does_not_touch_blob_payloads():
    """A blob whose bytes happen to contain the trailer must survive intact."""
    blob_body = b"git-svn-id: https://svn.example.com/repo/trunk@99 uuid\n"
    stream = (b"blob\nmark :2\ndata %d\n" % len(blob_body)) + blob_body + b"\n"
    out, stats = run_filter(stream)
    assert blob_body in out
    assert stats.messages_rewritten == 0


def test_stream_filter_handles_annotated_tags():
    tag_message = b"Tag 1.0\n\ngit-svn-id: https://svn.example.com/repo/tags/v1@50 uuid\n"
    stream = (b"tag v1.0\nfrom :1\ntagger A <a@x> 1600000000 +0000\n"
              b"data %d\n" % len(tag_message)) + tag_message
    out, stats = run_filter(stream)
    assert b"git-svn-id" not in out
    assert stats.tags == 1
    assert stats.messages_rewritten == 1


def test_stream_filter_is_deterministic():
    """Re-derivation depends on identical input producing identical output."""
    stream = build_commit(SVN_ID) + build_commit(b"Second commit\n")
    first, _ = run_filter(stream)
    second, _ = run_filter(stream)
    assert first == second


# --------------------------------------------------------------------------- #
# LFS
# --------------------------------------------------------------------------- #

POINTER = (b"version https://git-lfs.github.com/spec/v1\n"
           b"oid sha256:" + b"a" * 64 + b"\n"
           b"size 12345\n")


def test_lfs_pointer_is_parsed():
    parsed = parse_pointer(POINTER)
    assert parsed == ("a" * 64, 12345)


def test_non_pointer_content_is_rejected():
    assert parse_pointer(b"just a file\n") is None
    assert parse_pointer(b"version https://git-lfs.github.com/spec/v1\nno oid\n") is None


def test_patterns_prefer_wildcards_for_common_extensions():
    paths = {"a/one.zip", "b/two.zip", "c/three.zip", "d/lonely.psd"}
    patterns = _derive_patterns(paths, wanted_ext=set(), wanted_paths=[])
    assert "*.zip" in patterns
    # A single .psd is listed literally rather than sweeping every .psd into LFS.
    assert "d/lonely.psd" in patterns
    assert "*.psd" not in patterns


def test_explicitly_wanted_extensions_always_become_wildcards():
    patterns = _derive_patterns({"one.psd"}, wanted_ext={"psd"}, wanted_paths=[])
    assert "*.psd" in patterns


def test_files_without_an_extension_are_listed_literally():
    patterns = _derive_patterns({"bin/tool"}, wanted_ext=set(), wanted_paths=[])
    assert "bin/tool" in patterns


def test_pattern_batching_respects_the_command_line_limit():
    patterns = [f"*.ext{i:04d}" for i in range(2000)]
    batches = _batch_patterns(patterns, max_chars=200)
    assert sum(len(b) for b in batches) == len(patterns)
    for batch in batches:
        assert len(",".join(batch)) <= 200 + max(len(p) for p in patterns)
