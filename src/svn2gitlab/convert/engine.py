"""Engine selection: the native converter or git-svn, behind one interface.

The export stage, the verifier and the incremental sync all talk to a "mirror": a
repository holding `refs/remotes/svn/*` and commits carrying `git-svn-id` trailers.
Both engines produce exactly that, so everything downstream is engine-agnostic and
switching engines cannot change the published result.

Choosing between them:

* **native** needs a local Subversion repository (to take a fulltext dump) and
  `svnadmin`. It has no Perl dependency and converts an order of magnitude faster.
* **git-svn** works directly against a remote URL and is the reference
  implementation, kept as a fallback for anything the native engine declines.
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Dict, List, Optional

from ..config import ConversionEngine, ConvertConfig, SourceConfig
from ..errors import ConversionError
from ..logging_setup import get_logger
from ..procs import IS_WINDOWS
from ..svn.analyze import DetectedLayout
from ..svn.authors import AuthorMap
from ..tools import ToolSet
from .git import Git
from .gitsvn import SVN_PREFIX, FetchResult, GitSvnMirror
from .native import NativeConverter, dump_command

log = get_logger("engine")


def resolve_engine(requested: ConversionEngine, tools: ToolSet,
                   has_local_repo: bool) -> str:
    """Turn `auto` into a concrete engine, or validate an explicit choice."""
    if requested == ConversionEngine.NATIVE:
        if not tools.svnadmin.available:
            raise ConversionError(
                "convert.engine is `native` but svnadmin is not available",
                "The native engine converts from an `svnadmin dump`. Install a "
                "Subversion server package, or set `convert.engine: git-svn`.",
            )
        return "native"

    if requested == ConversionEngine.GIT_SVN:
        if not tools.git_svn.available:
            raise ConversionError(
                f"convert.engine is `git-svn` but it is unusable: {tools.git_svn.detail}",
                "Install Git for Windows with its standard installer (MinGit omits "
                "Perl), or switch to `convert.engine: native`, which needs neither.",
            )
        return "git-svn"

    # auto: prefer native wherever it can run.
    if tools.svnadmin.available and has_local_repo:
        return "native"
    if tools.git_svn.available:
        log.info("using git-svn: %s", "no local repository available" if not has_local_repo
                 else "svnadmin is unavailable")
        return "git-svn"
    if tools.svnadmin.available:
        # No git-svn at all, so the source must be brought local first.
        return "native"
    raise ConversionError(
        "no usable conversion engine: neither svnadmin nor git-svn is available",
        "Install a Subversion client (which provides svnadmin) for the native "
        "engine, or Git for Windows' standard installer for git-svn. "
        "`svn2gitlab doctor` reports what is missing.",
    )


class NativeMirror:
    """A git-svn-shaped mirror produced by the native engine.

    Deliberately mirrors `GitSvnMirror`'s interface rather than introducing a second
    one, so `export.py` and the verifier never branch on engine.
    """

    def __init__(
        self,
        tools: ToolSet,
        repo_dir: Path,
        source_url: str,
        layout: DetectedLayout,
        convert: ConvertConfig,
        source: SourceConfig,
        local_repo: Path,
        authors: AuthorMap,
        uuid: str = "",
        cancel: Optional[Event] = None,
    ) -> None:
        tools.require("git", "svnadmin")
        self.tools = tools
        self.repo_dir = Path(repo_dir)
        self.git = Git(tools, self.repo_dir, cancel=cancel)
        self.source_url = source_url.rstrip("/")
        self.layout = layout
        self.convert = convert
        self.source = source
        self.local_repo = Path(local_repo)
        self.authors = authors
        self.uuid = uuid
        self.cancel = cancel
        self.authors_file: Optional[Path] = None
        self._converter: Optional[NativeConverter] = None

    # -- GitSvnMirror-compatible surface -------------------------------------

    @property
    def trunk_ref(self) -> str:
        from ..config import LayoutMode
        if self.layout.mode == LayoutMode.FLAT or self.layout.trunk in (None, ".", ""):
            return f"refs/remotes/{SVN_PREFIX}trunk"
        return f"refs/remotes/{SVN_PREFIX}trunk"

    def init(self, force: bool = False) -> None:
        self.git.init(bare=False, initial_branch=self.convert.default_branch)
        self.git.apply_base_config()

    @property
    def initialised(self) -> bool:
        return self.git.exists

    def last_fetched_revision(self) -> int:
        from .gitsvn import extract_svn_revision
        best = 0
        for ref in self.git.list_refs(f"refs/remotes/{SVN_PREFIX}"):
            meta = self.git.commit_meta(ref.sha)
            rev = extract_svn_revision(meta.get("message", "")) or 0
            best = max(best, rev)
        return best

    def revision_of(self, ref: str) -> int:
        from .gitsvn import extract_svn_revision
        meta = self.git.commit_meta(ref)
        return extract_svn_revision(meta.get("message", "")) or 0

    @property
    def _ignore_state_file(self) -> Path:
        return self.repo_dir / ".git" / "svn2gitlab-ignores.json"

    def _load_ignores(self) -> Dict[str, str]:
        try:
            return json.loads(self._ignore_state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_ignores(self, ignores: Dict[str, str]) -> None:
        """Persist svn:ignore across runs.

        An incremental dump only contains the revisions since the last conversion, so
        the properties set years ago are simply not in it. Without carrying the state
        forward, the generated .gitignore would silently disappear from the published
        repository on the first sync after the migration.
        """
        try:
            self._ignore_state_file.parent.mkdir(parents=True, exist_ok=True)
            self._ignore_state_file.write_text(json.dumps(ignores, indent=2, sort_keys=True),
                                               encoding="utf-8")
        except OSError as exc:
            log.warning("could not persist svn:ignore state: %s", exc)

    def svn_ignore_content(self, ref: Optional[str] = None) -> str:
        from .native import render_gitignore
        return render_gitignore(self._load_ignores())

    def remote_refs(self) -> Dict[str, str]:
        return {r.name: r.sha for r in self.git.list_refs(f"refs/remotes/{SVN_PREFIX}")}

    def _youngest_revision(self) -> int:
        """The newest revision in the local repository, or 0 if it cannot be read."""
        from ..svn.acquire import youngest_revision
        try:
            return youngest_revision(self.local_repo, self.tools)
        except Exception as exc:
            log.debug("could not read the youngest revision: %s", exc)
            return 0

    # -- conversion ----------------------------------------------------------

    def fetch(
        self,
        revision_start: int = 1,
        revision_end: Optional[int] = None,
        head_revision: int = 0,
        on_progress: Optional[Callable[[float, str], None]] = None,
        retries: int = 0,
    ) -> FetchResult:
        """Convert new revisions by streaming a dump through the native converter.

        Incremental by design: the dump starts one revision after whatever the
        repository already holds, so a re-run costs only the new history.
        """
        already = self.last_fetched_revision()
        start = max(revision_start, already + 1) if already else revision_start
        # svnadmin needs a concrete end revision, so resolve the repository's own
        # youngest rather than passing a keyword it does not understand.
        youngest = self._youngest_revision()
        end = revision_end or head_revision or youngest
        if youngest:
            end = min(end, youngest)

        if end and start > end:
            log.info("native mirror already at r%d (repository youngest r%d); "
                     "nothing to convert", already, youngest)
            result = FetchResult(revisions_fetched=0, last_revision=already)
            result.refs = self.remote_refs()
            result.head_sha = self.git.rev_parse(self.trunk_ref) or ""
            return result

        converter = NativeConverter(
            git_exe=self.git.exe,
            repo_dir=self.repo_dir,
            layout=self.layout,
            convert=self.convert,
            authors=self.authors,
            env=self.git._env(),
            cancel=self.cancel,
            default_domain="localhost",
            svn_url=self.source.url or self.source_url,
            uuid=self.uuid,
        )
        # Resuming: seed the converter with the refs already present so new commits
        # attach to existing history instead of starting fresh roots.
        converter.seed_from_repository(self.git)
        self._converter = converter

        cmd = dump_command(self.tools.svnadmin.path or "svnadmin", self.local_repo,
                           start=start if already else 0, end=end)
        log.info("native conversion: %s", " ".join(cmd))

        kwargs = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = 0x08000000
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                bufsize=1024 * 1024, **kwargs)
        stderr_lines: List[str] = []

        def drain() -> None:
            for raw in proc.stderr:  # type: ignore[union-attr]
                text = raw.decode("utf-8", "replace").rstrip()
                if text:
                    stderr_lines.append(text)

        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()

        try:
            native_result = converter.convert_stream(
                proc.stdout, head_revision=head_revision or 0, on_progress=on_progress)  # type: ignore[arg-type]
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            proc.wait(timeout=600)
            drainer.join(timeout=30)

        if proc.returncode not in (0, None):
            detail = "\n    ".join(stderr_lines[-10:])
            raise ConversionError(
                f"`svnadmin dump` failed (exit {proc.returncode})"
                + (f"\n    {detail}" if detail else ""),
                "Check that the repository path is readable and not corrupt "
                "(`svnadmin verify`).",
            )

        # Merge this slice's svn:ignore changes over whatever earlier runs recorded.
        merged = self._load_ignores()
        merged.update(converter.ignores)
        for directory in converter.cleared_ignores:
            merged.pop(directory, None)
        self._save_ignores(merged)

        result = FetchResult(
            revisions_fetched=native_result.revisions,
            last_revision=native_result.last_revision,
            duration=native_result.duration,
        )
        result.refs = self.remote_refs()
        result.head_sha = self.git.rev_parse(self.trunk_ref) or ""
        return result


def build_mirror(
    engine: str,
    tools: ToolSet,
    repo_dir: Path,
    source_url: str,
    layout: DetectedLayout,
    convert: ConvertConfig,
    source: SourceConfig,
    authors_file: Optional[Path],
    authors: AuthorMap,
    local_repo: Optional[Path],
    uuid: str = "",
    cancel: Optional[Event] = None,
):
    """Construct whichever mirror implementation the resolved engine calls for."""
    if engine == "native":
        if not local_repo:
            raise ConversionError(
                "the native engine needs a local Subversion repository",
                "Set `source.local_path` when running on the SVN server, or leave "
                "`source.fast_path: auto` so the repository is mirrored locally "
                "first.",
            )
        mirror = NativeMirror(
            tools=tools, repo_dir=repo_dir, source_url=source_url, layout=layout,
            convert=convert, source=source, local_repo=local_repo, authors=authors,
            uuid=uuid, cancel=cancel,
        )
        mirror.authors_file = authors_file
        return mirror

    mirror = GitSvnMirror(
        tools=tools, repo_dir=repo_dir, source_url=source_url, layout=layout,
        convert=convert, source=source, authors_file=authors_file, cancel=cancel,
    )
    return mirror
