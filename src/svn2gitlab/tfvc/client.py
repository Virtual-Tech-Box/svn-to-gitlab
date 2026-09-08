"""Team Foundation Version Control client, over the REST API.

Targets on-premises TFS 2015/2017/2018 and Azure DevOps Server/Services. REST is
used deliberately rather than `tf.exe` or the .NET client libraries: those are
Windows-only and would reintroduce exactly the third-party dependency this project
removed on the Subversion side.

Everything here is grounded in the published API reference rather than assumption,
because three of its defaults will silently corrupt a migration if taken at face
value:

* **`maxCommentLength` defaults to 80.** Ask for changesets without it and every
  commit message longer than 80 characters comes back truncated, with a quiet
  `commentTruncated: true` beside it. We always request the full comment and assert
  it was not truncated.
* **`changeType` is a flags enum**, serialised as a comma-separated string such as
  `"rename, edit"`. Treating it as a single value loses the edit half of a
  rename-and-edit, so the file keeps its old content under its new name.
* **`item.hashValue` is a base64 MD5**, not hex and not SHA-1. It is genuinely
  useful — it lets us deduplicate identical content across paths and detect a
  truncated download — but only if decoded correctly.

Item content for a given `(path, changeset)` is immutable, which makes it safely
cacheable; TFVC reports the changeset in which each item last changed, so the same
blob is fetched once no matter how many changesets reference it.
"""

from __future__ import annotations

import base64
import hashlib
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence
from urllib.parse import quote, urlencode

import requests

from ..errors import Svn2GitlabError
from ..logging_setup import REDACTOR, get_logger

log = get_logger("tfvc")

# Highest api-version we ask for. TFS 2015 speaks 2.0, 2017 speaks 3.x, 2018 4.x;
# the server is probed and we fall back rather than guessing.
PREFERRED_API_VERSIONS = ("4.1", "3.2", "2.0", "1.0")

# Comments are truncated to 80 characters unless we ask otherwise.
MAX_COMMENT_LENGTH = 1_000_000

# Page sizes. The server caps these; we honour whatever it actually returns.
CHANGESET_PAGE = 200
CHANGES_PAGE = 500


class TfvcError(Svn2GitlabError):
    """TFVC/Azure DevOps API failure."""


@dataclass
class Identity:
    display_name: str = ""
    unique_name: str = ""       # usually DOMAIN\user or an email address
    id: str = ""

    @classmethod
    def parse(cls, raw: Optional[Dict[str, Any]]) -> "Identity":
        raw = raw or {}
        return cls(
            display_name=raw.get("displayName") or "",
            unique_name=raw.get("uniqueName") or raw.get("mailAddress") or "",
            id=raw.get("id") or "",
        )

    @property
    def key(self) -> str:
        """The identifier we map to a Git identity."""
        return self.unique_name or self.display_name or "(unknown)"


@dataclass
class Changeset:
    id: int
    author: Identity = field(default_factory=Identity)
    checked_in_by: Identity = field(default_factory=Identity)
    created_date: str = ""
    comment: str = ""
    comment_truncated: bool = False

    @property
    def message(self) -> str:
        return self.comment or ""

    def git_date(self) -> str:
        return _iso_to_git_date(self.created_date)


