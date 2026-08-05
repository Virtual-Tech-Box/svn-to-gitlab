# Migration runbook

The operational procedure for migrating a Subversion repository to GitLab with
`svn2gitlab`. Written for the engineer running the migration. The reference case
throughout is VisualSVN Server on Windows Server 2019 migrating to GitLab SaaS,
because it is the most common; everything applies equally to other deployments.

"The client" below means whoever owns the repository — an external customer, or your
own team. Substitute accordingly.

The shape of a migration that goes well:

```text
  Week -2   Discovery      analyze, review authors, agree the plan
  Week -1   Rehearsal      full migration to a scratch project, verified
  Week -1   Go live        real migration, then sync on a schedule
  Day  0    Cutover        freeze, final sync, verify, hand over
  Week +1   Aftercare      keep the SVN server read-only, then archive
```

---

## Phase 0 — Prerequisites

**Where to run it.** On the Subversion server if you possibly can. It removes the
network from the inner conversion loop (typically hours instead of days) and it is
the only way to apply the cutover lock. If you cannot, run it on a machine with fast
network access to both SVN and GitLab.

**Accounts.**

- An SVN account with **read access to the entire repository**. A partially readable
  repository cannot be migrated faithfully, and the failure is silent — paths simply
  do not appear.
- A GitLab token with the `api` and `write_repository` scopes, held by a user with at
  least Maintainer on the target namespace. A group access token is usually the
  cleanest choice.

**Disk.** Budget **4x** the Subversion repository size on the work volume: the
git-svn mirror, the derived export, and a temporary export during verification.
`svn2gitlab migrate` refuses to start below half that and warns below the full
estimate.

**Checks.**

```bash
svn2gitlab doctor
```

Everything under `git`, `git-svn` and `svn` must be green. On a VisualSVN host, `svn`
is usually present but not on `PATH`:

```yaml
tool_dirs:
  - C:\Program Files\VisualSVN Server\bin
```

---

## Phase 1 — Discovery

```bash
svn2gitlab init --output migration.yaml --svn-url https://svn.acme.local/svn/core
svn2gitlab analyze -c migration.yaml
```

Read the findings, not just the summary. Specifically:

**Layout.** Confirm the detected trunk/branches/tags match reality. If the report says
`flat` for a repository you believe has branches, they live under a name the detector
did not recognise — set `source.layout.mode: custom` with explicit paths. Getting this
wrong produces a plausible-looking single-branch conversion.

**`svn:externals`.** Every one needs a decision before cutover: Git submodule, package
dependency, or vendored copy. There is no automatic translation.

**Large files.** If anything at HEAD is over 100 MiB, or the repository is over ~2 GiB,
turn on `convert.lfs.enabled` now. Discovering it at push time means re-running the
export stage.

**Multi-project repositories.** If the report says the repository holds several
projects, decide with the repository owners whether they become several GitLab projects (the
convention) or one. Use `repositories:` with `subpath:` for the split.

Share the analysis report with the repository owners. It is usually the first time
anyone has seen these numbers.

---

## Phase 2 — Authors

This is the step that most affects how the migration *feels* afterwards. Get it wrong
and 40,000 commits are attributed to nobody.

```bash
svn2gitlab authors -c migration.yaml --write
```

Open `authors.txt`. Entries the tool generated are listed in the header — those are
guesses from the username and need review. **The email address decides which GitLab
user a commit links to, and it must match an address on that person's GitLab account
(primary or secondary).**

```text
jsmith            = John Smith <john.smith@acme.com>
CONTOSO\bwilliams = Beth Williams <beth.williams@acme.com>
a.jones           = Alice Jones <alice.jones@acme.com>
(no author)       = svn <svn@acme.com>
```

Ask HR or IT for the leavers. An address that no longer exists is fine — the commit
just will not link to a profile — but a *wrong* address attributes someone's work to
a colleague, which is worse.

Set `authors.policy: strict` once the file is complete, so an unmapped author stops
the migration instead of being invented.

---

## Phase 3 — Rehearsal

Never let the production run be the first run.

```bash
svn2gitlab migrate -c migration.yaml --dry-run
```

This converts and verifies locally, pushing nothing. Then repeat against a scratch
GitLab project (`target.project: core-rehearsal`) so the push path is exercised too.

Check, in the report:

