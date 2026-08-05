"""Configuration loading, validation, inheritance and secret handling."""

from __future__ import annotations

import pytest

from svn2gitlab.config import (AuthorPolicy, LayoutMode, MigrationConfig, dump_config,
                               load_config, resolve_secret)
from svn2gitlab.errors import ConfigError


def write(tmp_path, text):
    path = tmp_path / "migration.yaml"
    path.write_text(text, encoding="utf-8")
    return path


MINIMAL = """
version: 1
source:
  url: https://svn.example.com/svn/repo
target:
  gitlab_url: https://gitlab.com
  token: tok-abcdef
  project: myproject
"""


def test_minimal_config_creates_one_repository(tmp_path):
    config = load_config(write(tmp_path, MINIMAL))
    assert len(config.repositories) == 1
    resolved = config.all_resolved()[0]
    assert resolved.source.url == "https://svn.example.com/svn/repo"
    assert resolved.target.project == "myproject"
    assert resolved.convert.default_branch == "main"


def test_trailing_slash_is_stripped_from_svn_url(tmp_path):
    config = load_config(write(tmp_path, MINIMAL.replace("svn/repo", "svn/repo/")))
    assert config.all_resolved()[0].source.url.endswith("svn/repo")


def test_credentials_embedded_in_the_url_are_moved_out(tmp_path):
    """Credentials in the URL would end up in every log line and remote we create."""
    text = MINIMAL.replace("https://svn.example.com/svn/repo",
                           "https://user:secret@svn.example.com/svn/repo")
    config = load_config(write(tmp_path, text))
    url = config.all_resolved()[0].source.url
    assert "secret" not in url
    assert "user" not in url
    assert url == "https://svn.example.com/svn/repo"


def test_unsupported_scheme_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="scheme"):
        load_config(write(tmp_path, MINIMAL.replace("https://svn", "ftp://svn")))


def test_source_needs_url_or_local_path(tmp_path):
    text = """
version: 1
source:
  username: bob
target:
  gitlab_url: https://gitlab.com
  token: t
  project: p
"""
    with pytest.raises(ConfigError, match="url"):
        load_config(write(tmp_path, text))


def test_target_needs_a_token(tmp_path):
    text = """
version: 1
source:
  url: https://svn.example.com/repo
target:
  gitlab_url: https://gitlab.com
  project: p
"""
    with pytest.raises(ConfigError, match="token"):
        load_config(write(tmp_path, text))


def test_unknown_key_is_rejected_rather_than_ignored(tmp_path):
    """A typo in a key must fail loudly, not silently disable a feature."""
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, MINIMAL + "\nconvert:\n  defualt_branch: main\n"))


def test_env_secret_indirection(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "glpat-supersecret")
    config = load_config(write(tmp_path, MINIMAL.replace("tok-abcdef", "env:MY_TOKEN")))
    assert config.all_resolved()[0].target.token == "glpat-supersecret"


def test_missing_env_secret_names_the_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("ABSENT_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="ABSENT_TOKEN"):
        load_config(write(tmp_path, MINIMAL.replace("tok-abcdef", "env:ABSENT_TOKEN")))


def test_file_secret_indirection(tmp_path):
    secret = tmp_path / "token.txt"
    secret.write_text("  file-token\n", encoding="utf-8")
    config = load_config(write(tmp_path, MINIMAL.replace("tok-abcdef", f"file:{secret}")))
    assert config.all_resolved()[0].target.token == "file-token"


def test_resolve_secret_passes_through_plain_values():
    assert resolve_secret("plain") == "plain"
    assert resolve_secret(None) is None


def test_repository_entries_inherit_and_override(tmp_path):
    text = """
version: 1
name: batch
source:
  url: https://svn.example.com/svn/root
  username: shared
target:
  gitlab_url: https://gitlab.com
  token: t
  namespace: acme
convert:
  default_branch: main
repositories:
  - name: alpha
    source: {subpath: alpha}
    target: {project: alpha}
  - name: beta
    source: {subpath: beta, username: special}
    target: {project: beta, visibility: internal}
    convert: {default_branch: trunk-main}
"""
    config = load_config(write(tmp_path, text))
    alpha, beta = config.all_resolved()

    # Inherited
    assert alpha.source.username == "shared"
    assert alpha.convert.default_branch == "main"
    assert alpha.target.namespace == "acme"
    # Overridden
    assert beta.source.username == "special"
    assert beta.convert.default_branch == "trunk-main"
    assert beta.target.visibility.value == "internal"
    # Untouched siblings keep the inherited value
    assert alpha.target.visibility.value == "private"


def test_duplicate_repository_names_are_rejected(tmp_path):
    text = MINIMAL + """
repositories:
  - name: same
  - name: Same
"""
    with pytest.raises(ConfigError, match="[Dd]uplicate"):
        load_config(write(tmp_path, text))


def test_selecting_an_unknown_repository_lists_the_known_ones(tmp_path):
    config = load_config(write(tmp_path, MINIMAL))
    with pytest.raises(ConfigError, match="no such repository"):
        config.all_resolved(["nope"])


def test_custom_layout_requires_paths(tmp_path):
    text = MINIMAL + "\n  layout:\n    mode: custom\n"
    # Indentation places layout under `source`.
    text = MINIMAL.replace("  url: https://svn.example.com/svn/repo",
                           "  url: https://svn.example.com/svn/repo\n"
                           "  layout:\n    mode: custom")
    with pytest.raises(ConfigError, match="custom"):
        load_config(write(tmp_path, text))


def test_invalid_ignore_path_regex_is_caught_at_load_time(tmp_path):
    text = MINIMAL.replace(
        "  url: https://svn.example.com/svn/repo",
        "  url: https://svn.example.com/svn/repo\n  layout:\n    ignore_paths: '['")
    with pytest.raises(ConfigError, match="regex"):
        load_config(write(tmp_path, text))


def test_invalid_default_branch_is_rejected(tmp_path):
    text = MINIMAL + "\nconvert:\n  default_branch: 'bad branch'\n"
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_dump_redacts_secrets_by_default(tmp_path):
    config = load_config(write(tmp_path, MINIMAL))
    dumped = dump_config(config)
    assert "tok-abcdef" not in dumped
    assert "***" in dumped
    assert "tok-abcdef" in dump_config(config, redact=False)


def test_workdir_is_resolved_relative_to_the_config_file(tmp_path):
    config = load_config(write(tmp_path, MINIMAL + "\nworkdir: ./relative-work\n"))
    assert config.workdir_path() == (tmp_path / "relative-work").resolve()


def test_local_path_produces_a_file_url(tmp_path):
    repo = tmp_path / "somerepo"
    repo.mkdir()
    text = f"""
version: 1
source:
  local_path: {repo.as_posix()}
target:
  gitlab_url: https://gitlab.com
  token: t
  project: p
"""
    config = load_config(write(tmp_path, text))
    assert config.all_resolved()[0].source.effective_url().startswith("file://")


def test_unsupported_version_is_reported_clearly(tmp_path):
    with pytest.raises(ConfigError, match="version"):
        load_config(write(tmp_path, MINIMAL.replace("version: 1", "version: 99")))


def test_missing_file_suggests_init(tmp_path):
    with pytest.raises(ConfigError, match="init"):
        load_config(tmp_path / "nope.yaml")
