"""GitLab REST v4 client.

Written against the subset of the API a migration needs, and equally happy talking
to gitlab.com SaaS or a self-managed instance behind a corporate CA. The parts that
earn their keep in the field:

* rate-limit awareness - GitLab.com returns 429 with `RateLimit-Reset`, and a batch
  migration of 200 repositories will hit it;
* precise permission diagnostics - "403" is useless, "your token has `read_api` but
  needs `api` to create projects" is actionable;
* namespace creation that walks a nested path (`acme/legacy/tools`) creating only
  the groups that are missing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin

import requests

from ..config import TargetConfig, Visibility
from ..errors import GitLabError
from ..logging_setup import REDACTOR, get_logger

log = get_logger("gitlab")

DEFAULT_RETRIES = 4
RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class Project:
    id: int
    path_with_namespace: str
    web_url: str
    http_url_to_repo: str
    ssh_url_to_repo: str
    default_branch: str
    empty_repo: bool
    visibility: str
    lfs_enabled: bool
    statistics: Dict[str, Any]

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Project":
        return cls(
            id=data.get("id", 0),
            path_with_namespace=data.get("path_with_namespace", ""),
            web_url=data.get("web_url", ""),
            http_url_to_repo=data.get("http_url_to_repo", ""),
            ssh_url_to_repo=data.get("ssh_url_to_repo", ""),
            default_branch=data.get("default_branch") or "",
            empty_repo=bool(data.get("empty_repo", False)),
            visibility=data.get("visibility", ""),
            lfs_enabled=bool(data.get("lfs_enabled", True)),
            statistics=data.get("statistics") or {},
        )


@dataclass
class Namespace:
    id: int
    full_path: str
    kind: str


class GitLabClient:
    def __init__(self, config: TargetConfig) -> None:
        self.config = config
        self.base_url = config.gitlab_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/v4/"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "svn2gitlab/1.0",
            "Accept": "application/json",
        })
        if config.token:
            REDACTOR.add(config.token)
            if config.token_kind == "oauth2":
                self.session.headers["Authorization"] = f"Bearer {config.token}"
            elif config.token_kind == "job":
                self.session.headers["JOB-TOKEN"] = config.token
            else:
                self.session.headers["PRIVATE-TOKEN"] = config.token
        if config.ca_bundle:
            self.session.verify = str(Path(config.ca_bundle).expanduser())
        elif not config.verify_tls:
            self.session.verify = False
            import urllib3  # noqa: PLC0415
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._cached_user: Optional[Dict[str, Any]] = None

    # -- transport -----------------------------------------------------------

    def request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                json_body: Optional[Dict[str, Any]] = None, expect: Iterable[int] = (200, 201, 204),
                retries: int = DEFAULT_RETRIES) -> Tuple[int, Any, Dict[str, str]]:
        url = urljoin(self.api_url, path.lstrip("/"))
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.session.request(
                    method, url, params=params, json=json_body,
                    timeout=self.config.api_timeout,
                )
            except requests.exceptions.SSLError as exc:
                raise GitLabError(
                    f"TLS verification failed talking to {self.base_url}: {exc}",
                    remedy="For a self-managed GitLab with a private CA, set "
                           "`target.ca_bundle` to the CA certificate path. Disabling "
                           "`target.verify_tls` is a last resort.",
                ) from exc
            except requests.exceptions.ConnectionError as exc:
                if attempt <= retries:
                    self._sleep_backoff(attempt)
                    continue
                raise GitLabError(
                    f"could not connect to {self.base_url}: {exc}",
                    remedy="Check the URL, DNS, and whether outbound HTTPS to GitLab is "
                           "allowed through the corporate proxy or firewall.",
                ) from exc
            except requests.exceptions.Timeout as exc:
                if attempt <= retries:
                    self._sleep_backoff(attempt)
                    continue
                raise GitLabError(f"request to {url} timed out after "
                                  f"{self.config.api_timeout}s") from exc

            if response.status_code in RETRY_STATUS and attempt <= retries:
                self._handle_retryable(response, attempt)
                continue

            body: Any
            try:
                body = response.json() if response.content else None
            except json.JSONDecodeError:
                body = response.text

            if response.status_code not in expect:
                raise self._translate(method, url, response, body)
            return response.status_code, body, dict(response.headers)

    def _sleep_backoff(self, attempt: int) -> None:
        delay = min(30, 2 ** attempt)
        log.warning("retrying GitLab request in %ds (attempt %d)", delay, attempt)
        time.sleep(delay)

    def _handle_retryable(self, response: requests.Response, attempt: int) -> None:
        if response.status_code == 429:
            reset = response.headers.get("RateLimit-Reset") or response.headers.get("Retry-After")
            wait = 60
            if reset:
                try:
                    value = int(reset)
                    wait = max(1, value - int(time.time())) if value > 10_000 else value
                except ValueError:
                    pass
            wait = min(wait, 300)
            log.warning("GitLab rate limit hit; waiting %ds before retrying", wait)
            time.sleep(wait)
        else:
            self._sleep_backoff(attempt)

    def _translate(self, method: str, url: str, response: requests.Response, body: Any) -> GitLabError:
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("error") or body)
        elif body:
            message = str(body)[:500]

        status = response.status_code
        if status == 401:
            return GitLabError(
                f"GitLab rejected the access token (401): {message}", status, message,
                "The token is invalid, expired or revoked. Create a new personal or group "
                "access token with the `api` and `write_repository` scopes.",
            )
        if status == 403:
            return GitLabError(
                f"GitLab denied the request (403): {message}", status, message,
                "The token authenticated but lacks permission. Creating projects and groups "
                "needs the `api` scope and at least Maintainer on the target namespace; "
                "pushing needs `write_repository`.",
            )
        if status == 404:
            return GitLabError(
                f"GitLab returned 404 for {method} {url}: {message}", status, message,
                "For a private namespace this often means 'exists but you cannot see it'. "
                "Confirm the namespace path and that the token's user is a member.",
            )
        if status == 400 and "has already been taken" in message:
            return GitLabError(
                f"the project path is already in use: {message}", status, message,
                "Choose a different `target.project`, or set "
                "`target.allow_non_empty_project: true` to push into the existing one.",
            )
        return GitLabError(f"GitLab {method} {url} failed with {status}: {message}", status, message)

    def paginate(self, path: str, params: Optional[Dict[str, Any]] = None,
                 max_pages: int = 100) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        page_params = dict(params or {})
        page_params.setdefault("per_page", 100)
        page = 1
        while page <= max_pages:
            page_params["page"] = page
            _status, body, headers = self.request("GET", path, params=page_params, expect=(200,))
            if not isinstance(body, list) or not body:
                break
            results.extend(body)
            next_page = headers.get("X-Next-Page")
            if not next_page:
                break
            page = int(next_page)
        return results

    # -- identity & capability -----------------------------------------------

    def current_user(self) -> Dict[str, Any]:
        if self._cached_user is None:
            _status, body, _headers = self.request("GET", "user", expect=(200,))
            self._cached_user = body if isinstance(body, dict) else {}
        return self._cached_user

    def version(self) -> Dict[str, Any]:
        try:
            _status, body, _headers = self.request("GET", "version", expect=(200,), retries=1)
            return body if isinstance(body, dict) else {}
        except GitLabError:
            return {}

    def check_access(self) -> Dict[str, Any]:
        """Prove the token works before a six-hour conversion, not after."""
        user = self.current_user()
        version = self.version()
        return {
            "username": user.get("username", ""),
            "name": user.get("name", ""),
            "id": user.get("id", 0),
            "is_admin": bool(user.get("is_admin", False)),
            "gitlab_version": version.get("version", "unknown"),
            "gitlab_url": self.base_url,
        }

    # -- namespaces ----------------------------------------------------------

    def find_namespace(self, full_path: str) -> Optional[Namespace]:
        if not full_path:
            return None
        try:
            _status, body, _headers = self.request(
                "GET", f"namespaces/{quote(full_path, safe='')}", expect=(200,), retries=1)
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(body, dict):
            return None
        return Namespace(id=body.get("id", 0), full_path=body.get("full_path", full_path),
                         kind=body.get("kind", "group"))

    def find_group(self, full_path: str) -> Optional[Dict[str, Any]]:
        try:
            _status, body, _headers = self.request(
                "GET", f"groups/{quote(full_path, safe='')}", expect=(200,), retries=1)
            return body if isinstance(body, dict) else None
        except GitLabError as exc:
            if exc.status in (404, 403):
                return None
            raise

    def ensure_namespace(self, full_path: str, visibility: Visibility = Visibility.PRIVATE,
                         create: bool = True) -> Optional[Namespace]:
        """Find or create a possibly-nested group path."""
        if not full_path:
            return None
        existing = self.find_namespace(full_path)
        if existing:
            return existing
        if not create:
            raise GitLabError(
                f"namespace {full_path!r} does not exist", 404, "",
                "Create the group in GitLab first, or set `target.create_namespace: true`.",
            )

        segments = full_path.split("/")
        parent_id: Optional[int] = None
        walked: List[str] = []
        for segment in segments:
            walked.append(segment)
            path_so_far = "/".join(walked)
            found = self.find_namespace(path_so_far)
            if found:
                if found.kind == "user" and len(walked) < len(segments):
                    raise GitLabError(
                        f"{path_so_far!r} is a user namespace and cannot contain subgroups",
                        400, "",
                        "Nested paths must be groups. Use a top-level group for the migration.",
                    )
                parent_id = found.id
                continue
            payload: Dict[str, Any] = {
                "name": segment, "path": segment, "visibility": visibility.value,
                "description": "Created by svn2gitlab during SVN migration",
            }
            if parent_id:
                payload["parent_id"] = parent_id
            log.info("creating GitLab group %s", path_so_far)
            _status, body, _headers = self.request("POST", "groups", json_body=payload,
                                                   expect=(200, 201))
            parent_id = body.get("id") if isinstance(body, dict) else None

        result = self.find_namespace(full_path)
        if not result:
            raise GitLabError(f"created namespace {full_path!r} but could not read it back")
        return result

    # -- projects ------------------------------------------------------------

    def find_project(self, full_path: str, statistics: bool = True) -> Optional[Project]:
        params = {"statistics": "true"} if statistics else None
        try:
            _status, body, _headers = self.request(
                "GET", f"projects/{quote(full_path, safe='')}", params=params,
                expect=(200,), retries=1)
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise
        return Project.from_json(body) if isinstance(body, dict) else None

    def create_project(self, path: str, namespace: Optional[Namespace],
                       name: Optional[str] = None, description: Optional[str] = None,
                       visibility: Visibility = Visibility.PRIVATE,
                       default_branch: str = "main", lfs_enabled: bool = True) -> Project:
        payload: Dict[str, Any] = {
            "path": path,
            "name": name or path,
            "visibility": visibility.value,
            "description": description or "Migrated from Subversion by svn2gitlab",
            "initialize_with_readme": False,
            "lfs_enabled": lfs_enabled,
            "default_branch": default_branch,
        }
        if namespace:
            payload["namespace_id"] = namespace.id
        log.info("creating GitLab project %s", f"{namespace.full_path}/{path}" if namespace else path)
        _status, body, _headers = self.request("POST", "projects", json_body=payload,
                                               expect=(200, 201))
        if not isinstance(body, dict):
            raise GitLabError("project creation returned an unexpected response")
        return Project.from_json(body)

    def ensure_project(self, target: TargetConfig, default_branch: str,
                       lfs_enabled: bool = True, allow_existing: bool = False) -> Project:
        """Find or create the target project.

        `allow_existing` is set by the caller when *this* migration has already
        pushed here, which is the normal state of every incremental sync after the
        first one. Without it the non-empty guard - which exists to stop us
        clobbering an unrelated project - would block the entire sync workflow.
        """
        full_path = target.full_path()
        existing = self.find_project(full_path)
        if existing:
            if not existing.empty_repo and not target.allow_non_empty_project \
                    and not allow_existing:
                raise GitLabError(
                    f"GitLab project {full_path} already contains commits", 409, "",
                    "Refusing to push into a non-empty project, because this migration "
                    "has no record of having populated it. Delete the project, choose "
                    "another path, or set `target.allow_non_empty_project: true` to add "
                    "to it deliberately. (Re-syncing a migration this tool already "
                    "pushed is allowed automatically.)",
                )
            log.info("using existing GitLab project %s (empty=%s, previously pushed by us=%s)",
                     full_path, existing.empty_repo, allow_existing)
            return existing

        namespace = None
        if target.namespace:
            namespace = self.ensure_namespace(target.namespace, target.visibility,
                                              target.create_namespace)
        return self.create_project(
            path=target.project or "", namespace=namespace,
            name=target.project_name or target.project,
            description=target.description, visibility=target.visibility,
            default_branch=default_branch, lfs_enabled=lfs_enabled,
        )

    def update_project(self, project_id: int, **fields: Any) -> Project:
        _status, body, _headers = self.request("PUT", f"projects/{project_id}",
                                               json_body=fields, expect=(200,))
        return Project.from_json(body) if isinstance(body, dict) else None  # type: ignore[return-value]

    def set_default_branch(self, project_id: int, branch: str) -> None:
        self.update_project(project_id, default_branch=branch)
        log.info("set default branch to %s", branch)

    # -- protections ---------------------------------------------------------

    def protect_branch(self, project_id: int, branch: str,
                       push_level: int = 40, merge_level: int = 40,
                       allow_force_push: bool = False) -> None:
        """Access levels: 0 no-one, 30 developer, 40 maintainer."""
        self.request("DELETE", f"projects/{project_id}/protected_branches/{quote(branch, safe='')}",
                     expect=(200, 204, 404))
        payload = {
            "name": branch,
            "push_access_level": push_level,
            "merge_access_level": merge_level,
            "allow_force_push": allow_force_push,
        }
        self.request("POST", f"projects/{project_id}/protected_branches",
                     json_body=payload, expect=(200, 201, 409))
        log.info("protected branch %s", branch)

    def unprotect_branch(self, project_id: int, branch: str) -> None:
        self.request("DELETE", f"projects/{project_id}/protected_branches/{quote(branch, safe='')}",
                     expect=(200, 204, 404))

    def protect_tags(self, project_id: int, pattern: str = "*", access_level: int = 40) -> None:
        self.request("POST", f"projects/{project_id}/protected_tags",
                     json_body={"name": pattern, "create_access_level": access_level},
                     expect=(200, 201, 409))

    # -- users & quotas ------------------------------------------------------

    def find_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        results = self.paginate("users", {"search": email}, max_pages=2)
        for user in results:
            if (user.get("email") or "").lower() == email.lower():
                return user
            if (user.get("public_email") or "").lower() == email.lower():
                return user
        return results[0] if len(results) == 1 else None

    def match_authors(self, emails: Iterable[str]) -> Dict[str, Optional[str]]:
        """Map author emails to GitLab usernames so the report can flag unattributed history."""
        out: Dict[str, Optional[str]] = {}
        for email in emails:
            if not email:
                continue
            try:
                user = self.find_user_by_email(email)
                out[email] = user.get("username") if user else None
            except GitLabError as exc:
                # Searching users requires elevated scope on some instances; not fatal.
                log.debug("user lookup for %s failed: %s", email, exc.message)
                out[email] = None
        return out

    def namespace_storage(self, full_path: str) -> Dict[str, Any]:
        group = self.find_group(full_path)
        if not group:
            return {}
        stats = group.get("root_storage_statistics") or {}
        return {
            "storage_size": stats.get("storage_size"),
            "repository_size": stats.get("repository_size"),
            "lfs_objects_size": stats.get("lfs_objects_size"),
        }

    def project_statistics(self, project_id: int) -> Dict[str, Any]:
        _status, body, _headers = self.request("GET", f"projects/{project_id}",
                                               params={"statistics": "true"}, expect=(200,))
        return (body or {}).get("statistics", {}) if isinstance(body, dict) else {}

    # -- push URL ------------------------------------------------------------

    def push_url(self, project: Project) -> str:
        """An https URL with credentials embedded, for a one-shot push.

        The URL is registered with the redactor, so it never reaches a log file or the
        web UI in readable form.
        """
        if self.config.remote_url:
            return self.config.remote_url
        if self.config.ssh_key:
            return project.ssh_url_to_repo
        http = project.http_url_to_repo or f"{self.base_url}/{project.path_with_namespace}.git"
        token = self.config.token or ""
        if not token:
            return http
        scheme, _, rest = http.partition("://")
        user = "oauth2" if self.config.token_kind == "oauth2" else "gitlab-ci-token"
        if self.config.token_kind == "private":
            user = "oauth2"
        url = f"{scheme}://{user}:{token}@{rest}"
        REDACTOR.add(url)
        REDACTOR.add(token)
        return url
