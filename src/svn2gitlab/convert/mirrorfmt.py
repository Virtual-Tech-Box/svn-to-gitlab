"""Conventions for the mirror repository.

The mirror keeps Subversion history under `refs/remotes/svn/*`, with a `git-svn-id:`
trailer recording which Subversion revision each commit came from.

That layout was originally git-svn's. This tool no longer uses git-svn to produce it
— conversion is done by `dumpstream.py` and `native.py` — but the format is kept
deliberately: it is documented, widely understood by anyone who has migrated from
Subversion before, and the trailer is what makes incremental sync possible, since it
tells us exactly which revision the mirror already holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

SVN_PREFIX = "svn/"
TFVC_PREFIX = "tfvc/"

# `git-svn-id: <url>@<rev> <uuid>` — the trailer we write and later parse back.
_SVN_ID = re.compile(r"^git-svn-id:\s*(?P<url>\S+)@(?P<rev>\d+)\s+(?P<uuid>\S+)\s*$", re.M)
# The TFVC equivalent: `tfs-changeset-id: <collection><project>@<changeset>`.
_TFS_ID = re.compile(r"^tfs-changeset-id:\s*(?P<url>\S+)@(?P<rev>\d+)\s*$", re.M)


@dataclass(frozen=True)
class MirrorFormat:
    """How one source's history is recorded in the mirror repository.

    Subversion and TFVC produce the same *shape* — refs under `refs/remotes/<prefix>`
    and a trailer naming the source version each commit came from — so the export,
    verify and sync stages read the mirror through this description rather than
    hard-coding Subversion's conventions.
    """

    name: str                 # "svn" | "tfvc"
    prefix: str               # ref namespace under refs/remotes/
    trailer: str              # trailer key, e.g. "git-svn-id"
    version_word: str         # what one version is called, for human-facing messages
    pattern: Any = None

    @property
    def ref_root(self) -> str:
        return f"refs/remotes/{self.prefix}"

    def extract_version(self, message: str) -> Optional[int]:
        """The source version a commit came from, or None if the trailer is absent."""
        match = self.pattern.search(message or "")
        return int(match.group("rev")) if match else None


SVN_FORMAT = MirrorFormat("svn", SVN_PREFIX, "git-svn-id", "revision", _SVN_ID)
TFVC_FORMAT = MirrorFormat("tfvc", TFVC_PREFIX, "tfs-changeset-id", "changeset", _TFS_ID)


def format_for(source_kind: str) -> MirrorFormat:
    return TFVC_FORMAT if str(source_kind).lower() == "tfvc" else SVN_FORMAT


@dataclass
class FetchResult:
    revisions_fetched: int = 0
    last_revision: int = 0
    head_sha: str = ""
    duration: float = 0.0
    refs: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "revisions_fetched": self.revisions_fetched,
            "last_revision": self.last_revision,
            "head_sha": self.head_sha,
            "duration": round(self.duration, 1),
            "ref_count": len(self.refs),
        }


def extract_svn_revision(message: str) -> Optional[int]:
    """The Subversion revision a commit came from, or None if untagged."""
    match = _SVN_ID.search(message or "")
    return int(match.group("rev")) if match else None


def extract_svn_id(message: str) -> Optional[Dict[str, str]]:
    match = _SVN_ID.search(message or "")
    if not match:
        return None
    return {"url": match.group("url"), "rev": match.group("rev"), "uuid": match.group("uuid")}
