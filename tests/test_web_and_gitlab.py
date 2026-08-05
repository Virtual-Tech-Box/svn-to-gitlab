"""Web API surface and the GitLab client's error translation.

The GitLab tests use a stubbed transport rather than a live instance: what matters
is that an HTTP status becomes an error a human can act on, and that we never leak a
token into a message or a log.
"""

from __future__ import annotations

import json

import pytest
import requests

from svn2gitlab.config import TargetConfig, Visibility
from svn2gitlab.errors import GitLabError
from svn2gitlab.gitlab.api import GitLabClient, Project
from svn2gitlab.logging_setup import REDACTOR, Redactor

# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

def test_registered_secrets_are_scrubbed():
    redactor = Redactor()
    redactor.add("glpat-supersecretvalue")
    assert "glpat-supersecretvalue" not in redactor.scrub("token=glpat-supersecretvalue end")


def test_credentials_in_urls_are_scrubbed_even_when_unregistered():
    redactor = Redactor()
    scrubbed = redactor.scrub("pushing to https://oauth2:glpat-abc123xyz@gitlab.com/g/p.git")
    assert "glpat-abc123xyz" not in scrubbed
    assert "gitlab.com/g/p.git" in scrubbed


def test_password_arguments_are_scrubbed():
    redactor = Redactor()
    assert "hunter2" not in redactor.scrub("svn info --username bob --password hunter2")
    assert "hunter2" not in redactor.scrub("svn info --password=hunter2")


def test_private_token_headers_are_scrubbed():
    redactor = Redactor()
    assert "abc123def456" not in redactor.scrub("PRIVATE-TOKEN: abc123def456")


def test_longer_secrets_are_scrubbed_first():
    """Overlapping secrets must not leave a fragment behind."""
    redactor = Redactor()
    redactor.add("secret")
    redactor.add("secretvalue")
    assert "value" not in redactor.scrub("secretvalue")


# --------------------------------------------------------------------------- #
# GitLab error translation
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise json.JSONDecodeError("empty", "", 0)
        return self._body


@pytest.fixture
def client(monkeypatch):
    target = TargetConfig(gitlab_url="https://gitlab.example.com", token="glpat-testtoken",
                          namespace="grp", project="proj")
    return GitLabClient(target)


def stub(client, response):
    client.session.request = lambda *args, **kwargs: response


@pytest.mark.parametrize("status,fragment,remedy_fragment", [
    (401, "rejected the access token", "expired or revoked"),
    (403, "denied the request", "Maintainer"),
    (404, "404", "cannot see it"),
])
def test_http_errors_become_actionable_messages(client, status, fragment, remedy_fragment):
    stub(client, FakeResponse(status, {"message": "boom"}))
    with pytest.raises(GitLabError) as excinfo:
        client.request("GET", "projects/1", retries=0)
    assert fragment in excinfo.value.message
    assert remedy_fragment in excinfo.value.remedy


def test_taken_path_suggests_the_right_fix(client):
    stub(client, FakeResponse(400, {"message": "path has already been taken"}))
    with pytest.raises(GitLabError) as excinfo:
        client.request("POST", "projects", retries=0)
    assert "already in use" in excinfo.value.message
    assert "allow_non_empty_project" in excinfo.value.remedy


def test_tls_failure_points_at_the_ca_bundle_setting(client):
    def raise_ssl(*args, **kwargs):
        raise requests.exceptions.SSLError("certificate verify failed")
    client.session.request = raise_ssl
    with pytest.raises(GitLabError) as excinfo:
        client.request("GET", "user", retries=0)
    assert "ca_bundle" in excinfo.value.remedy


def test_connection_failure_mentions_proxy_and_firewall(client):
    def raise_conn(*args, **kwargs):
        raise requests.exceptions.ConnectionError("no route to host")
    client.session.request = raise_conn
    with pytest.raises(GitLabError) as excinfo:
        client.request("GET", "user", retries=0)
    assert "proxy or firewall" in excinfo.value.remedy


def test_404_on_namespace_lookup_returns_none_rather_than_raising(client):
    stub(client, FakeResponse(404, {"message": "404 Not Found"}))
    assert client.find_namespace("nope/missing") is None
    assert client.find_project("nope/missing") is None


def test_non_empty_project_is_refused_by_default(client, monkeypatch):
    project = Project(id=1, path_with_namespace="grp/proj", web_url="", http_url_to_repo="",
                      ssh_url_to_repo="", default_branch="main", empty_repo=False,
                      visibility="private", lfs_enabled=True, statistics={})
    monkeypatch.setattr(client, "find_project", lambda *a, **k: project)
    with pytest.raises(GitLabError, match="already contains commits"):
        client.ensure_project(client.config, "main")