@dataclass
class Change:
    """One item changed in one changeset."""

    path: str
    change_types: List[str]          # flags, lower-cased
    version: int = 0                 # changeset in which this item content lives
    size: int = 0
    md5_base64: str = ""
    is_folder: bool = False
    is_branch: bool = False
    is_symlink: bool = False
    deletion_id: int = 0
    source_path: str = ""            # sourceServerItem, for rename/branch
    merge_sources: List[Dict[str, Any]] = field(default_factory=list)
    encoding: int = 0                # TFVC codepage; -1 marks binary

    # -- flag helpers ---------------------------------------------------------

    def has(self, *kinds: str) -> bool:
        return any(k in self.change_types for k in kinds)

    @property
    def is_delete(self) -> bool:
        return self.has("delete")

    @property
    def is_rename(self) -> bool:
        return self.has("rename", "sourcerename", "targetrename")

    @property
    def is_branch_creation(self) -> bool:
        return self.has("branch")

    @property
    def touches_content(self) -> bool:
        """Whether the item's bytes changed, as opposed to only its path or metadata.

        A rename with no edit keeps its content; a `rename, edit` does not. Missing
        this distinction is how a renamed-and-edited file ends up with stale content.

        `merge` is *not* in this set, and that is the point. It is a relationship
        flag, not an action: the action travels beside it as `merge, edit`,
        `merge, branch` or `merge, delete`. A bare `merge` records that a merge
        happened without changing the item - and when what was merged is a deletion,
        TFS reports a bare `merge` on a path that is already, and stays, deleted.
        Treating that as content resurrected 730 deleted files across two branches of
        a customer migration, silently, because `_blob_for` served the bytes from its
        hash cache and never made a request that could have 404'd.
        """
        return self.has("add", "edit", "branch", "undelete", "rollback")

    @property
    def is_metadata_only(self) -> bool:
        return bool(self.change_types) and not self.touches_content and not self.is_delete

    @property
    def md5(self) -> str:
        """Hex MD5, or "" when the server did not supply one."""
        if not self.md5_base64:
            return ""
        try:
            return base64.b64decode(self.md5_base64).hex()
        except (ValueError, TypeError):
            return ""


def _iso_to_git_date(value: str) -> str:
    """`2019-05-29T18:00:23.457Z` -> `<unix ts> +0000`."""
    if not value:
        return "0 +0000"
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                parsed = datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return "0 +0000"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return f"{int(parsed.timestamp())} +0000"


def _server_message(response) -> str:
    """TFS's own error text, which is almost always more precise than ours."""
    try:
        payload = response.json()
    except Exception:
        return ""
    message = payload.get("message") if isinstance(payload, dict) else ""
    return str(message).strip() if message else ""


def _parse_change_types(raw: Any) -> List[str]:
    """`"rename, edit"` -> `["rename", "edit"]`. Also accepts a list."""
    if isinstance(raw, list):
        return [str(v).strip().lower() for v in raw if str(v).strip()]
    if not raw:
        return []
    return [part.strip().lower() for part in str(raw).split(",") if part.strip()]


