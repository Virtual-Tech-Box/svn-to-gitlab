"""A minimal GitLab stand-in: the REST endpoints we call, plus a real Git HTTP server.

This exists to close the one gap the rest of the suite cannot reach. Everything up
to the push is exercised against real Subversion and real Git; the push itself
previously had no coverage beyond unit-tested error translation, which means the
step most likely to fail at the very end of a six-hour job was the least tested.

It is deliberately a *real* server rather than a mock object:

* the REST side answers the endpoints `GitLabClient` actually calls, and asserts the
  access token arrives in the expected header;
* the Git side shells out to `git http-backend`, so `git push` runs the genuine smart
  HTTP protocol against a genuine bare repository — credentials in the URL, packfile
  negotiation, porcelain output and all.

What it does not cover: Git LFS uploads (which need an LFS batch API) and GitLab's
push rules. Those remain rehearsal-only, and the README says so.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def find_git_http_backend(git_exe: str) -> Optional[Path]:
    """Locate `git-http-backend`, which carries a .exe suffix on Windows.

    Looking only for the extensionless name made the whole push suite skip on
    Windows - silently, and on the platform this project most needs to cover.
    """
    try:
        exec_path = subprocess.run([git_exe, "--exec-path"], capture_output=True,
                                   text=True, check=True).stdout.strip()
    except Exception:
        return None
    for name in ("git-http-backend", "git-http-backend.exe"):
        candidate = Path(exec_path) / name
        if candidate.is_file():
            return candidate
    return None


class GitLabState:
    """In-memory model of the instance, plus the bare repositories on disk."""

    def __init__(self, root: Path, token: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.namespaces: Dict[str, Dict[str, Any]] = {}
        self.projects: Dict[str, Dict[str, Any]] = {}
        self.protected: Dict[int, List[Dict[str, Any]]] = {}
        self.requests: List[Tuple[str, str]] = []   # (method, path) audit trail
        self.auth_failures = 0
        self._next_id = 1
        self.base_url = ""

    def new_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def repo_path(self, full_path: str) -> Path:
        return self.root / f"{full_path}.git"

    def create_namespace(self, full_path: str, kind: str = "group") -> Dict[str, Any]:
        namespace = {"id": self.new_id(), "full_path": full_path,
                     "path": full_path.split("/")[-1], "kind": kind,
                     "name": full_path.split("/")[-1]}
        self.namespaces[full_path] = namespace
        return namespace

    def create_project(self, full_path: str, git_exe: str, **fields: Any) -> Dict[str, Any]:
        repo = self.repo_path(full_path)
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([git_exe, "init", "--bare", "-q", str(repo)], check=True)
        # Without this, git http-backend refuses to accept a push.
        subprocess.run([git_exe, "-C", str(repo), "config", "http.receivepack", "true"], check=True)

        project = {
            "id": self.new_id(),
            "path": full_path.split("/")[-1],
            "name": fields.get("name") or full_path.split("/")[-1],
            "path_with_namespace": full_path,
            "web_url": f"{self.base_url}/{full_path}",
            "http_url_to_repo": f"{self.base_url}/{full_path}.git",
            "ssh_url_to_repo": f"git@fake:{full_path}.git",
            "default_branch": fields.get("default_branch") or "main",
            "empty_repo": True,
            "visibility": fields.get("visibility", "private"),
            "lfs_enabled": fields.get("lfs_enabled", True),
            "description": fields.get("description", ""),
            "statistics": {"repository_size": 0, "lfs_objects_size": 0},
        }
        self.projects[full_path] = project
        return project

    def project_by_id(self, project_id: int) -> Optional[Dict[str, Any]]:
        for project in self.projects.values():
            if project["id"] == project_id:
                return project
        return None

    def set_head(self, full_path: str, branch: str, git_exe: str) -> None:
        repo = self.repo_path(full_path)
        if repo.is_dir():
            subprocess.run([git_exe, "-C", str(repo), "symbolic-ref", "HEAD",
                            f"refs/heads/{branch}"], check=False)

    def head(self, full_path: str, git_exe: str) -> str:
        repo = self.repo_path(full_path)
        if not repo.is_dir():
            return ""
        result = subprocess.run([git_exe, "-C", str(repo), "symbolic-ref", "HEAD"],
                                capture_output=True, text=True, check=False)
        return result.stdout.strip()

    def refs(self, full_path: str, git_exe: str) -> Dict[str, str]:
        """Whatever the bare repository actually holds after a push."""
        repo = self.repo_path(full_path)
        if not repo.is_dir():
            return {}
        result = subprocess.run(
            [git_exe, "-C", str(repo), "for-each-ref", "--format=%(refname) %(objectname)"],
            capture_output=True, text=True, check=False)
        out: Dict[str, str] = {}
        for line in result.stdout.splitlines():
            name, _, sha = line.partition(" ")
            if name:
                out[name] = sha
        return out


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeGitLab/1.0"

    # -- plumbing ------------------------------------------------------------

    def log_message(self, *_args) -> None:  # keep pytest output readable
        pass

    @property
    def state(self) -> GitLabState:
        return self.server.state  # type: ignore[attr-defined]

    @property
    def git_exe(self) -> str:
        return self.server.git_exe  # type: ignore[attr-defined]

    def _body(self) -> bytes:
        """Read the request body, honouring chunked transfer encoding.

        git uses chunked encoding for pushes whose size it does not know up front,
        and BaseHTTPRequestHandler will not decode that for us.
        """
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size_line = self.rfile.readline().strip()
                if not size_line:
                    break
                try:
                    size = int(size_line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()  # trailing CRLF
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        token = self.headers.get("PRIVATE-TOKEN") or ""
        bearer = (self.headers.get("Authorization") or "").replace("Bearer ", "")
        if token == self.state.token or bearer == self.state.token:
            return True
        self.state.auth_failures += 1
        return False

    # -- dispatch ------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        self.state.requests.append((method, path))

        if path.startswith("/api/v4/"):
            if not self._authorised():
                self._json(401, {"message": "401 Unauthorized"})
                return
            self._api(method, path[len("/api/v4/"):], urllib.parse.parse_qs(parsed.query))
            return
        self._git_http(method, parsed)

    # -- REST ----------------------------------------------------------------

    def _api(self, method: str, route: str, query: Dict[str, List[str]]) -> None:
        state = self.state
        body: Dict[str, Any] = {}
        raw = self._body()
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {}

        if route == "user":
            self._json(200, {"id": 1, "username": "migration-bot",
                             "name": "Migration Bot", "is_admin": False})
            return
        if route == "version":
            self._json(200, {"version": "17.9.0-fake", "revision": "deadbee"})
            return
        if route == "users":
            self._json(200, [])         # no matching GitLab accounts
            return

        if route.startswith("namespaces/"):
            full = urllib.parse.unquote(route[len("namespaces/"):])
            namespace = state.namespaces.get(full)
            self._json(200, namespace) if namespace else self._json(404, {"message": "404 Not found"})
            return

        if route == "groups" and method == "POST":
            parent = body.get("parent_id")
            path = body["path"]
            if parent:
                parent_path = next((n["full_path"] for n in state.namespaces.values()
                                    if n["id"] == parent), "")
                path = f"{parent_path}/{path}" if parent_path else path
            self._json(201, state.create_namespace(path))
            return

        if route.startswith("groups/"):
            full = urllib.parse.unquote(route[len("groups/"):])
            group = state.namespaces.get(full)
            self._json(200, group) if group else self._json(404, {"message": "404 Not found"})
            return

        if route == "projects" and method == "POST":
            namespace_id = body.get("namespace_id")
            namespace_path = next((n["full_path"] for n in state.namespaces.values()
                                   if n["id"] == namespace_id), "")
            full = f"{namespace_path}/{body['path']}" if namespace_path else body["path"]
            if full in state.projects:
                self._json(400, {"message": {"path": ["has already been taken"]}})
                return
            self._json(201, state.create_project(full, self.git_exe, **body))
            return

        if route.startswith("projects/"):
            remainder = route[len("projects/"):]

            # projects/<id>/protected_branches[/<name>]
            if "/protected_branches" in remainder:
                ident, _, rest = remainder.partition("/protected_branches")
                project_id = int(urllib.parse.unquote(ident))
                if method == "POST":
                    state.protected.setdefault(project_id, []).append(body)
                    self._json(201, body)
                elif method == "DELETE":
                    name = urllib.parse.unquote(rest.lstrip("/"))
                    entries = state.protected.get(project_id, [])
                    state.protected[project_id] = [e for e in entries if e.get("name") != name]
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._json(200, state.protected.get(project_id, []))
                return

            if "/protected_tags" in remainder:
                self._json(201, body)
                return

            ident = urllib.parse.unquote(remainder)
            project = None
            if ident.isdigit():
                project = state.project_by_id(int(ident))
            else:
                project = state.projects.get(ident)

            if project is None:
                self._json(404, {"message": "404 Project Not Found"})
                return

            if method == "PUT":
                project.update({k: v for k, v in body.items() if k in
                                ("default_branch", "description", "visibility")})
                if "default_branch" in body:
                    # Real GitLab repoints the repository's HEAD when the default
                    # branch changes. Without this the fake would happily record the
                    # field while leaving HEAD on `master`, and every clone would come
                    # out empty - which is exactly the failure a caller would hit for
                    # real if we never made this API call.
                    state.set_head(project["path_with_namespace"], body["default_branch"],
                                   self.git_exe)
                self._json(200, project)
                return

            # Reflect reality: report the repository as non-empty once it has refs.
            refs = state.refs(project["path_with_namespace"], self.git_exe)
            project["empty_repo"] = not refs
            project["statistics"] = {"repository_size": 4096 * max(1, len(refs)),
                                     "lfs_objects_size": 0}
            self._json(200, project)
            return

        self._json(404, {"message": f"404 Not found: {route}"})

    # -- Git smart HTTP ------------------------------------------------------

    def _git_http(self, method: str, parsed) -> None:
        """Delegate to `git http-backend`, which speaks the real protocol."""
        backend = find_git_http_backend(self.git_exe)
        if backend is None:
            self._json(500, {"message": "git-http-backend not available"})
            return

        body = self._body()
        env = {
            "GIT_PROJECT_ROOT": str(self.state.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": parsed.path,
            "REQUEST_METHOD": method,
            "QUERY_STRING": parsed.query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": "migration-bot",
            "REMOTE_ADDR": self.client_address[0],
            "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
            "PATH": __import__("os").environ.get("PATH", ""),
        }
        result = subprocess.run([str(backend)], input=body, env=env,
                                capture_output=True, check=False)
        if result.returncode != 0:
            self._json(500, {"message": result.stderr.decode("utf-8", "replace")[:500]})
            return

        # CGI output: headers, blank line, body.
        head, _, payload = result.stdout.partition(b"\r\n\r\n")
        if not _:
            head, _, payload = result.stdout.partition(b"\n\n")
        headers = []
        status = 200
        for line in head.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            name, _, value = line.partition(":")
            value = value.strip()
            if name.lower() == "status":
                status = int(value.split()[0])
            else:
                headers.append((name, value))

        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FakeGitLab:
    """Start/stop wrapper. Use as a context manager."""

    def __init__(self, root: Path, git_exe: str, token: str = "glpat-faketoken12345") -> None:
        self.state = GitLabState(root, token)
        self.git_exe = git_exe
        self.token = token
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "FakeGitLab":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    def start(self) -> "FakeGitLab":
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.state = self.state          # type: ignore[attr-defined]
        server.git_exe = self.git_exe      # type: ignore[attr-defined]
        server.daemon_threads = True
        self._server = server
        self.state.base_url = f"http://127.0.0.1:{server.server_address[1]}"
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
        return self.state.base_url

    def refs(self, full_path: str) -> Dict[str, str]:
        return self.state.refs(full_path, self.git_exe)

    def called(self, method: str, fragment: str) -> bool:
        return any(m == method and fragment in p for m, p in self.state.requests)
