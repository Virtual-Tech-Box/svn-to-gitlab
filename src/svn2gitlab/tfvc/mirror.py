"""The TFVC mirror: the same interface the Subversion mirror presents.

`export.py`, the verifier and the incremental sync all talk to "a mirror" — a Git
repository holding source history under `refs/remotes/<prefix>/*` with a trailer per
commit recording which source version it came from. Presenting that same surface
here is what lets the entire second half of the pipeline be shared rather than
written twice.
"""

from __future__ import annotations

import time
from pathlib import Path
from threading import Event
from typing import Callable, Dict, List, Optional

from ..convert.git import Git
from ..convert.mirrorfmt import TFVC_FORMAT, FetchResult
from ..errors import ConversionError
from ..logging_setup import get_logger
from ..svn.authors import AuthorMap
from ..tools import ToolSet
from .client import TfvcClient
from .convert import MAIN_BRANCH_KEY, TfvcConverter, TfvcLayout, TfvcMapper

log = get_logger("tfvc.mirror")


class TfvcMirror:
    """Converts a TFVC collection into a Git mirror, incrementally."""

    fmt = TFVC_FORMAT

    def __init__(
        self,
        tools: ToolSet,
        repo_dir: Path,
        client: TfvcClient,
        layout: TfvcLayout,
        convert,                      # ConvertConfig
        authors: AuthorMap,
        collection_url: str = "",
        default_domain: str = "localhost",
        cancel: Optional[Event] = None,
    ) -> None:
        tools.require("git")
        self.tools = tools
        self.repo_dir = Path(repo_dir)
        self.git = Git(tools, self.repo_dir, cancel=cancel)
        self.client = client
        self.layout = layout
        self.convert = convert
        self.authors = authors
        self.collection_url = collection_url
        self.default_domain = default_domain
        self.cancel = cancel
        self.mapper = TfvcMapper(layout, convert.default_branch)

    # -- mirror surface -------------------------------------------------------

    @property
    def trunk_ref(self) -> str:
        return self.mapper.ref(MAIN_BRANCH_KEY)

    @property
    def initialised(self) -> bool:
        return self.git.exists

    def init(self, force: bool = False) -> None:
        self.git.init(bare=False, initial_branch=self.convert.default_branch)
        self.git.apply_base_config()

    def last_fetched_revision(self) -> int:
        """Highest changeset already converted, from the trailers in the mirror."""
        best = 0
        for ref in self.git.list_refs(self.fmt.ref_root):
            meta = self.git.commit_meta(ref.sha)
            version = self.fmt.extract_version(meta.get("message", "")) or 0
            best = max(best, version)
        return best

    def revision_of(self, ref: str) -> int:
        meta = self.git.commit_meta(ref)
        return self.fmt.extract_version(meta.get("message", "")) or 0

    def remote_refs(self) -> Dict[str, str]:
        return {r.name: r.sha for r in self.git.list_refs(self.fmt.ref_root)}

    def svn_ignore_content(self, ref: Optional[str] = None) -> str:
        """TFVC has no per-folder ignore property, so there is nothing to translate.

        `.tfignore` files exist but are ordinary versioned files: they migrate as
        content like anything else, and inventing a `.gitignore` from them would be
        guessing at semantics that do not match.
        """
        return ""

    # -- conversion -----------------------------------------------------------

    def fetch(
        self,
        revision_start: int = 1,
        revision_end: Optional[int] = None,
        head_revision: int = 0,
        on_progress: Optional[Callable[[float, str], None]] = None,
        retries: int = 0,
    ) -> FetchResult:
        started = time.time()
        already = self.last_fetched_revision()
        from_id = max(revision_start, already + 1) if already else None

        if on_progress:
            on_progress(0.02, "listing changesets")

        changesets = list(self.client.iter_changesets(
            item_path=self.layout.project_root,
            from_id=from_id,
            to_id=revision_end,
        ))
        if already:
            changesets = [c for c in changesets if c.id > already]

        if not changesets:
            log.info("TFVC mirror already at changeset %d; nothing to convert", already)
            result = FetchResult(revisions_fetched=0, last_revision=already)
            result.refs = self.remote_refs()
            result.head_sha = self.git.rev_parse(self.trunk_ref) or ""
            return result

        converter = TfvcConverter(
            client=self.client,
            git_exe=self.git.exe,
            repo_dir=self.repo_dir,
            layout=self.layout,
            authors=self.authors,
            default_branch=self.convert.default_branch,
            default_domain=self.default_domain,
            env=self.git._env(),
            cancel=self.cancel,
            collection_url=self.collection_url,
        )
        self._seed(converter)

        outcome = converter.convert(
            changesets,
            on_progress=(lambda f, m: on_progress(0.05 + 0.93 * f, m)) if on_progress else None,
        )
        self.last_result = outcome

        result = FetchResult(
            revisions_fetched=outcome.changesets,
            last_revision=outcome.last_changeset,
            duration=time.time() - started,
        )
        result.refs = self.remote_refs()
        result.head_sha = self.git.rev_parse(self.trunk_ref) or ""
        if outcome.unresolved_branches:
            log.warning("%d branch copy/copies could not be resolved: %s",
                        len(outcome.unresolved_branches),
                        "; ".join(outcome.unresolved_branches[:5]))
        return result

    def _seed(self, converter: TfvcConverter) -> None:
        """Attach an incremental run to the refs already in the repository.

        Without this a second pass starts every branch from a fresh root and orphans
        everything the first pass produced.
        """
        root = self.fmt.ref_root
        seeded = 0
        for ref in self.git.list_refs(root):
            name = ref.name[len(root):]
            key = MAIN_BRANCH_KEY if name == self.convert.default_branch else name
            meta = self.git.commit_meta(ref.sha)
            version = self.fmt.extract_version(meta.get("message", "")) or 0
            converter.heads[key] = ref.sha
            converter.history.setdefault(key, []).append((version, ref.sha))
            seeded += 1
        if seeded:
            log.info("resuming TFVC conversion from %d existing ref(s)", seeded)