def test_non_empty_project_is_allowed_when_opted_in(client, monkeypatch):
    project = Project(id=1, path_with_namespace="grp/proj", web_url="", http_url_to_repo="",
                      ssh_url_to_repo="", default_branch="main", empty_repo=False,
                      visibility="private", lfs_enabled=True, statistics={})
    monkeypatch.setattr(client, "find_project", lambda *a, **k: project)
    client.config.allow_non_empty_project = True
    assert client.ensure_project(client.config, "main") is project


def test_push_url_embeds_the_token_and_registers_it_for_redaction(client):
    project = Project(id=1, path_with_namespace="grp/proj", web_url="",
                      http_url_to_repo="https://gitlab.example.com/grp/proj.git",
                      ssh_url_to_repo="", default_branch="main", empty_repo=True,
                      visibility="private", lfs_enabled=True, statistics={})
    url = client.push_url(project)
    assert "glpat-testtoken" in url
    assert REDACTOR.scrub(f"pushing to {url}") .count("glpat-testtoken") == 0


def test_explicit_remote_url_bypasses_the_api(client):
    client.config.remote_url = "git@gitlab.example.com:grp/proj.git"
    project = Project(id=1, path_with_namespace="grp/proj", web_url="", http_url_to_repo="",
                      ssh_url_to_repo="", default_branch="main", empty_repo=True,
                      visibility="private", lfs_enabled=True, statistics={})
    assert client.push_url(project) == "git@gitlab.example.com:grp/proj.git"


# --------------------------------------------------------------------------- #
# Push failure diagnosis
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("output,fragment", [
    ("! [remote rejected] main -> main (non-fast-forward)", "force-push"),
    ("remote: GitLab: pre-receive hook declined", "push rules"),
    ("remote: fatal: file is too large", "lfs.enabled"),
    ("fatal: Authentication failed for 'https://gitlab.example.com'", "write_repository"),
])
def test_push_failures_are_explained(output, fragment):
    from svn2gitlab.convert.git import Git
    from svn2gitlab.gitlab.push import Publisher
    from svn2gitlab.tools import detect_tools

    tools = detect_tools()
    if not tools.git.available:
        pytest.skip("git is required")
    target = TargetConfig(gitlab_url="https://gitlab.example.com", token="t", project="p")
    publisher = Publisher.__new__(Publisher)
    publisher.target = target
    error = publisher._translate_push_failure(1, output, forced=False)
    assert fragment in (error.remedy + error.message)


# --------------------------------------------------------------------------- #
# Web API
# --------------------------------------------------------------------------- #

@pytest.fixture
def web_client(migration_config):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from svn2gitlab.web.server import create_app

    try:
        return fastapi_testclient.TestClient(create_app(migration_config))
    except RuntimeError as exc:
        # starlette imports fine but raises at construction when its HTTP transport
        # (httpx or httpx2, depending on the starlette version) is absent. Skipping
        # with the real reason beats a wall of identical stack traces.
        pytest.skip(f"starlette TestClient is unusable in this environment: {exc}")


def test_dashboard_page_is_served(web_client):
    response = web_client.get("/")
    assert response.status_code == 200
    assert "svn2gitlab" in response.text


def test_meta_reports_config_and_tool_state(web_client):
    meta = web_client.get("/api/meta").json()
    assert meta["config_loaded"] is True
    assert meta["name"] == "testrepo"
    assert "git" in meta["tools"]


def test_repositories_are_listed(web_client):
    repos = web_client.get("/api/repositories").json()
    assert len(repos) == 1
    assert repos[0]["name"] == "testrepo"
    assert repos[0]["target"] == "testgroup/testrepo"


def test_config_endpoint_redacts_the_token(web_client):
    text = web_client.get("/api/config").text
    assert "not-a-real-token" not in text
    assert "***" in text


def test_unknown_action_is_rejected(web_client):
    response = web_client.post("/api/repositories/testrepo/run", json={"kind": "rm-rf"})
    assert response.status_code == 400


def test_report_endpoint_404s_before_a_migration_runs(web_client):
    assert web_client.get("/api/repositories/testrepo/report").status_code == 404


def test_unknown_repository_report_is_404(web_client):
    assert web_client.get("/api/repositories/ghost/report").status_code == 404


def test_logs_endpoint_returns_a_cursor(web_client):
    body = web_client.get("/api/logs").json()
    assert "entries" in body
    assert "last" in body
