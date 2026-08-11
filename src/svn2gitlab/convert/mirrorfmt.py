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
from typing import Dict, Optional

SVN_PREFIX = "svn/"

# `git-svn-id: <url>@<rev> <uuid>` — the trailer we write and later parse back.
_SVN_ID = re.compile(r"^git-svn-id:\s*(?P<url>\S+)@(?P<rev>\d+)\s+(?P<uuid>\S+)\s*$", re.M)


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