class TfvcClient:
    """Read-only access to a TFVC collection."""

    def __init__(
        self,
        base_url: str,
        collection: str = "",
        project: str = "",
        token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_tls: bool = True,
        ca_bundle: Optional[str] = None,
        timeout: int = 120,
        cancel: Optional[threading.Event] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.collection = collection.strip("/")
        self.project = project.strip("/")
        self.timeout = timeout
        self.cancel = cancel
        self.api_version = ""
        REDACTOR.add(token)
        REDACTOR.add(password)

        self.session = requests.Session()
        self.session.verify = ca_bundle or verify_tls
        self.session.headers["Accept"] = "application/json"
        self.session.headers["User-Agent"] = "svn2gitlab"

        if token:
            # Personal access token: HTTP Basic with an empty username. Supported by
            # TFS 2015 Update 3 and later, and by Azure DevOps.
            pair = base64.b64encode(f":{token}".encode()).decode()
            self.session.headers["Authorization"] = f"Basic {pair}"
        elif username:
            # On-premises TFS is commonly Windows-authenticated. NTLM needs an extra
            # package; fall back to Basic and say so clearly if it is absent.
            if "\\" in username or "@" in username:
                try:
                    from requests_ntlm import HttpNtlmAuth  # type: ignore

                    self.session.auth = HttpNtlmAuth(username, password or "")
                except ImportError:
                    log.warning(
                        "requests-ntlm is not installed; falling back to Basic auth. "
                        "If this TFS uses Windows authentication, install it "
                        "(pip install requests-ntlm) or use a personal access token."
                    )
                    self.session.auth = (username, password or "")
            else:
                self.session.auth = (username, password or "")

        # (path, version) -> content. Item content at a version is immutable.
        self._content_cache: Dict[str, bytes] = {}
        self._cache_lock = threading.Lock()

    # -- plumbing ------------------------------------------------------------

    def _url(self, resource: str, project_scoped: bool = False) -> str:
        parts = [self.base_url]
        if self.collection:
            parts.append(self.collection)
        if project_scoped and self.project:
            parts.append(quote(self.project, safe=""))
        parts.append("_apis")
        parts.append(resource.lstrip("/"))
        return "/".join(p.strip("/") for p in parts if p)

    def request(
        self,
        resource: str,
        params: Optional[Dict[str, Any]] = None,
        project_scoped: bool = False,
        raw: bool = False,
        accept: Optional[str] = None,
        retries: int = 3,
    ) -> Any:
        if self.cancel is not None and self.cancel.is_set():
            raise TfvcError("cancelled")

        query = dict(params or {})
        query.setdefault("api-version", self.api_version or PREFERRED_API_VERSIONS[0])
        # Encoded here rather than left to the HTTP library, which renders a space as
        # `+`. TFVC server paths very often contain spaces ("$/Daily Mail Scheduler"),
        # and TFS reads that plus literally, then reports the item does not exist.
        encoded = urlencode({k: str(v) for k, v in query.items()}, quote_via=quote)
        url = self._url(resource, project_scoped=project_scoped)
        headers = {"Accept": accept} if accept else None

        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                response = self.session.get(url, params=encoded, headers=headers,
                                            timeout=self.timeout, stream=raw)
            except requests.exceptions.SSLError as exc:
                raise TfvcError(
                    f"TLS verification failed for {self.base_url}: {exc}",
                    "On-premises TFS often uses a private certificate authority. Set "
                    "`source.ca_bundle` to the CA certificate, or `verify_tls: false` "
                    "on a trusted network.",
                ) from exc
            except requests.exceptions.ConnectionError as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise TfvcError(
                    f"could not reach {self.base_url}: {exc}",
                    "Check the collection URL, that the TFS application tier is "
                    "running, and that any proxy or firewall allows this host.",
                ) from exc

            if response.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = int(response.headers.get("Retry-After") or (2 ** attempt))
                log.warning("TFS returned %s; retrying in %ss", response.status_code, wait)
                time.sleep(wait)
                continue

            if response.status_code == 401:
                raise TfvcError(
                    "TFS rejected the credentials (401)",
                    "For Azure DevOps or TFS 2015 Update 3+, use a personal access "
                    "token with the `Code (read)` scope. For older Windows-"
                    "authenticated servers, set source.username as DOMAIN\\user and "
                    "install requests-ntlm.",
                )
            if response.status_code == 403:
                raise TfvcError(
                    "TFS denied the request (403)",
                    "The account needs 'Read' on the team project. A partially "
                    "readable collection cannot be migrated faithfully.",
                )
            if response.status_code == 404:
                detail = _server_message(response)
                raise TfvcError(
                    f"TFS returned 404 for {resource}"
                    + (f": {detail}" if detail else f" ({url})"),
                    "If the message names an item that does not exist, check "
                    "`source.project` / `source.project_root`: the team project is "
                    "often not the same name as the collection. "
                    "`svn2gitlab analyze` lists the projects it can see.",
                )
            if not response.ok:
                detail = _server_message(response) or response.text[:300]
                raise TfvcError(f"TFS request failed ({response.status_code}): {detail}")

            # An HTML response means the server answered with a sign-in page rather
            # than JSON, which is a silent auth failure on some IIS configurations.
            content_type = response.headers.get("Content-Type", "")
            if not raw and "html" in content_type.lower():
                raise TfvcError(
                    "TFS returned an HTML page instead of JSON",
                    "This normally means authentication was redirected to a sign-in "
                    "page. Use a personal access token rather than interactive "
                    "credentials.",
                )
            return response.content if raw else response.json()

        raise TfvcError(f"request failed after {retries} retries: {last_error}")

    # -- capability probing ---------------------------------------------------

    def probe(self) -> str:
        """Find an api-version this server accepts, newest first.

        TFS 2015 speaks 2.0, 2017 speaks 3.x, 2018 speaks 4.x. Asking for a version
        the server does not implement returns an error rather than degrading, so we
        establish this once instead of hard-coding a guess.
        """
        errors = []
        for version in PREFERRED_API_VERSIONS:
            self.api_version = version
            try:
                self.request("tfvc/changesets", params={"$top": 1})
                log.info("TFS at %s accepts api-version %s", self.base_url, version)
                return version
            except TfvcError as exc:
                if "401" in exc.message or "403" in exc.message:
                    raise      # credentials, not an unsupported API version
                errors.append(f"{version}: {exc.message}")
                continue
        self.api_version = ""
        raise TfvcError(
            "could not negotiate a usable API version with this server:\n    "
            + "\n    ".join(errors),
            "TFS 2013 and older do not expose the TFVC REST API. Those need "
            "tf.exe on Windows, which this tool deliberately does not use.",
        )

    def server_info(self) -> Dict[str, Any]:
        try:
            return self.request("projects", params={"$top": 100})
        except TfvcError:
            return {}

    # -- changesets -----------------------------------------------------------

    def changeset_count_hint(self, item_path: str = "") -> int:
        """The newest changeset id, which bounds the work. 0 when unknown."""
        params: Dict[str, Any] = {"$top": 1, "$orderby": "id desc",
                                  "maxCommentLength": 1}
        if item_path:
            params["searchCriteria.itemPath"] = item_path
        payload = self.request("tfvc/changesets", params=params)
        values = payload.get("value") or []
        return int(values[0].get("changesetId", 0)) if values else 0

    def iter_changesets(
        self,
        item_path: str = "",
        from_id: Optional[int] = None,
        to_id: Optional[int] = None,
        page_size: int = CHANGESET_PAGE,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> Iterator[Changeset]:
        """Yield changesets oldest-first.

        Paging is by `$skip`, and the server may return fewer than asked for; we
        follow what it actually gives us rather than assuming the page size held.
        """
        skip = 0
        newest = to_id or 0
        previous_last: Optional[int] = None
        while True:
            params: Dict[str, Any] = {
                "$top": page_size,
                "$skip": skip,
                "$orderby": "id asc",
                # Without this the server truncates every comment to 80 characters.
                "maxCommentLength": MAX_COMMENT_LENGTH,
            }
            if item_path:
                params["searchCriteria.itemPath"] = item_path
            if from_id is not None:
                params["searchCriteria.fromId"] = from_id
            if to_id is not None:
                params["searchCriteria.toId"] = to_id

            payload = self.request("tfvc/changesets", params=params)
            values = payload.get("value") or []
            if not values:
                return          # an empty page is the only reliable end-of-data signal

            last_id: Optional[int] = None
            for raw in values:
                changeset = Changeset(
                    id=int(raw.get("changesetId", 0)),
                    author=Identity.parse(raw.get("author")),
                    checked_in_by=Identity.parse(raw.get("checkedInBy")),
                    created_date=raw.get("createdDate") or "",
                    comment=raw.get("comment") or "",
                    comment_truncated=bool(raw.get("commentTruncated")),
                )
                if changeset.comment_truncated:
                    # Should not happen given maxCommentLength, but a silently
                    # shortened commit message is not something to guess about.
                    changeset.comment = self.changeset_comment(changeset.id) or changeset.comment
                last_id = changeset.id
                yield changeset
                if on_progress and newest:
                    on_progress(changeset.id, newest)

            skip += len(values)
            # Deliberately NOT `if len(values) < page_size: return`. The server caps
            # page size independently of what we ask for, so a short page is the
            # normal case - treating it as the end silently truncates the migration
            # at the first page and reports success.
            if last_id is not None and last_id == previous_last:
                raise TfvcError(
                    f"the server stopped advancing at changeset {last_id}",
                    "Paging made no progress, which would loop forever. Report this "
                    "with the server version from `svn2gitlab doctor`.",
                )
            previous_last = last_id

    def changeset_comment(self, changeset_id: int) -> str:
        """Full comment for one changeset, for when a listing came back truncated."""
        payload = self.request(f"tfvc/changesets/{changeset_id}",
                               params={"maxCommentLength": MAX_COMMENT_LENGTH})
        return payload.get("comment") or ""

    def changeset_changes(self, changeset_id: int,
                          page_size: int = CHANGES_PAGE) -> List[Change]:
        """Every item changed in a changeset, following pagination to the end."""
        changes: List[Change] = []
        skip = 0
        while True:
            payload = self.request(
                f"tfvc/changesets/{changeset_id}/changes",
                params={"$top": page_size, "$skip": skip},
            )
            values = payload.get("value") or []
            if not values:
                break
            for raw in values:
                changes.append(self._parse_change(raw))
            skip += len(values)  # again: only an empty page ends the listing
        return changes

    @staticmethod
    def _parse_change(raw: Dict[str, Any]) -> Change:
        item = raw.get("item") or {}
        metadata = item.get("contentMetadata") or {}
        return Change(
            path=item.get("path") or "",
            change_types=_parse_change_types(raw.get("changeType")),
            version=int(item.get("version") or 0),
            size=int(item.get("size") or 0),
            md5_base64=item.get("hashValue") or "",
            is_folder=bool(item.get("isFolder")),
            is_branch=bool(item.get("isBranch")),
            is_symlink=bool(item.get("isSymLink")),
            deletion_id=int(item.get("deletionId") or 0),
            source_path=raw.get("sourceServerItem") or "",
            merge_sources=raw.get("mergeSources") or [],
            encoding=int(metadata.get("encoding") or item.get("encoding") or 0),
        )

    # -- items ----------------------------------------------------------------

    def item_content(self, path: str, version: int, expected_md5: str = "",
                     expected_size: int = -1) -> bytes:
        """Raw bytes of an item at a changeset.

        Cached on `(path, version)`: TFVC item content at a given changeset is
        immutable, so the same blob is never fetched twice however many changesets
        reference it.
        """
        key = f"{version}\x00{path}"
        with self._cache_lock:
            hit = self._content_cache.get(key)
        if hit is not None:
            return hit

        content = self.request(
            "tfvc/items",
            params={
                "path": path,
                "versionDescriptor.version": version,
                "versionDescriptor.versionType": "changeset",
                # `download` forces an attachment rather than a JSON envelope.
                "download": "true",
            },
            project_scoped=bool(self.project),
            raw=True,
            accept="application/octet-stream",
        )

        if expected_size >= 0 and len(content) != expected_size:
            raise TfvcError(
                f"{path}@{version}: expected {expected_size} bytes, received "
                f"{len(content)}",
                "The download was truncated or the server returned a JSON envelope "
                "instead of raw content. This would silently corrupt the migrated "
                "file, so the conversion stops here.",
            )
        if expected_md5:
            actual = hashlib.md5(content).hexdigest()
            if actual != expected_md5:
                raise TfvcError(
                    f"{path}@{version}: content hash mismatch "
                    f"(expected {expected_md5}, got {actual})",
                    "TFS reported a different MD5 for this item than the bytes it "
                    "sent. Re-run; if it persists the item may be corrupt server-side.",
                )

        with self._cache_lock:
            self._content_cache[key] = content
        return content

    def list_items(self, scope_path: str, version: Optional[int] = None,
                   recursive: bool = True) -> List[Dict[str, Any]]:
        """Items under a path, optionally at a specific changeset.

        Used by verification to enumerate the source tree, the TFVC equivalent of
        `svn ls -R`.
        """
        params: Dict[str, Any] = {
            "scopePath": scope_path,
            # Capitalised exactly as the server demands. TFS rejects "full" with
            # a 400 listing the valid values, and because the tree listing is what
            # verification reads, a lowercase value made every branch fail to verify.
            "recursionLevel": "Full" if recursive else "OneLevel",
        }
        if version:
            params["versionDescriptor.version"] = version
            params["versionDescriptor.versionType"] = "changeset"
        payload = self.request("tfvc/items", params=params,
                               project_scoped=bool(self.project))
        return payload.get("value") or []

    def branches(self, include_deleted: bool = False) -> List[Dict[str, Any]]:
        """Branch objects the server knows about.

        Only branches explicitly *converted to branches* in TFVC appear here. Plenty
        of real projects branch by copying folders and never convert them, so this is
        a hint used alongside path-based detection, never the sole source of truth.
        """
        try:
            payload = self.request(
                "tfvc/branches",
                params={"includeParent": "true", "includeChildren": "true",
                        "includeDeleted": str(bool(include_deleted)).lower()},
                project_scoped=bool(self.project),
            )
        except TfvcError as exc:
            log.debug("branch listing unavailable: %s", exc)
            return []
        return payload.get("value") or []

    def projects(self) -> List[Dict[str, Any]]:
        payload = self.request("projects", params={"$top": 500})
        return payload.get("value") or []
