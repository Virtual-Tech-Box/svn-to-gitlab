"""Shared fixtures.

The end-to-end tests build a real Subversion repository and convert it. That needs
git, an svn client, and a conversion engine - either svnadmin for the native
converter or git-svn for the legacy one. Where those are absent the tests skip
rather than fail, so the unit tests still run on a bare CI image.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from svn2gitlab.tools import detect_tools  # noqa: E402


def _run(args, cwd=None, env=None):
    merged = os.environ.copy()
    merged.update(env or {})
    result = subprocess.run(args, cwd=cwd, env=merged, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, args))} failed:\n{result.stderr}")
    return result.stdout


@pytest.fixture(scope="session")
def tools():
    return detect_tools(refresh=True)


@pytest.fixture(scope="session")
def svn_available(tools):
    return tools.svn.available and tools.svnadmin.available


def _can_convert(tools) -> bool:
    """Whether this machine can convert a repository at all.

    Either engine will do: the native converter needs svnadmin, git-svn needs Perl.
    Requiring git-svn here would skip the entire end-to-end suite on Windows, which
    is precisely the platform the native engine exists to serve.
    """
    if not (tools.git.available and tools.svn.available):
        return False
    return tools.svnadmin.available or tools.git_svn.available


@pytest.fixture(scope="session")
def conversion_available(tools):
    return _can_convert(tools)


requires_svn = pytest.mark.skipif(
    not (detect_tools().svn.available and detect_tools().svnadmin.available),
    reason="svn and svnadmin are required",
)

requires_conversion = pytest.mark.skipif(
    not _can_convert(detect_tools()),
    reason="git, svn and a conversion engine (svnadmin or git-svn) are required",
)


def make_svn_repo(root: Path, tools) -> Path:
    """Create a small standard-layout repository with branches, tags and authors."""
    repo = root / "repo"
    work = root / "wc"
    svn = tools.svn.path
    svnadmin = tools.svnadmin.path

    _run([svnadmin, "create", str(repo)])
    hook = repo / "hooks" / ("pre-revprop-change.bat" if os.name == "nt"
                             else "pre-revprop-change")
    if os.name == "nt":
        hook.write_text("@ECHO OFF\r\nEXIT /B 0\r\n", encoding="ascii")
    else:
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)

    url = repo.as_uri()
    _run([svn, "mkdir", "-q", "-m", "layout", f"{url}/trunk", f"{url}/branches", f"{url}/tags"])
    _run([svn, "checkout", "-q", f"{url}/trunk", str(work)])

    (work / "src").mkdir()
    # write_bytes, not write_text: on Windows text mode rewrites \n to \r\n, which
    # would make the fixture repository - and therefore every converted tree - differ
    # by platform. The golden master caught exactly that.
    (work / "src" / "app.py").write_bytes(b"print('hello')\n")
    (work / "binary.dat").write_bytes(bytes(range(256)) * 64)
    _run([svn, "add", "-q", "src", "binary.dat"], cwd=work)
    _run([svn, "propset", "-q", "svn:ignore", "*.pyc\nbuild/", "."], cwd=work)
    _run([svn, "commit", "-q", "-m", "Initial import"], cwd=work)

    (work / "src" / "app.py").write_bytes(b"print('hello, world')\n")
    _run([svn, "commit", "-q", "-m", "Improve the greeting"], cwd=work)

    _run([svn, "copy", "-q", "-m", "branch", f"{url}/trunk", f"{url}/branches/feature-a"])
    _run([svn, "copy", "-q", "-m", "tag 1.0", f"{url}/trunk", f"{url}/tags/v1.0"])

    (work / "LICENSE").write_bytes(b"MIT\n")
    _run([svn, "add", "-q", "LICENSE"], cwd=work)
    _run([svn, "commit", "-q", "-m", "Add the licence"], cwd=work)

    youngest = int(_run([tools.svnlook.path, "youngest", str(repo)]).strip())
    for rev in range(1, youngest + 1):
        author = ["alice", "bob", "CONTOSO\\carol"][rev % 3]
        _run([svn, "propset", "-q", "--revprop", "-r", str(rev), "svn:author", author, url])
    return repo


@pytest.fixture
def svn_repo(tmp_path, tools, svn_available):
    if not svn_available:
        pytest.skip("svn and svnadmin are required")
    return make_svn_repo(tmp_path, tools)


@pytest.fixture
def migration_config(tmp_path, svn_repo):
    """A complete, valid config pointing at the fixture repository."""
    config = tmp_path / "migration.yaml"
    config.write_text(f"""
version: 1
name: testrepo
workdir: {(tmp_path / 'work').as_posix()}
source:
  local_path: {svn_repo.as_posix()}
  layout:
    mode: auto
authors:
  file: {(tmp_path / 'authors.txt').as_posix()}
  default_domain: example.com
  policy: generate
convert:
  default_branch: main
  strip_svn_metadata: true
  generate_gitignore: true
target:
  gitlab_url: https://gitlab.example.com
  token: not-a-real-token
  namespace: testgroup
  project: testrepo
verify:
  mode: full
  verify_all_branches: true
""", encoding="utf-8")
    return config
