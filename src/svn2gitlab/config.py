"""Configuration model.

One YAML file describes a whole migration programme: where SVN lives (any of the
supported access methods), how its history should be reshaped, and where in GitLab
it lands. A single config can carry many repositories, each inheriting the shared
source/target defaults and overriding only what differs.

Secrets are never written into the config in plain text if you don't want them to
be: any string field marked as a secret accepts `env:VAR`, `file:/path/to/secret`
or `keyring:service/user` and is resolved at load time.
"""

from __future__ import annotations

import os
import re
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ConfigError
from .logging_setup import REDACTOR

# --------------------------------------------------------------------------- #
# Secret resolution
# --------------------------------------------------------------------------- #

_SECRET_PREFIX = re.compile(r"^(env|file|keyring):(.+)$", re.I)


def resolve_secret(value: Optional[str], field_name: str = "secret") -> Optional[str]:
    """Expand `env:` / `file:` / `keyring:` indirections; register the result for redaction."""
    if value is None:
        return None
    value = str(value)
    match = _SECRET_PREFIX.match(value.strip())
    if match:
        kind, ref = match.group(1).lower(), match.group(2).strip()
        if kind == "env":
            resolved = os.environ.get(ref)
            if resolved is None:
                raise ConfigError(
                    f"{field_name} references environment variable {ref!r}, which is not set",
                    f"Set it before running, e.g. `set {ref}=...` (cmd) or `$env:{ref}='...'` (PowerShell).",
                )
        elif kind == "file":
            path = Path(os.path.expandvars(os.path.expanduser(ref)))
            if not path.is_file():
                raise ConfigError(f"{field_name} references file {path}, which does not exist",
                                  "Create the file containing only the secret, or use env: instead.")
            resolved = path.read_text(encoding="utf-8").strip()
        else:  # keyring
            try:
                import keyring  # type: ignore
            except ImportError as exc:
                raise ConfigError(f"{field_name} uses keyring: but the `keyring` package is not installed",
                                  "pip install keyring, or switch to env:/file:.") from exc
            service, _, user = ref.partition("/")
            resolved = keyring.get_password(service, user or "")
            if resolved is None:
                raise ConfigError(f"{field_name}: no keyring entry for {ref!r}",
                                  "Store it first: `keyring set <service> <user>`.")
    else:
        resolved = value
    REDACTOR.add(resolved)
    return resolved


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #

class LayoutMode(str, Enum):
    AUTO = "auto"            # probe the repository and decide
    STANDARD = "standard"    # trunk/ branches/ tags/ at the root
    CUSTOM = "custom"        # explicit paths given below
    FLAT = "flat"            # the whole repo is one linear line of development
    MULTI_PROJECT = "multi"  # <project>/trunk, <project>/branches, ... per subdirectory


class FastPath(str, Enum):
    AUTO = "auto"        # pick the best available for this source
    HOTCOPY = "hotcopy"  # svnadmin hotcopy from a local repository path
    RDUMP = "rdump"      # svnrdump the remote into a fresh local repository
    NONE = "none"        # talk to the live server with git-svn directly


class TagStyle(str, Enum):
    ANNOTATED = "annotated"
    LIGHTWEIGHT = "lightweight"
    BRANCHES = "branches"    # keep tag paths as branches (they were modified after creation)


class Visibility(str, Enum):
    PRIVATE = "private"
    INTERNAL = "internal"
    PUBLIC = "public"


class VerifyMode(str, Enum):
    FULL = "full"       # byte-for-byte content comparison of every branch tip
    TREE = "tree"       # path/size comparison only
    COUNTS = "counts"   # revision and author reconciliation only
    OFF = "off"