- verification says **identical** for every branch;
- branch and tag counts match the analysis;
- the retargeted-tags list looks sensible (a tag created as a pure directory copy
  points at the commit it copied, which is what people expect);
- the renamed-refs list — names sanitised for Git — is acceptable to the team;
- skipped refs are genuinely branches deleted in SVN, not something you wanted.

Time the run. A rehearsal is also your estimate for the real one.

Delete the rehearsal project before going live.

---

## Phase 4 — Go live

```bash
svn2gitlab migrate -c migration.yaml
```

Safe to interrupt with Ctrl+C; re-running resumes at the first incomplete stage. If
the machine reboots, run the same command again.

Watch progress from another terminal:

```bash
svn2gitlab status -c migration.yaml
```

Then keep GitLab in step while people carry on working in Subversion:

```bash
svn2gitlab sync -c migration.yaml --once      # one round, e.g. from a scheduled task
svn2gitlab sync -c migration.yaml --watch     # or a supervised foreground loop
```

For unattended operation prefer a scheduled task over `--watch` — it survives
reboots:

```powershell
schtasks /Create /TN svn2gitlab-sync-core `
  /TR "\"C:\Program Files\svn2gitlab\svn2gitlab.exe\" sync --config C:\ProgramData\svn2gitlab\migration.yaml --repo core --once" `
  /SC MINUTE /MO 60 /RL HIGHEST /F
```

> **Do not let anyone commit to the GitLab project during this phase.** Re-derived
> history can require a force push, which would discard their work. The project is a
> mirror until cutover. Announce this clearly, and keep the default branch protected.

---

## Phase 5 — Cutover

Pick a quiet window. Announce it at least a day ahead.

**Before:**

1. Confirm the last sync succeeded — `svn2gitlab status -c migration.yaml`.
2. Confirm no one is mid-commit: `svn status` on the main working copies.
3. Confirm CI, release scripts and IDE configurations are ready to point at GitLab.
4. Take a Subversion backup: `svnadmin hotcopy <repo> <backup>`. Independent of this
   tool, and non-negotiable.

**Run:**

```bash
svn2gitlab cutover -c migration.yaml
```

In order: Subversion is made read-only *first*, then the final sync runs, then
verification. That order is deliberate — locking after the final sync leaves a window
in which a commit is accepted and then lost.

**After:**

1. Read the verification section of the report. It must say identical.
2. Set branch protections, approval rules and CI variables on the GitLab project.
3. Send the team the clone URL and the report.
4. Keep the Subversion server running and read-only for at least a month. People will
   need it, and a read-only server answers the question "was this really migrated
   correctly?" better than any report.

If something is wrong and you need to reverse the freeze:

```bash
svn2gitlab unlock -c migration.yaml
```

That restores Subversion write access, including any pre-commit hook the lock
displaced. It does not touch GitLab.

---

## Phase 6 — Aftercare

- Week 1: keep the sync task **disabled** but the tool installed, in case you need to
  re-derive.
- Week 4: archive the Subversion repository (`svnadmin dump` to cold storage) and
  decommission the server.
- Keep `manifest-latest.json` with the project records. It carries a SHA-256 checksum
  and is the artefact that answers audit questions years later.

---

## Failure playbook

**The conversion stops partway through.**
Re-run the same command. It resumes. If it stops at the same revision every time,
that revision contains something git-svn cannot represent — exclude the path with
`source.layout.ignore_paths` and re-run with `--restart-from convert`.

**Verification reports differences.**
Open the HTML report and read the classification.
*eol-normalised* entries are not defects — `svn export` applied `svn:eol-style`
translation. *missing-in-git* on a path you excluded is expected. Anything else is
real: stop, and do not cut over.

**The push is rejected.**
See the troubleshooting table in the README. The three common causes are GitLab push
rules rejecting imported history, a file over the size limit (enable LFS), and a
non-fast-forward after a history rewrite (`--force-push`, safe pre-cutover only).

**Someone committed to GitLab before cutover.**
Their work is on a branch that the next re-derivation will not contain. Before
syncing again: fetch their branch, save it, complete the cutover, then reapply it on
top. Do not force-push over it.

**You need to start completely over.**
```bash
svn2gitlab migrate -c migration.yaml --no-resume
```
and delete the GitLab project first. The work directory can be removed entirely; it
holds no state that is not reproducible from Subversion.
