"""Author mapping: parsing, normalisation, generation policy."""

from __future__ import annotations

import pytest

from svn2gitlab.config import AuthorPolicy, AuthorsConfig
from svn2gitlab.errors import ConfigError
from svn2gitlab.svn.authors import (AuthorMap, Identity, build_author_map, humanise,
                                    normalise_username, unmapped)


def test_roundtrip_through_a_file(tmp_path):
    amap = AuthorMap()
    amap.set("jsmith", Identity("John Smith", "john.smith@acme.com"))
    amap.set("CONTOSO\\bwilliams", Identity("Beth Williams", "beth@acme.com"))
    path = tmp_path / "authors.txt"
    amap.save(path, header=["a comment"])

    loaded = AuthorMap.load(path)
    assert len(loaded) == 2
    assert str(loaded.get("jsmith")) == "John Smith <john.smith@acme.com>"
    assert loaded.get("CONTOSO\\bwilliams").email == "beth@acme.com"


def test_lookup_is_case_insensitive():
    amap = AuthorMap()
    amap.set("JSmith", Identity("John Smith", "js@acme.com"))
    assert "jsmith" in amap
    assert amap.get("JSMITH").email == "js@acme.com"


def test_comments_and_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "authors.txt"
    path.write_text("# a comment\n\njsmith = John Smith <js@acme.com>\n", encoding="utf-8")
    assert len(AuthorMap.load(path)) == 1


def test_malformed_lines_are_reported_but_do_not_lose_good_ones(tmp_path):
    path = tmp_path / "authors.txt"
    path.write_text("good = Good Person <g@acme.com>\nthis line is broken\n", encoding="utf-8")
    amap = AuthorMap.load(path)
    assert len(amap) == 1
    with pytest.raises(ConfigError):
        AuthorMap.load(path, strict=True)


def test_git_svn_file_has_no_comments_or_padding(tmp_path):
    """git-svn's parser is stricter than ours; its file must be plain."""
    amap = AuthorMap()
    amap.set("a", Identity("A", "a@x.com"))
    amap.set("bbbbbbbb", Identity("B", "b@x.com"))
    path = amap.write_git_svn_file(tmp_path / "git-svn-authors.txt")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines == ["a = A <a@x.com>", "bbbbbbbb = B <b@x.com>"]


@pytest.mark.parametrize("raw,expected", [
    ("CONTOSO\\bwilliams", "bwilliams"),
    ("jsmith@acme.local", "jsmith"),
    ("plain", "plain"),
    ("", ""),
])
def test_username_normalisation(raw, expected):
    assert normalise_username(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("john.smith", "John Smith"),
    ("a_jones", "A Jones"),
    ("bwilliams", "Bwilliams"),
])
def test_humanise(raw, expected):
    assert humanise(raw) == expected


def test_generate_policy_synthesises_addresses():
    config = AuthorsConfig(default_domain="acme.com", policy=AuthorPolicy.GENERATE)
    amap, synthesised = build_author_map(["jsmith", "a.jones"], config)
    assert set(synthesised) == {"jsmith", "a.jones"}
    assert amap.get("a.jones").email == "a.jones@acme.com"


def test_strict_policy_leaves_authors_unmapped_for_the_caller_to_reject():
    config = AuthorsConfig(policy=AuthorPolicy.STRICT)
    amap, synthesised = build_author_map(["jsmith"], config)
    assert synthesised == []
    assert unmapped(["jsmith"], amap) == ["jsmith"]


def test_explicit_mapping_wins_over_generation():
    config = AuthorsConfig(default_domain="acme.com",
                           mapping={"jsmith": "John Smith <john@real.com>"})
    amap, synthesised = build_author_map(["jsmith"], config)
    assert amap.get("jsmith").email == "john@real.com"
    assert "jsmith" not in synthesised


def test_bare_email_in_mapping_is_accepted():
    config = AuthorsConfig(mapping={"jsmith": "john@real.com"})
    amap, _ = build_author_map(["jsmith"], config)
    assert amap.get("jsmith").email == "john@real.com"
    assert amap.get("jsmith").name == "Jsmith"


def test_invalid_mapping_value_is_rejected():
    config = AuthorsConfig(mapping={"jsmith": "not an identity"})
    with pytest.raises(ConfigError, match="valid identity"):
        build_author_map(["jsmith"], config)


def test_authorless_revisions_get_the_git_svn_key():
    """git-svn looks up authorless revisions under the literal key '(no author)'."""
    config = AuthorsConfig(default_domain="acme.com", no_author_name="svn")
    amap, _ = build_author_map(["", "jsmith"], config)
    assert "(no author)" in amap
    assert amap.get("(no author)").email == "svn@acme.com"


def test_existing_entries_are_preserved_across_regeneration():
    existing = AuthorMap()
    existing.set("jsmith", Identity("John Smith", "john@real.com"))
    config = AuthorsConfig(default_domain="acme.com")
    amap, synthesised = build_author_map(["jsmith", "newcomer"], config, existing)
    assert amap.get("jsmith").email == "john@real.com"
    assert synthesised == ["newcomer"]


def test_implausible_and_duplicate_emails_are_detectable():
    amap = AuthorMap()
    amap.set("a", Identity("A", "not-an-email"))
    amap.set("b", Identity("B", "shared@acme.com"))
    amap.set("c", Identity("C", "shared@acme.com"))
    assert [u for u, _ in amap.invalid_emails()] == ["a"]
    assert amap.duplicate_emails() == {"shared@acme.com": ["b", "c"]}


def test_loading_a_missing_file_yields_an_empty_map(tmp_path):
    assert len(AuthorMap.load(tmp_path / "absent.txt")) == 0
