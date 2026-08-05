# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-08-05

First public release. Not yet tagged: pushing a `v1.0.0` tag builds the Windows
installer and opens a draft GitHub release.

### Added

**Migration pipeline** — `preflight → analyze → authors → acquire → convert →
export → push → verify`, with every stage recorded in SQLite so an interrupted run
resumes at the first incomplete stage instead of starting over.

**Source support** — any Subversion deployment reachable over `http`, `https`,
`svn`, `svn+ssh` or `file`: VisualSVN Server, Apache `mod_dav_svn`, `svnserve`, a
repository on local disk, or a hosted provider.

**Layout detection** — automatic recognition of standard `trunk`/`branches`/`tags`,
non-standard directory names, two-level nested branches, flat repositories, and
repositories holding several projects. Overridable per repository.

**Fast-path acquisition** — reads a local repository directly via `file://` with no
copy, takes an `svnadmin hotcopy` when isolation is wanted, or mirrors a remote
repository locally with `svnsync` (resumable) or `svnrdump`.

**Author mapping** — generates `authors.txt` from the repository's own history,
normalises domain-qualified accounts (`CONTOSO\user`) and authorless revisions, and
validates addresses. `strict` policy refuses to migrate with unmapped authors.

**Publishable export** — derived from the git-svn mirror rather than replacing it:
real branches and tags, `git-svn-id` stripped in favour of an `Svn-Revision:`
trailer, `.gitignore` generated from `svn:ignore`, and optional `.gitkeep` markers
for directories Git cannot otherwise keep. Deterministic, so re-derivation after a
sync produces identical commit SHAs.

**Tag fidelity** — a tag created as a pure directory copy is retargeted to the
commit it copied; a tag modified after creation keeps pointing at the modified
state, and the difference is reported.

**Git LFS** — planned against the whole history rather than HEAD, so binaries
deleted years ago are still found. Rewrites matching blobs into pointers and
uploads objects before the refs that reference them.

**GitLab publication** — gitlab.com and self-managed instances, private CAs, nested
group creation, project creation, default-branch and protection settings, and
pre-push checks for size limits and LFS availability.

**Verification** — compares Subversion content against the pushed Git objects by
computing the Git blob hash of each exported file locally, with no checkout or
archive. LFS files verify through the pointer OID without downloading anything.
Differences are classified as missing-either-side, content, symlink, or
eol-normalised.

**Reports** — a self-contained HTML report for people and a JSON manifest with a
SHA-256 checksum for audit.

**Incremental sync and cutover** — keeps GitLab in step with a live Subversion
server, picking up new committers automatically. Cutover locks Subversion *before*
the final sync so no commit can be lost in the gap, and the lock is reversible with
`svn2gitlab unlock`.

**Interfaces** — a 12-command CLI and a local web dashboard with live progress and
log streaming, both backed by the same state store.

**Batch migration** — many repositories from one configuration, with key-wise
inheritance of shared settings and optional parallel execution.

**Packaging** — PyInstaller bundle plus an Inno Setup installer for Windows Server
2019 and later, which detects missing Git and Subversion and says where to get them.

### Security

- Credentials resolve from `env:`, `file:` or `keyring:` indirection and never need
  to appear in a configuration file.
- All output passes through a redactor that scrubs registered secrets and structural
  patterns (credentials in URLs, `--password` arguments, `PRIVATE-TOKEN` headers,
  `glpat-` tokens).
- The token-bearing git remote is removed from the repository after a push.

### Known limitations

- `svn:externals` are reported but not migrated; Git has no equivalent.
- `svn:mergeinfo` is not reconstructed as Git merge commits.
- Per-file locks and `svn:needs-lock` have no Git equivalent.
- The cutover lock needs filesystem access to the Subversion repository; against a
  remote URL the tool reports that it could not lock rather than claiming success.
- Re-derived history can require a force push when history rewriting (LFS or
  metadata stripping) is enabled. Safe before cutover only.

[Unreleased]: https://github.com/Virtual-Tech-Box/svn-to-gitlab/commits/main
[1.0.0]: https://github.com/Virtual-Tech-Box/svn-to-gitlab/releases
