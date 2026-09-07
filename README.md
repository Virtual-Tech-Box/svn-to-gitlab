# svn2gitlab

[![CI](https://github.com/Virtual-Tech-Box/svn-to-gitlab/actions/workflows/ci.yml/badge.svg)](https://github.com/Virtual-Tech-Box/svn-to-gitlab/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)](#install)

An installable tool for migrating Subversion repositories to GitLab — any SVN
deployment to any GitLab deployment.

It converts history with full fidelity, keeps GitLab in step with a still-live SVN
server until you are ready to switch, and then **proves** the result: every file at
the migrated revision is compared byte-for-byte against Subversion and the outcome
is written into a checksummed report you can hand to stakeholders for sign-off.

```text
preflight → analyze → authors → acquire → convert → export → push → verify
```

Every stage is resumable. A conversion interrupted at hour nine restarts at hour
nine, not hour zero.

---

## What it handles

| | |
| --- | --- |
| **Sources** | Subversion, and Team Foundation Version Control (TFS 2015+ / Azure DevOps Server / Azure DevOps Services). |
| **SVN sources** | VisualSVN Server, Apache `mod_dav_svn`, `svnserve`, `svn+ssh`, a repository on local disk, or a hosted provider. Any of `http`, `https`, `svn`, `svn+ssh`, `file`. |
| **SVN layouts** | Standard `trunk`/`branches`/`tags`, non-standard directory names, nested branches (`branches/team/feature`), flat repositories with no layout at all, and one-repository-many-projects. Detected automatically; overridable. |
| **GitLab targets** | gitlab.com SaaS and any self-managed instance, including behind a private CA. Personal, group, project or OAuth tokens. |
| **Scale** | Resumable at every stage. A conversion interrupted at hour nine restarts at hour nine, not hour zero. |
| **Large binaries** | Optional Git LFS rewriting, planned from the *whole history* rather than from HEAD, because the file that breaks the push is usually one deleted years ago. |
| **Live cutover** | Incremental sync until cutover, then a final sync, an SVN read-only lock, and verification — in that order, so no commit can slip through the gap. |

---

## Install

### Windows

Download `svn2gitlab-setup-<version>.exe` from
[Releases](https://github.com/Virtual-Tech-Box/svn-to-gitlab/releases) and run it. It
installs to `Program Files`, adds the tool to `PATH`, creates a working area under
`C:\ProgramData\svn2gitlab`, and checks for the external tools described below.

A portable zip is published alongside it if you would rather not run an installer.
The binaries are **not code-signed**, so SmartScreen will warn on first run.

### From source (any platform)

```bash
git clone https://github.com/Virtual-Tech-Box/svn-to-gitlab.git
cd svn-to-gitlab
pip install .
svn2gitlab doctor
```

### External tools

The tool drives standard Git and Subversion binaries rather than reimplementing
them. `svn2gitlab doctor` reports exactly what is present and what to do about
anything that is not.

| Tool | Needed for | Notes |
| --- | --- | --- |
| `git` | Everything | Any reasonably recent build. |
| `svn`, `svnadmin` | Everything | On a VisualSVN Server host these already exist in `C:\Program Files\VisualSVN Server\bin` — either add it to `PATH` or list the folder under `tool_dirs:`. |
| `svnsync` / `svnrdump` | Mirroring a *remote* repository locally | Either will do; `svnsync` is preferred. |
| `git-lfs` | Only if `convert.lfs.enabled` | |

**`git-svn` is not required and not used.** Conversion is done by this project's own
engine. That is deliberate: Git for Windows removed `git svn` in v2.54.0, citing
persistent maintenance challenges, so on a current Windows install it is absent
whichever installer you choose.

### Migrating from TFS (TFVC)

Set `source.kind: tfvc` and point at the collection:

```yaml
source:
  kind: tfvc
  url: https://tfs.acme.local/tfs
  collection: DefaultCollection
  project: Payments
  token: env:TFS_TOKEN        # personal access token, Code (read) scope
```

History is read entirely over the REST API, so this runs from any machine that can
reach the TFS application tier — no Visual Studio, no `tf.exe`, nothing Windows-only.
Changesets become commits, TFVC branches become Git branches, and everything after
the conversion — export, LFS, GitLab push, verification, incremental sync — is the
same code path as Subversion.

Verification works the same way too: each file is fetched from TFVC at the migrated
changeset and compared by Git blob hash, with the item's MD5 checked first so a
truncated download is caught rather than reported as a content difference.

What differs from Subversion, and is reported in the analysis:

- **No tags.** TFVC has no tag concept; labels are the nearest equivalent and are
  not migrated. Recreate the important ones as Git tags afterwards.
- **Merges are not reconstructed.** A merge changeset becomes an ordinary commit
  holding the merged content. File content is correct; branch topology is
  simplified. This matches how `svn:mergeinfo` is handled.
- **No ignore translation.** TFVC has no ignore property. `.tfignore` files are
  ordinary versioned files and migrate as content.
- **Branch detection matters.** If a project branched by copying a folder without
  converting it to a TFVC branch, there are no branch definitions to read and roots
  are inferred from path conventions. The analysis says which happened — check it,
  and set `source.branch_roots` explicitly if it guessed wrong.
- **The cutover lock is Subversion-only.** For TFVC, deny "Check in" on the team
  project in TFS at cutover.

See [`examples/tfs-tfvc-to-gitlab.yaml`](examples/tfs-tfvc-to-gitlab.yaml).

### How conversion works

The engine reads an `svnadmin dump` and writes a `git fast-import` stream. Nothing
is shelled out to a third-party converter, there is no Perl anywhere in the path,
and it runs roughly an order of magnitude faster than the round-trip-per-revision
approach `git svn` used.

Its output was originally validated against `git svn` and is now pinned permanently:
[`tests/test_golden_master.py`](tests/test_golden_master.py) records the exact Git
tree objects the reference fixture must produce. A tree hashes paths, contents and
file modes, so if a conversion ever writes something different, the suite says so —
without needing a deprecated tool installed to compare against.

---

## Five-minute start

```bash
# 1. Check the machine
svn2gitlab doctor

# 2. Write a configuration
svn2gitlab init --output migration.yaml \
  --svn-url https://svn.acme.local/svn/core \
  --namespace acme/legacy --project core

# 3. Provide a GitLab token (scopes: api, write_repository)
set GITLAB_TOKEN=glpat-xxxxxxxxxxxx        # PowerShell: $env:GITLAB_TOKEN='glpat-...'

# 4. Look before you leap — read-only, changes nothing
svn2gitlab analyze -c migration.yaml

# 5. Review who wrote the history, and fix the addresses
svn2gitlab authors -c migration.yaml --write
notepad authors.txt

# 6. Rehearse locally: converts and verifies, pushes nothing
svn2gitlab migrate -c migration.yaml --dry-run

# 7. Migrate for real
svn2gitlab migrate -c migration.yaml
```

Or drive the whole thing from a browser:

```bash
svn2gitlab serve -c migration.yaml
```

---

## Commands

| Command | Purpose |
| --- | --- |
| `doctor` | Check external tools; explain how to install anything missing. |
| `init` | Write a commented starter configuration. |
| `analyze` | Inspect the source and report layout, authors, size, and risks. Read-only. |
| `authors` | Generate and review the SVN→Git author map. |
| `migrate` | Run the migration. Resumable; safe to interrupt. |
| `sync` | Bring GitLab up to date with new SVN commits. |
| `cutover` | Final sync, make SVN read-only, verify, sign off. |
| `verify` | Re-run verification against the current state. |
| `status` | Job history, stage progress, sync state. |
| `unlock` | Reverse a cutover lock. |
| `config` | Validate and print the fully resolved configuration. |
| `serve` | Local web dashboard. |

---

## Configuration

One YAML file describes a whole migration programme. Every section can be set once
and overridden per repository.

```yaml
version: 1
name: acme-migration
workdir: C:\ProgramData\svn2gitlab\work     # needs ~4x the SVN repository size

source:
  url: https://svn.acme.local/svn/core      # or local_path: for the fast path
  username: ACME\svcmigration
  password: env:SVN_PASSWORD                # env: / file: / keyring:
  trust_server_cert: true                   # VisualSVN self-signed certificates
  layout:
    mode: auto                              # auto | standard | custom | flat | multi
  fast_path: auto                           # auto | hotcopy | rdump | none

authors:
  file: authors.txt
  default_domain: acme.com
  policy: generate                          # or `strict` to refuse unknown authors

convert:
  default_branch: main
  strip_svn_metadata: true                  # drop git-svn-id from published messages
  keep_revision_trailer: true               # keep `Svn-Revision: NNN` for traceability
  tag_style: annotated
  generate_gitignore: true                  # translate svn:ignore
  lfs:
    enabled: true
    size_threshold_mb: 50
    extensions: [zip, exe, dll, msi, pdf, psd, mp4, iso]

target:
  gitlab_url: https://gitlab.com            # or your self-managed instance
  token: env:GITLAB_TOKEN
  namespace: acme/legacy                    # nested groups are created as needed
  project: core
  visibility: private
  protect_default_branch: true

verify:
  mode: full                                # full | tree | counts | off
  verify_all_branches: true
  fail_on_mismatch: true

sync:
  enabled: true
  interval_minutes: 60
  lock_svn_on_cutover: true
```

### Many repositories at once

```yaml
source: { url: https://svn.acme.local/svn/root, username: ACME\svc }
target: { gitlab_url: https://gitlab.com, token: env:GITLAB_TOKEN, namespace: acme }

repositories:
  - name: core
    source: { subpath: core }
    target: { project: core }
  - name: tools
    source: { subpath: tools }
    target: { project: tools }
    convert: { lfs: { enabled: true } }     # overrides merge key-wise
```

### Secrets

No password or token needs to be written into the file:

```yaml
password: env:SVN_PASSWORD
token:    file:C:\secure\gitlab-token.txt
token:    keyring:gitlab/migration
```

Secrets are registered with a redactor at load time, so they cannot appear in a log
file, a report, the web UI, or a stored git remote.

---

## How it works

```text
preflight → analyze → authors → acquire → convert → export → push → verify
```

Each stage records its outcome, so a re-run resumes at the first stage that has not
succeeded.

**Two repositories, not one.** The *mirror* holds the converted history under
`refs/remotes/svn/*` with a `git-svn-id:` trailer per commit — that metadata is
what makes `git svn fetch` resumable and incremental. The *export* is derived from
it, and is what GitLab receives: real branches, real tags, clean messages, LFS
pointers. Publishing requires rewriting exactly the things the mirror needs intact,
so keeping them separate means a rewrite never damages the next sync.

Every step of the derivation is deterministic — fixed tagger dates, pinned identities
on synthetic commits, a byte-stable message filter — so re-deriving after a sync
produces the same commit SHAs and the push stays a fast-forward.

**Getting the history local first.** `git svn` against a remote server makes one
round trip per revision; over a WAN that is days for a large repository. When the
tool runs on the SVN server (`source.local_path`) it reads via `file://` with no copy
at all. For a remote repository it can mirror with `svnsync` first — resumable, so a
dropped VPN costs minutes rather than a restart.

---

## Verification

The claim at sign-off is specific: *every file in Subversion at revision N is
byte-for-byte identical to the file in GitLab at commit X, on every migrated branch.*

Git names a blob `sha1("blob " + length + "\0" + content)`. So the tool lists the Git
tree, exports the same revision from Subversion, and computes the **Git** blob hash of
each exported file locally. Matching hashes prove identical content — with no
checkout, no archive, and no second copy of the repository.

Files moved into Git LFS are covered too, without downloading anything: an LFS
pointer's OID *is* the SHA-256 of the original file.

Differences are classified rather than lumped together — missing on either side,
content, symlink target, and *eol-normalised* (where `svn export` applied
`svn:eol-style` translation), so a line-ending artefact is never confused with data
loss.

Each run writes `report-latest.html` (for people) and `manifest-latest.json` (for
audit, with a SHA-256 checksum) into the repository's `reports/` folder.

---

## Cutover

```bash
svn2gitlab sync -c migration.yaml --watch      # keep GitLab fresh
svn2gitlab cutover -c migration.yaml           # when everyone is ready
```

Cutover locks Subversion **before** the final sync, not after — otherwise a commit
can land in the gap between the last fetch and the lock, and it would be lost
silently. The lock is a `pre-commit` hook that rejects writes with your own message;
any hook it displaces is backed up and restored by `svn2gitlab unlock`.

Locking needs filesystem access to the repository. Running against a remote URL, the
tool says plainly that it could not lock rather than reporting a complete cutover.

---

## What does not migrate

Stated up front, because these surprise people:

- **`svn:externals`** — Git has no equivalent. Every one is listed in the analysis
  report so you can plan submodules, package dependencies, or vendoring.
- **Per-file locks and `svn:needs-lock`** — no Git equivalent.
- **Empty directories** — Git cannot track them. Set
  `convert.preserve_empty_dirs: true` to place `.gitkeep` markers.
- **Revision properties other than author, date and log message.**
- **Mergeinfo** — `svn:mergeinfo` is not reconstructed as Git merge commits.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `SVN author 'x' is not present in the author map` | A new committer appeared. `migrate` needs `svn2gitlab authors --write`; `sync` now picks new authors up automatically. The fetch resumes where it stopped. |
| Push rejected, `pre-receive hook declined` | GitLab push rules reject imported history (commit-message regex, author-email restriction, max file size). Disable them for the migration, then re-enable. |
| Push rejected, file too large | Enable `convert.lfs.enabled` and re-run with `--restart-from export`. |
| Push rejected, non-fast-forward | Re-derived history after a rewrite. Pre-cutover this is expected: re-run with `--force-push`. |
| Conversion is extremely slow | You are converting over the network. Run on the SVN server with `source.local_path`, or leave `fast_path: auto` to mirror locally first. |
| TLS failure against a self-managed GitLab | Set `target.ca_bundle` to your CA certificate. |
| Verification reports eol-normalised matches | Not a defect: `svn export` applied `svn:eol-style` translation. Recorded separately from real differences. |

Full logs are under `<workdir>/logs/`, with secrets redacted.

---

## Building the installer

```powershell
.\installer\build.ps1                 # Windows: PyInstaller + Inno Setup
.\installer\build.ps1 -Vendor C:\offline-tools   # embed Git/SVN for offline sites
```

```bash
./installer/build.sh                  # Linux/macOS: self-contained directory
```

Both run the test suite first and refuse to package a failing tree.

---

## Development

```bash
pip install -e ".[dev]"
pytest                    # unit tests plus end-to-end conversions
pytest -m "not slow"      # unit tests only
```

The end-to-end tests build a real Subversion repository — branches, tags, a
modified-after-creation tag, a deleted branch, a binary file, a non-ASCII path,
multiple authors including a domain-qualified one and an authorless revision — then
convert it and assert on the resulting Git objects.

The push is covered too: `tests/fake_gitlab.py` runs a GitLab-shaped REST API in
front of `git http-backend`, so `git push` speaks the real smart HTTP protocol to a
real bare repository. Those tests assert on what arrived on the server — the refs,
a clean clone of the content, the default branch, and that protection is applied
*after* the push rather than before. Not covered there: Git LFS uploads (which need
an LFS batch API) and GitLab push rules, which remain rehearsal-only.

All of these skip automatically where `svn` or `git-http-backend` are unavailable.

Contributions are welcome. [`CONTRIBUTING.md`](CONTRIBUTING.md) covers the setup and
the four rules that matter here: verification must be able to fail, errors must carry
a remedy, secrets must never reach a log, and the conversion must stay deterministic.

---

## Documentation

- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — the operational procedure, from discovery to
  cutover to aftercare, with a failure playbook
- [`examples/`](examples/) — two worked configurations: VisualSVN to GitLab SaaS, and
  a batch migration of several projects to a self-managed instance
- [`CHANGELOG.md`](CHANGELOG.md) — release history and known limitations
- [`SECURITY.md`](SECURITY.md) — threat model and how to report a vulnerability

---

## Licence

[Apache License 2.0](LICENSE) — see [`NOTICE`](NOTICE) for third-party attributions.

This project does not bundle Git or Subversion; it invokes whatever copies are
installed on the host, and those are covered by their own licences.
