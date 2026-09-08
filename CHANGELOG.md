# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **TFVC branch content came from the wrong place.** A folder-level `branch` change
  was treated as a whole-tree copy from the *converted* source branch's head, and the
  per-file `branch` records TFVC emits alongside it were discarded. TFVC branches from
  any version, not only the source's tip, so that head is frequently the wrong tree —
  and a branch cut from an already-stale branch inherits the error and adds its own.
  On the first production migration this left roughly 4,800 files stale across ten
  branches, untouched on the mainline and worst on branches cut from branches. The
  per-file records carry each file's own item version and are now the authority.
  Deduplication on the content hash happens before any request, so a branch copy still
  costs metadata rather than bandwidth.

- **The conversion and the analysis could disagree about which branch is the
  mainline.** `analyze` honoured the operator's `source.branch_roots` ordering while
  the conversion fell back to choosing alphabetically, so the report named one branch
  as trunk while the export published a different one as `main`. Verification cannot
  catch this — it compares `main` against whatever the conversion decided `main` meant,
  so a side branch published as the default branch verifies perfectly. The two layouts
  must now agree or the run stops.

- **Verification under-reported its own findings.** `difference_count` counted the
  list *after* `verify.max_reported_diffs` truncated it, so a branch with 1,245
  differences reported 200. The count is never capped now; only the listing is, and
  `differences_omitted` records how much was withheld.

- Verification's truncation warning and every skip reason were written to an attribute
  `BranchVerification` does not declare, and were silently discarded.

- The migration manifest lost all verification detail when verification *failed* — the
  one case where it matters — because the stage raises before returning a result. The
  error text told the operator to go and read a report that had been emptied.

- Files with no extension were bucketed in the manifest under an empty JSON key, which
  Windows PowerShell's `ConvertFrom-Json` refuses to load, making the report
  unreadable on the platform the tool is built for. They now bucket under
  `(no extension)`.

- **The Windows installer now bundles Git instead of downloading it.** Fetching it
  on demand failed at a client site, and always would have: migration hosts sit
  behind VPNs and proxies that block public DNS, which is precisely the environment
  this tool is built for. Git for Windows (MinGit) is downloaded when the release is
  built and shipped inside the package, so installation needs no network at all. The
  release build proves it by stripping every Git directory from PATH and confirming
  the tool still resolves one from inside its own package.

- The Windows installer refused to run on **Windows Server 2016**. Its minimum OS
  was set to build 10.0.17763 (Server 2019) when nothing in the tool needs anything
  newer; the gate is now 10.0.14393, which is Server 2016 / Windows 10 1607.

### Added

- **TFVC support: migrate from TFS to GitLab.** `source.kind: tfvc` reads a Team
  Foundation Version Control history over the REST API (TFS 2015/2017/2018, Azure
  DevOps Server and Services) and converts changesets into Git commits with the same
  fast-import writer the Subversion engine uses. Everything downstream — export, LFS,
  GitLab push, byte-for-byte verification and incremental sync — is shared, not
  duplicated.

  REST is used deliberately rather than `tf.exe` or the .NET client libraries, so
  the migrator stays cross-platform and free of Windows-only dependencies.

  Three documented API behaviours are handled explicitly because each silently
  corrupts a migration otherwise: comments truncate to 80 characters by default,
  `changeType` is a comma-separated flags enum where `rename, edit` means both, and
  `hashValue` is a base64 MD5. Branch creation is resolved with fast-import's `ls`
  rather than replaying the per-file `branch` changes TFVC emits, so branching a
  large tree costs one operation instead of re-downloading it.

  Not migrated, and reported in the analysis: TFVC labels (no Git equivalent), merge
  topology (content is correct, branch shape is simplified), and ignore rules. The
  cutover lock remains Subversion-only; for TFVC deny "Check in" in TFS.

## [1.0.1] - 2026-08-11

Fixes a release-blocking problem on Windows. Git for Windows removed `git svn` in
v2.54.0, so it is absent from every current Windows install — and 1.0.0 treated it
as mandatory, refusing to run on the very platform this tool targets. 1.0.1 removes
the dependency entirely.

### Fixed

- `git-svn` is no longer a hard requirement. `doctor` previously failed, and the
  tool refused to run, on any machine whose Git build omits Perl — which is the
  common case on Windows.
- CI no longer reported success on Windows while the toolchain was unusable: the
  tool-check step had `continue-on-error` set, so a missing `svn` **and** a missing
  `git-svn` still produced a green build with every end-to-end test silently
  skipped.
- Incremental sync no longer refuses to push after the first migration. The
  non-empty-project guard, which exists to stop the tool overwriting an unrelated
  project, fired on the tool's own re-pushes and broke the entire sync workflow.
  A push recorded in the state store now marks the project as ours to update.

### Removed

- **git-svn, entirely.** It is no longer probed for, reported by `doctor`, shown in
  the web dashboard, or selectable via `convert.engine`. Upstream removed `git svn`
  from Git for Windows in v2.54.0, this project's own engine produces identical
  trees, and keeping it as an "optional fallback" achieved nothing except putting a
  red `missing` row in front of operators on machines that were working perfectly.
  `convert.engine` now raises a clear error explaining the removal rather than a
  generic unknown-key failure.

### Added

- Golden-master trees: the exact Git tree objects the fixture must produce are
  pinned in `tests/test_golden_master.py`, verified byte-identical to git-svn and
  reproducible across runs. Correctness no longer depends on having a deprecated
  tool available to compare against, and CI no longer holds git back to an old
  release to keep one installable.
- **A native conversion engine that does not need `git-svn` or Perl.** It reads an
  `svnadmin dump` and writes a `git fast-import` stream, converting roughly an order
  of magnitude faster than git-svn's one-round-trip-per-revision design. It is now
  the default wherever a local repository is available (`convert.engine: auto`);
  `git-svn` remains selectable and is still used for converting directly from a
  remote URL. The engines are interchangeable — the suite asserts every branch and
  tag has an identical Git tree object under both.
- Push coverage: a GitLab-shaped REST API in front of `git http-backend` lets the
  tests run a real `git push` over the smart HTTP protocol and assert on what
  arrived — refs, a clean clone, the default branch, and protection ordering.

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
2016 and later, which detects missing Git and Subversion and says where to get them.

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

[Unreleased]: https://github.com/Virtual-Tech-Box/svn-to-gitlab/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/Virtual-Tech-Box/svn-to-gitlab/releases/tag/v1.0.1
[1.0.0]: https://github.com/Virtual-Tech-Box/svn-to-gitlab/releases