class AuthorPolicy(str, Enum):
    STRICT = "strict"      # fail if any SVN author is unmapped
    GENERATE = "generate"  # synthesise user@default_domain for unmapped authors


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class LayoutConfig(_Base):
    mode: LayoutMode = LayoutMode.AUTO
    trunk: Optional[str] = None
    branches: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    # Paths matching this regex are excluded from the conversion entirely.
    ignore_paths: Optional[str] = None
    include_paths: Optional[str] = None
    # For multi-project layouts: only migrate these top-level projects (empty = all).
    projects: List[str] = Field(default_factory=list)

    @field_validator("trunk", "ignore_paths", "include_paths")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) and v.strip() else None

    @model_validator(mode="after")
    def _check_custom(self) -> "LayoutConfig":
        if self.mode == LayoutMode.CUSTOM and not (self.trunk or self.branches or self.tags):
            raise ValueError("layout.mode=custom requires at least one of trunk/branches/tags")
        for pattern in (self.ignore_paths, self.include_paths):
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"invalid path regex {pattern!r}: {exc}") from exc
        return self


class SourceConfig(_Base):
    """Where the Subversion history comes from.

    Exactly one of `url` or `local_path` is required. `local_path` unlocks the
    hotcopy fast path and the read-only cutover lock, so prefer it when the tool
    runs on the SVN server itself.
    """

    url: Optional[str] = None
    local_path: Optional[str] = None
    # Subdirectory inside the repository to migrate (for one-repo-many-projects setups).
    subpath: Optional[str] = None

    username: Optional[str] = None
    password: Optional[str] = None
    trust_server_cert: bool = True
    config_dir: Optional[str] = None

    layout: LayoutConfig = Field(default_factory=LayoutConfig)
    fast_path: FastPath = FastPath.AUTO
    revision_start: int = 1
    revision_end: Optional[int] = None  # None = HEAD

    @field_validator("password")
    @classmethod
    def _resolve_password(cls, v: Optional[str]) -> Optional[str]:
        return resolve_secret(v, "source.password")

    @field_validator("subpath")
    @classmethod
    def _clean_subpath(cls, v: Optional[str]) -> Optional[str]:
        # Normalised here rather than in the model validator: this model uses
        # validate_assignment, so assigning to a field from an after-validator
        # re-enters that validator and recurses forever.
        return v.strip().strip("/") or None if isinstance(v, str) else None

    @field_validator("url")
    @classmethod
    def _normalise_url(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = v.strip().rstrip("/")
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https", "svn", "svn+ssh", "file"):
            raise ValueError(
                f"unsupported SVN URL scheme {parsed.scheme!r}; expected http, https, svn, svn+ssh or file"
            )
        if parsed.username or parsed.password:
            # Credentials belong in the dedicated fields, not the URL we log everywhere.
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc += f":{parsed.port}"
            v = urlunparse(parsed._replace(netloc=netloc))
        return v

    @model_validator(mode="after")
    def _check_source(self) -> "SourceConfig":
        if not self.url and not self.local_path:
            raise ValueError("source requires either `url` or `local_path`")
        if self.revision_start < 0:
            raise ValueError("source.revision_start must be >= 0")
        if self.revision_end is not None and self.revision_end < self.revision_start:
            raise ValueError("source.revision_end must be >= revision_start")
        return self

    def effective_url(self) -> str:
        """A URL git-svn can use, preferring the local repository when present."""
        if self.local_path:
            return Path(self.local_path).resolve().as_uri()
        return self.url or ""


class AuthorsConfig(_Base):
    file: Optional[str] = None
    default_domain: str = "example.invalid"
    policy: AuthorPolicy = AuthorPolicy.GENERATE
    # Explicit inline mappings win over the file.
    mapping: Dict[str, str] = Field(default_factory=dict)
    # SVN often records no author for revisions created by hooks/imports.
    no_author_name: str = "svn"
    no_author_email: Optional[str] = None


class LfsConfig(_Base):
    enabled: bool = False
    size_threshold_mb: int = 50
    extensions: List[str] = Field(default_factory=list)
    paths: List[str] = Field(default_factory=list)
    # GitLab SaaS rejects single files above this; we warn during preflight.
    warn_single_file_mb: int = 100

    @field_validator("extensions")
    @classmethod
    def _clean_ext(cls, v: List[str]) -> List[str]:
        return [e.lower().lstrip("*.").strip() for e in v if e and e.strip()]

    @model_validator(mode="after")
    def _check(self) -> "LfsConfig":
        if self.size_threshold_mb < 1:
            raise ValueError("lfs.size_threshold_mb must be >= 1")
        return self


class ConvertConfig(_Base):
    default_branch: str = "main"
    # Strip the `git-svn-id:` trailer from commit messages in the pushed history.
    strip_svn_metadata: bool = True
    # Keep a machine-readable `Svn-Revision: NNN` trailer so revisions stay traceable.
    keep_revision_trailer: bool = True
    tag_style: TagStyle = TagStyle.ANNOTATED
    # SVN tags are directory copies; point the tag at the copy's parent when the copy
    # commit itself changed nothing, which is what users expect.
    tag_from_parent: bool = True
    branch_prefix: str = ""
    tag_prefix: str = ""
    # Translate svn:ignore properties into a committed .gitignore.
    generate_gitignore: bool = True
    # Drop branches/tags whose SVN path was deleted before HEAD.
    include_deleted_refs: bool = False
    # git-svn cannot represent empty directories; optionally materialise .gitkeep.
    preserve_empty_dirs: bool = False
    # Rewrite refs so branch names are valid in git (spaces, ~, ^, .. etc.).
    sanitize_ref_names: bool = True
    lfs: LfsConfig = Field(default_factory=LfsConfig)

    @field_validator("default_branch")
    @classmethod
    def _valid_branch(cls, v: str) -> str:
        v = v.strip()
        if not v or v.startswith("-") or any(c in v for c in " ~^:?*[\\"):
            raise ValueError(f"invalid default branch name {v!r}")
        return v


class TargetConfig(_Base):
    """Where the history lands. Works for gitlab.com SaaS and any self-managed instance."""

    gitlab_url: str = "https://gitlab.com"
    token: Optional[str] = None
    # Personal/group/project access token or OAuth token; `oauth2` for the latter.
    token_kind: str = "private"  # private | oauth2 | job
    namespace: Optional[str] = None            # group path, may be nested: "acme/legacy"
    project: Optional[str] = None              # project path within the namespace
    project_name: Optional[str] = None         # human-readable name (defaults to project)
    description: Optional[str] = None
    visibility: Visibility = Visibility.PRIVATE
    create_namespace: bool = True
    # Push behaviour
    push_branches: bool = True
    push_tags: bool = True
    protect_default_branch: bool = True
    set_default_branch: bool = True
    # If the project already has commits, refuse rather than clobber.
    allow_non_empty_project: bool = False
    # Direct git remote override; when set, the REST API is not used at all.
    remote_url: Optional[str] = None
    ssh_key: Optional[str] = None
    verify_tls: bool = True
    ca_bundle: Optional[str] = None
    api_timeout: int = 60

    @field_validator("token")
    @classmethod
    def _resolve_token(cls, v: Optional[str]) -> Optional[str]:
        return resolve_secret(v, "target.token")

    @field_validator("gitlab_url")
    @classmethod
    def _clean_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("target.gitlab_url must start with http:// or https://")
        return v

    @field_validator("namespace", "project")
    @classmethod
    def _clean_path(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().strip("/") if isinstance(v, str) and v.strip() else None

    @model_validator(mode="after")
    def _check_target(self) -> "TargetConfig":
        if not self.remote_url and not self.token:
            raise ValueError("target requires `token` (or a fully specified `remote_url`)")
        if self.token_kind not in ("private", "oauth2", "job"):
            raise ValueError("target.token_kind must be private, oauth2 or job")
        return self

    def full_path(self) -> str:
        parts = [p for p in (self.namespace, self.project) if p]
        return "/".join(parts)


class SyncConfig(_Base):
    enabled: bool = False
    interval_minutes: int = 60
    # Push each sync round even when nothing new arrived (keeps mirrors provably fresh).
    push_when_unchanged: bool = False
    # On cutover, make the SVN repository reject new commits.
    lock_svn_on_cutover: bool = True
    lock_message: str = (
        "This repository has been migrated to GitLab and is now read-only. "
        "Please use the GitLab project instead."
    )
    # Allow these SVN users to keep committing after the lock (e.g. the migration account).
    lock_exempt_users: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "SyncConfig":
        if self.interval_minutes < 1:
            raise ValueError("sync.interval_minutes must be >= 1")
        return self


class VerifyConfig(_Base):
    mode: VerifyMode = VerifyMode.FULL
    # Which branches to content-verify; empty means the default branch only.
    branches: List[str] = Field(default_factory=list)
    verify_all_branches: bool = False
    verify_tags: bool = False
    # SVN applies eol-style translation on export; accept a normalised match.
    normalize_eol: bool = True
    # Fail the run when verification finds differences.
    fail_on_mismatch: bool = True
    max_reported_diffs: int = 200


class RepositoryConfig(_Base):
    """One repository within a migration programme.

    The section overrides are held as raw mappings rather than models. They are
    *fragments*: `source: {subpath: alpha}` is a perfectly valid override even though
    it would be an invalid SourceConfig on its own. Validation happens once the
    fragment has been merged onto the shared defaults, which is also where the error
    message can name the repository responsible.
    """

    name: str
    source: Optional[Dict[str, Any]] = None
    target: Optional[Dict[str, Any]] = None
    convert: Optional[Dict[str, Any]] = None
    authors: Optional[Dict[str, Any]] = None
    verify: Optional[Dict[str, Any]] = None
    sync: Optional[Dict[str, Any]] = None
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        v = v.strip()
        if not v or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", v):
            raise ValueError(
                f"repository name {v!r} must start alphanumeric and contain only letters, "
                "digits, dot, underscore or hyphen"
            )
        return v


class MigrationConfig(_Base):
    """Top-level document."""

    version: int = 1
    name: str = "migration"
    workdir: str = "./svn2gitlab-work"
    # Defaults inherited by every repository entry. Raw mappings for the same reason
    # as RepositoryConfig: in a batch migration the shared `source` may legitimately
    # carry only credentials, with each repository supplying its own URL.
    source: Optional[Dict[str, Any]] = None
    target: Optional[Dict[str, Any]] = None
    convert: Optional[Dict[str, Any]] = None
    authors: Optional[Dict[str, Any]] = None
    verify: Optional[Dict[str, Any]] = None
    sync: Optional[Dict[str, Any]] = None
    repositories: List[RepositoryConfig] = Field(default_factory=list)
    # How many repositories to migrate at once in batch mode.
    parallelism: int = 1
    # Extra directories to search for svn/git executables.
    tool_dirs: List[str] = Field(default_factory=list)

    _source_path: Optional[Path] = None

    @model_validator(mode="after")
    def _check(self) -> "MigrationConfig":
        if self.version != 1:
            raise ValueError(f"unsupported config version {self.version}; this build understands version 1")
        if self.parallelism < 1:
            raise ValueError("parallelism must be >= 1")
        if not self.repositories:
            if not self.source or not self.target:
                raise ValueError(
                    "a single-repository config needs top-level `source` and `target` sections; "
                    "a batch config needs a `repositories` list"
                )
            self.repositories = [RepositoryConfig(name=_slug(self.name))]
        names = [r.name.lower() for r in self.repositories]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate repository names: {', '.join(sorted(dupes))}")
        # Resolve every repository now so a bad merge is a load-time error rather
        # than a surprise three stages into a migration.
        for repo in self.repositories:
            self.resolved(repo)
        return self

    # -- resolution ----------------------------------------------------------

    def workdir_path(self) -> Path:
        base = Path(os.path.expandvars(os.path.expanduser(self.workdir)))
        if not base.is_absolute() and self._source_path:
            base = (self._source_path.parent / base).resolve()
        return base.resolve()

    def resolved(self, repo: RepositoryConfig) -> "ResolvedRepo":
        """Merge a repository entry with the top-level defaults and validate the result."""
        ctx = f"repository {repo.name!r}"
        source = _merge(self.source, repo.source, SourceConfig, f"{ctx}: source", required=True)
        target = _merge(self.target, repo.target, TargetConfig, f"{ctx}: target", required=True)
        convert = _merge(self.convert, repo.convert, ConvertConfig, f"{ctx}: convert")
        authors = _merge(self.authors, repo.authors, AuthorsConfig, f"{ctx}: authors")
        verify = _merge(self.verify, repo.verify, VerifyConfig, f"{ctx}: verify")
        sync = _merge(self.sync, repo.sync, SyncConfig, f"{ctx}: sync")
        if not target.project:
            target = target.model_copy(update={"project": _slug(repo.name)})
        return ResolvedRepo(
            name=repo.name, source=source, target=target, convert=convert,
            authors=authors, verify=verify, sync=sync,
            workdir=self.workdir_path() / repo.name,
        )

    def all_resolved(self, only: Optional[List[str]] = None) -> List["ResolvedRepo"]:
        wanted = {n.lower() for n in only} if only else None
        out = []
        for repo in self.repositories:
            if not repo.enabled and not (wanted and repo.name.lower() in wanted):
                continue
            if wanted and repo.name.lower() not in wanted:
                continue
            out.append(self.resolved(repo))
        if wanted:
            found = {r.name.lower() for r in out}
            missing = wanted - found
            if missing:
                raise ConfigError(
                    f"no such repository in config: {', '.join(sorted(missing))}",
                    f"Known repositories: {', '.join(r.name for r in self.repositories)}",
                )
        return out


class ResolvedRepo(_Base):
    """A fully merged, self-contained plan for one repository."""

    name: str
    source: SourceConfig
    target: TargetConfig
    convert: ConvertConfig
    authors: AuthorsConfig
    verify: VerifyConfig
    sync: SyncConfig
    workdir: Path

    @property
    def mirror_dir(self) -> Path:
        return self.workdir / "mirror"

    @property
    def export_dir(self) -> Path:
        return self.workdir / "export"

    @property
    def local_repo_dir(self) -> Path:
        """Where a hotcopy / rdump-loaded repository is materialised."""
        return self.workdir / "svnrepo"

    @property
    def reports_dir(self) -> Path:
        return self.workdir / "reports"

    @property
    def authors_file(self) -> Path:
        if self.authors.file:
            return Path(os.path.expandvars(os.path.expanduser(self.authors.file)))
        return self.workdir / "authors.txt"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursive mapping merge so nested sections override key-wise, not wholesale.

    Without this, `source: {layout: {mode: custom}}` on a repository would discard the
    shared `source.layout.ignore_paths` instead of adding to it.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _merge(base: Optional[Dict[str, Any]], override: Optional[Dict[str, Any]],
           model_cls, ctx: str, required: bool = False):
    """Merge the shared defaults with a repository fragment, then validate."""
    if base is None and override is None:
        if required:
            raise ConfigError(
                f"{ctx} is not configured",
                "Add the section either at the top level (shared by every repository) "
                "or on the repository entry itself.",
            )
        return model_cls()

    merged = _deep_merge(base or {}, override or {})
    try:
        return model_cls(**merged)
    except Exception as exc:
        raise ConfigError(f"{ctx}:\n{_format_validation(exc)}") from exc


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return slug or "repository"


def load_config(path: os.PathLike | str) -> MigrationConfig:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}",
                          "Generate a starting point with `svn2gitlab init --output migration.yaml`.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {path}: {exc}",
                          "Check indentation - YAML requires spaces, never tabs.") from exc
    if raw is None:
        raise ConfigError(f"{path} is empty")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    try:
        config = MigrationConfig(**raw)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"invalid configuration in {path}:\n{_format_validation(exc)}") from exc
    config._source_path = path
    return config


def _format_validation(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    lines = []
    for err in errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        lines.append(f"  {loc or '<root>'}: {err.get('msg', 'invalid')}")
    return "\n".join(lines) or str(exc)


def dump_config(config: MigrationConfig, redact: bool = True) -> str:
    data = config.model_dump(mode="json", exclude_none=True)
    if redact:
        _redact_tree(data)
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _redact_tree(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key in ("password", "token", "ssh_key") and isinstance(value, str):
                node[key] = "***"
            else:
                _redact_tree(value)
    elif isinstance(node, list):
        for item in node:
            _redact_tree(item)
