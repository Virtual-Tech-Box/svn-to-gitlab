"""A minimal TFS / Azure DevOps stand-in serving the TFVC REST endpoints we call.

There is no way to run a real TFVC server in a test suite, so this models one from
the published API reference. It is deliberately *awkward* in the same ways the real
API is, because those are the behaviours that break migrations:

* comments are truncated to `maxCommentLength`, which defaults to 80, and the
  response says so only via a `commentTruncated` flag;
* `changeType` is emitted as a comma-separated flags string (`"rename, edit"`);
* `hashValue` is a base64 MD5, not hex;
* listings page with `$top`/`$skip` and may return fewer rows than requested;
* `/_apis/tfvc/changesets/{id}/changes` is collection-scoped while `/items` is
  project-scoped.

A fake that is nicer than the real thing would let exactly the bugs we care about
through, so where the real API is annoying, this is annoying in the same way.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_MAX_COMMENT = 80


@dataclass
class FakeItem:
    """One item version: content at a particular changeset."""

    path: str
    content: Optional[bytes] = None      # None for folders
    is_folder: bool = False
    is_branch: bool = False
    is_symlink: bool = False
    encoding: int = 65001                # 65001 = UTF-8; -1 marks binary

    @property
    def size(self) -> int:
        return len(self.content or b"")

    @property
    def md5_base64(self) -> str:
        if self.content is None:
            return ""
        return base64.b64encode(hashlib.md5(self.content).digest()).decode()


@dataclass
class FakeChange:
    item: FakeItem
    change_types: List[str]
    source_path: str = ""


@dataclass
class FakeChangeset:
    id: int
    author: str
    comment: str
    created_date: str
    changes: List[FakeChange] = field(default_factory=list)
    display_name: str = ""


class TfvcHistory:
    """Builds a TFVC history for tests: changesets, item versions, branches."""

    def __init__(self, project: str = "DemoProject") -> None:
        self.project = project
        self.changesets: List[FakeChangeset] = []
        # (path, changeset_id) -> content, mirroring TFVC's immutable item versions.
        self.contents: Dict[Tuple[str, int], bytes] = {}
        self.branches: List[str] = []
        self._next_id = 1

    def commit(self, author: str, comment: str, changes: List[FakeChange],
               date: str = "") -> int:
        changeset_id = self._next_id
        self._next_id += 1
        for change in changes:
            if change.item.content is not None:
                self.contents[(change.item.path, changeset_id)] = change.item.content
        self.changesets.append(FakeChangeset(
            id=changeset_id,
            author=author,
            comment=comment,
            created_date=date or f"2020-01-{changeset_id:02d}T10:00:00.000Z",
            changes=changes,
            display_name=author.split("\\")[-1].replace(".", " ").title(),
        ))
        return changeset_id

    # -- convenience builders -------------------------------------------------

    def add(self, path: str, content: bytes, **kw) -> FakeChange:
        return FakeChange(FakeItem(path, content, **kw), ["add"])

    def edit(self, path: str, content: bytes, **kw) -> FakeChange:
        return FakeChange(FakeItem(path, content, **kw), ["edit"])

    def delete(self, path: str) -> FakeChange:
        return FakeChange(FakeItem(path, None), ["delete"])

    def mkdir(self, path: str, is_branch: bool = False) -> FakeChange:
        return FakeChange(FakeItem(path, None, is_folder=True, is_branch=is_branch),
                          ["add"])

    def rename(self, old_path: str, new_path: str, content: bytes,
               edited: bool = False) -> FakeChange:
        kinds = ["rename", "edit"] if edited else ["rename"]
        return FakeChange(FakeItem(new_path, content), kinds, source_path=old_path)

    def branch(self, path: str, source_path: str, content: Optional[bytes] = None,
               is_folder: bool = False) -> FakeChange:
        return FakeChange(FakeItem(path, content, is_folder=is_folder, is_branch=is_folder),
                          ["branch"], source_path=source_path)

    # -- queries used by the handler ------------------------------------------

    def content_at(self, path: str, version: int) -> Optional[bytes]:
        """Content of `path` as of changeset `version`, i.e. the newest at or below."""
        best: Optional[bytes] = None
        best_version = -1
        for (item_path, changeset_id), content in self.contents.items():
            if item_path == path and changeset_id <= version and changeset_id > best_version:
                best, best_version = content, changeset_id
        if self._deleted_at(path, version):
            return None
        return best

    def _deleted_at(self, path: str, version: int) -> bool:
        deleted = False
        for changeset in self.changesets:
            if changeset.id > version:
                break
            for change in changeset.changes:
                if change.item.path != path:
                    continue
                if "delete" in change.change_types:
                    deleted = True
                elif change.change_types:
                    deleted = False
        return deleted

    def tree_at(self, scope_path: str, version: int) -> List[FakeItem]:
        """Every live item under `scope_path` at a changeset."""
        seen: Dict[str, FakeItem] = {}
        for changeset in self.changesets:
            if changeset.id > version:
                break
            for change in changeset.changes:
                path = change.item.path
                if not (path == scope_path or path.startswith(scope_path.rstrip("/") + "/")):
                    continue
                if "delete" in change.change_types:
                    seen.pop(path, None)
                    for child in [p for p in seen if p.startswith(path + "/")]:
                        seen.pop(child, None)
                    continue
                if change.source_path and "rename" in change.change_types:
                    seen.pop(change.source_path, None)
                seen[path] = change.item
        return [item for item in seen.values() if not item.is_folder]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeTFS/1.0"

    def log_message(self, *_args) -> None:
        pass

    @property
    def history(self) -> TfvcHistory:
        return self.server.history  # type: ignore[attr-defined]

    @property
    def server_state(self):
        return self.server  # type: ignore[return-value]

    # -- helpers --------------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"message": message, "typeKey": "FakeTfsError"}, status)

    def _authorised(self) -> bool:
        expected = self.server.token  # type: ignore[attr-defined]
        if not expected:
            return True
        header = self.headers.get("Authorization") or ""
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode()
            except Exception:
                return False
            return decoded.split(":", 1)[-1] == expected
        return False

    # -- routing --------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        self.server.requests.append((parsed.path, query))  # type: ignore[attr-defined]

        if not self._authorised():
            self._error(401, "TF400813: The user is not authorized to access this resource.")
            return

        version = query.get("api-version", "")
        supported = self.server.api_versions  # type: ignore[attr-defined]
        if version not in supported:
            # Exactly how the real thing refuses an unimplemented version.
            self._error(400, f"The requested REST API version of {version!r} is out of "
                             f"range for this server. Supported: {', '.join(supported)}")
            return

        path = parsed.path
        if re.search(r"/_apis/tfvc/changesets/(\d+)/changes/?$", path):
            self._changeset_changes(int(re.search(r"changesets/(\d+)/changes", path).group(1)), query)
        elif re.search(r"/_apis/tfvc/changesets/(\d+)/?$", path):
            self._changeset(int(re.search(r"changesets/(\d+)", path).group(1)), query)
        elif path.endswith("/_apis/tfvc/changesets"):
            self._changesets(query)
        elif path.endswith("/_apis/tfvc/items"):
            self._items(query)
        elif path.endswith("/_apis/tfvc/branches"):
            self._json({"count": len(self.history.branches),
                        "value": [{"path": p, "isDeleted": False}
                                  for p in self.history.branches]})
        elif path.endswith("/_apis/projects"):
            self._json({"count": 1, "value": [{"id": "proj-1", "name": self.history.project}]})
        else:
            self._error(404, f"no route for {path}")

    # -- endpoints ------------------------------------------------------------

    def _changesets(self, query: Dict[str, str]) -> None:
        limit = int(query.get("maxCommentLength", DEFAULT_MAX_COMMENT))
        rows = list(self.history.changesets)

        from_id = query.get("searchCriteria.fromId")
        to_id = query.get("searchCriteria.toId")
        if from_id:
            rows = [c for c in rows if c.id >= int(from_id)]
        if to_id:
            rows = [c for c in rows if c.id <= int(to_id)]
        item_path = query.get("searchCriteria.itemPath")
        if item_path:
            rows = [c for c in rows
                    if any(ch.item.path == item_path
                           or ch.item.path.startswith(item_path.rstrip("/") + "/")
                           for ch in c.changes)]

        if query.get("$orderby", "id desc").startswith("id desc"):
            rows.sort(key=lambda c: c.id, reverse=True)
        else:
            rows.sort(key=lambda c: c.id)

        skip = int(query.get("$skip", 0))
        top = int(query.get("$top", 100))
        # The real service caps page size; returning fewer than asked for is normal.
        page = rows[skip:skip + min(top, self.server.page_cap)]  # type: ignore[attr-defined]

        self._json({"count": len(page),
                    "value": [self._changeset_json(c, limit) for c in page]})

    def _changeset(self, changeset_id: int, query: Dict[str, str]) -> None:
        limit = int(query.get("maxCommentLength", DEFAULT_MAX_COMMENT))
        for changeset in self.history.changesets:
            if changeset.id == changeset_id:
                self._json(self._changeset_json(changeset, limit))
                return
        self._error(404, f"changeset {changeset_id} does not exist")

    def _changeset_json(self, changeset: FakeChangeset, max_comment: int) -> Dict[str, Any]:
        comment = changeset.comment
        truncated = len(comment) > max_comment
        return {
            "changesetId": changeset.id,
            "author": {"displayName": changeset.display_name,
                       "uniqueName": changeset.author, "id": f"id-{changeset.author}"},
            "checkedInBy": {"displayName": changeset.display_name,
                            "uniqueName": changeset.author},
            "createdDate": changeset.created_date,
            "comment": comment[:max_comment] if truncated else comment,
            "commentTruncated": truncated,
        }

    def _changeset_changes(self, changeset_id: int, query: Dict[str, str]) -> None:
        target = next((c for c in self.history.changesets if c.id == changeset_id), None)
        if target is None:
            self._error(404, f"changeset {changeset_id} does not exist")
            return
        skip = int(query.get("$skip", 0))
        top = int(query.get("$top", 100))
        page = target.changes[skip:skip + min(top, self.server.page_cap)]  # type: ignore[attr-defined]

        value = []
        for change in page:
            item: Dict[str, Any] = {
                "path": change.item.path,
                "version": changeset_id,
                "isFolder": change.item.is_folder,
            }
            if change.item.is_branch:
                item["isBranch"] = True
            if change.item.is_symlink:
                item["isSymLink"] = True
            if not change.item.is_folder and change.item.content is not None:
                item["size"] = change.item.size
                item["hashValue"] = change.item.md5_base64
                item["contentMetadata"] = {"encoding": change.item.encoding,
                                           "isBinary": change.item.encoding == -1}
            entry: Dict[str, Any] = {
                "item": item,
                # Flags enum, comma separated - the shape that breaks naive parsers.
                "changeType": ", ".join(change.change_types),
            }
            if change.source_path:
                entry["sourceServerItem"] = change.source_path
            value.append(entry)
        self._json({"count": len(value), "value": value})

    def _items(self, query: Dict[str, str]) -> None:
        version_raw = query.get("versionDescriptor.version")
        version = int(version_raw) if version_raw else self.history._next_id - 1

        scope = query.get("scopePath")
        if scope:
            recursion = (query.get("recursionLevel") or "full").lower()
            if recursion == "onelevel":
                # Real TFS answers a one-level listing of $/ with the team project
                # folders. Returning only files here made a guard that depends on
                # seeing those folders silently untestable.
                prefix = scope.rstrip("/") + "/" if scope != "$/" else "$/"
                children = set()
                for changeset in self.history.changesets:
                    for change in changeset.changes:
                        path = change.item.path
                        if not path.startswith(prefix):
                            continue
                        rest = path[len(prefix):].split("/")[0]
                        if rest:
                            children.add(prefix + rest)
                value = [{"path": scope, "isFolder": True, "version": version}]
                value += [{"path": c, "isFolder": True, "version": version}
                          for c in sorted(children)]
                self._json({"count": len(value), "value": value})
                return
            items = self.history.tree_at(scope, version)
            self._json({"count": len(items), "value": [
                {"path": i.path, "size": i.size, "hashValue": i.md5_base64,
                 "isFolder": False, "version": version}
                for i in items]})
            return

        path = query.get("path")
        if not path:
            self._error(400, "path or scopePath is required")
            return
        content = self.history.content_at(path, version)
        if content is None:
            self._error(404, f"{path} does not exist at changeset {version}")
            return

        if query.get("download", "").lower() == "true" or \
                "octet-stream" in (self.headers.get("Accept") or ""):
            self._send(200, content, "application/octet-stream")
        else:
            self._json({"path": path, "version": version, "size": len(content),
                        "hashValue": base64.b64encode(hashlib.md5(content).digest()).decode(),
                        "content": content.decode("utf-8", "replace")})


class FakeTfs:
    """Start/stop wrapper for the fake server."""

    def __init__(self, history: TfvcHistory, token: str = "fake-pat-token",
                 api_versions: Tuple[str, ...] = ("4.1", "3.2", "2.0"),
                 page_cap: int = 100) -> None:
        self.history = history
        self.token = token
        self.api_versions = api_versions
        self.page_cap = page_cap
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.requests: List[Tuple[str, Dict[str, str]]] = []

    def __enter__(self) -> "FakeTfs":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    def start(self) -> "FakeTfs":
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.history = self.history          # type: ignore[attr-defined]
        server.token = self.token              # type: ignore[attr-defined]
        server.api_versions = self.api_versions  # type: ignore[attr-defined]
        server.page_cap = self.page_cap        # type: ignore[attr-defined]
        server.requests = self.requests        # type: ignore[attr-defined]
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=10)

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def called(self, fragment: str) -> bool:
        return any(fragment in path for path, _ in self.requests)


def demo_history(project: str = "DemoProject") -> TfvcHistory:
    """A TFVC history containing the cases that break naive converters."""
    h = TfvcHistory(project)
    root = f"$/{project}"

    h.commit("CONTOSO\\alice", "Create the main branch", [
        h.mkdir(f"{root}/Main", is_branch=True),
        h.add(f"{root}/Main/README.md", b"# Demo\n"),
        h.add(f"{root}/Main/src/app.cs", b"class App {}\n"),
    ])
    h.commit("CONTOSO\\bob",
             # Deliberately longer than the 80-character default truncation.
             "Improve the application entry point so that it prints a greeting, which "
             "makes the behaviour observable in tests and exceeds the default comment "
             "truncation length", [
                 h.edit(f"{root}/Main/src/app.cs", b"class App { void Run() {} }\n"),
             ])
    h.commit("CONTOSO\\alice", "Add a binary asset", [
        h.add(f"{root}/Main/assets/logo.bin", bytes(range(256)) * 4, encoding=-1),
    ])
    # Rename *and* edit in one change: the flags-enum trap.
    h.commit("CONTOSO\\carol", "Rename and update the entry point", [
        h.rename(f"{root}/Main/src/app.cs", f"{root}/Main/src/program.cs",
                 b"class Program { void Run() {} }\n", edited=True),
    ])
    # Branch creation by folder copy.
    h.commit("CONTOSO\\bob", "Branch for release 1.0", [
        h.branch(f"{root}/Release-1.0", f"{root}/Main", is_folder=True),
        h.branch(f"{root}/Release-1.0/README.md", f"{root}/Main/README.md", b"# Demo\n"),
        h.branch(f"{root}/Release-1.0/src/program.cs", f"{root}/Main/src/program.cs",
                 b"class Program { void Run() {} }\n"),
        h.branch(f"{root}/Release-1.0/assets/logo.bin", f"{root}/Main/assets/logo.bin",
                 bytes(range(256)) * 4),
    ])
    h.commit("CONTOSO\\alice", "Fix on the release branch", [
        h.edit(f"{root}/Release-1.0/src/program.cs",
               b"class Program { void Run() { /* fixed */ } }\n"),
    ])
    h.commit("CONTOSO\\bob", "Remove the obsolete readme", [
        h.delete(f"{root}/Main/README.md"),
    ])
    h.branches = [f"{root}/Main", f"{root}/Release-1.0"]
    return h
