"""Author mapping between SVN usernames and Git identities.

git-svn reads a file of `svnuser = Real Name <email>` lines. Getting this right is
the difference between a GitLab history where every commit links to a real user
and one where 40,000 commits are attributed to `jsmith@localhost`.

We support the standard git-svn format plus a couple of pragmatic extensions:
domain accounts (`ACME\\jsmith`, `jsmith@acme.local`) are normalised, and the file
round-trips comments so an operator can annotate it during review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from ..config import AuthorPolicy, AuthorsConfig
from ..errors import ConfigError
from ..logging_setup import get_logger

log = get_logger("authors")

_LINE = re.compile(r"^\s*(?P<svn>[^=#][^=]*?)\s*=\s*(?P<name>.*?)\s*<(?P<email>[^>]*)>\s*$")
_EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class Identity:
    name: str
    email: str

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"


class AuthorMap:
    """Case-insensitive SVN-username to Git-identity mapping."""

    def __init__(self, entries: Optional[Mapping[str, Identity]] = None) -> None:
        self._entries: Dict[str, Identity] = {}
        self._original_case: Dict[str, str] = {}
        for key, value in (entries or {}).items():
            self.set(key, value)

    # -- container behaviour -------------------------------------------------

    def __contains__(self, svn_user: str) -> bool:
        return (svn_user or "").lower() in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        for key, identity in self._entries.items():
            yield self._original_case.get(key, key), identity

    def get(self, svn_user: str) -> Optional[Identity]:
        return self._entries.get((svn_user or "").lower())

    def set(self, svn_user: str, identity: Identity) -> None:
        key = (svn_user or "").lower()
        self._entries[key] = identity
        self._original_case.setdefault(key, svn_user)

    def as_dict(self) -> Dict[str, str]:
        return {self._original_case.get(k, k): str(v) for k, v in self._entries.items()}

    # -- persistence ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path, strict: bool = False) -> "AuthorMap":
        amap = cls()
        if not path.is_file():
            return amap
        problems: List[str] = []
        for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = _LINE.match(line)
            if not match:
                problems.append(f"  line {lineno}: {raw.strip()!r}")
                continue
            svn_user = match.group("svn").strip()
            name = match.group("name").strip() or svn_user
            email = match.group("email").strip()
            amap.set(svn_user, Identity(name, email))
        if problems:
            message = (f"{len(problems)} unparseable line(s) in {path}:\n" + "\n".join(problems[:10]))
            if strict:
                raise ConfigError(message, "Each line must read: svnuser = Real Name <email@example.com>")
            log.warning("%s", message)
        log.info("loaded %d author mapping(s) from %s", len(amap), path)
        return amap

    def save(self, path: Path, header: Iterable[str] = ()) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        for note in header:
            lines.append(f"# {note}")
        if lines:
            lines.append("")
        width = max((len(u) for u, _ in self), default=0)
        for svn_user, identity in sorted(self, key=lambda kv: kv[0].lower()):
            lines.append(f"{svn_user.ljust(width)} = {identity}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info("wrote %d author mapping(s) to %s", len(self), path)

    def write_git_svn_file(self, path: Path) -> Path:
        """Emit the exact format git-svn expects (no comments, no padding surprises)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{svn_user} = {identity}" for svn_user, identity in
                 sorted(self, key=lambda kv: kv[0].lower())]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    # -- validation ----------------------------------------------------------

    def invalid_emails(self) -> List[Tuple[str, str]]:
        return [(user, ident.email) for user, ident in self
                if not _EMAIL_OK.match(ident.email or "")]

    def duplicate_emails(self) -> Dict[str, List[str]]:
        by_email: Dict[str, List[str]] = {}
        for user, ident in self:
            by_email.setdefault(ident.email.lower(), []).append(user)
        return {email: users for email, users in by_email.items() if len(users) > 1}


def normalise_username(svn_user: str) -> str:
    """`ACME\\jsmith` and `jsmith@acme.local` both become `jsmith`."""
    user = (svn_user or "").strip()
    if "\\" in user:
        user = user.rsplit("\\", 1)[-1]
    if "@" in user and not user.startswith("@"):
        user = user.split("@", 1)[0]
    return user


def humanise(svn_user: str) -> str:
    """Best-effort real name from a username: `john.smith` -> `John Smith`."""
    base = normalise_username(svn_user)
    parts = [p for p in re.split(r"[._\-]+", base) if p]
    if not parts:
        return svn_user or "unknown"
    return " ".join(p.capitalize() if p.islower() or p.isupper() else p for p in parts)


def build_author_map(
    svn_authors: Iterable[str],
    config: AuthorsConfig,
    existing: Optional[AuthorMap] = None,
) -> Tuple[AuthorMap, List[str]]:
    """Merge existing mappings with config overrides and fill the gaps per policy.

    Returns the complete map plus the list of authors that had to be synthesised, so
    the caller can surface them for review rather than silently guessing.
    """
    amap = AuthorMap()
    if existing:
        for user, identity in existing:
            amap.set(user, identity)

    for svn_user, spec in (config.mapping or {}).items():
        identity = _parse_identity(spec, svn_user)
        amap.set(svn_user, identity)

    synthesised: List[str] = []
    for raw_user in svn_authors:
        user = raw_user or ""
        if user in amap:
            continue
        if not user:
            email = config.no_author_email or f"{config.no_author_name}@{config.default_domain}"
            amap.set("(no author)", Identity(config.no_author_name, email))
            continue
        if config.policy == AuthorPolicy.STRICT:
            continue  # deliberately left unmapped; the caller turns this into an error
        base = normalise_username(user)
        amap.set(user, Identity(humanise(user), f"{base.lower()}@{config.default_domain}"))
        synthesised.append(user)

    return amap, synthesised


def _parse_identity(spec: str, fallback_user: str) -> Identity:
    spec = (spec or "").strip()
    match = re.match(r"^(?P<name>.*?)\s*<(?P<email>[^>]*)>$", spec)
    if match:
        return Identity(match.group("name").strip() or humanise(fallback_user),
                        match.group("email").strip())
    if _EMAIL_OK.match(spec):
        return Identity(humanise(fallback_user), spec)
    raise ConfigError(
        f"authors.mapping[{fallback_user!r}] = {spec!r} is not a valid identity",
        "Use either `Real Name <email@example.com>` or just `email@example.com`.",
    )


def unmapped(svn_authors: Iterable[str], amap: AuthorMap) -> List[str]:
    return sorted({a for a in svn_authors if (a or "") and a not in amap})


AUTHORS_HEADER = (
    "svn2gitlab author map",
    "",
    "One line per Subversion user:",
    "    svnuser = Real Name <email@example.com>",
    "",
    "The email address decides which GitLab user a commit is attributed to, so it must",
    "match the address on that person's GitLab account (primary or secondary).",
    "Entries generated automatically are marked below - review them before migrating.",
)
