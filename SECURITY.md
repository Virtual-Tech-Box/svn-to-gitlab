# Security policy

## Reporting a vulnerability

Please do not open a public issue.

Report privately through GitHub's
[private vulnerability reporting](https://github.com/Virtual-Tech-Box/svn-to-gitlab/security/advisories/new)
on this repository. We aim to acknowledge within 5 working days and to agree a
disclosure timeline with you.

Useful details: what an attacker can achieve, the version and platform, and a
minimal reproduction. If a credential was exposed, say so first — rotation is more
urgent than a full write-up.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.x | Yes |

## What this tool handles that is worth attacking

Anyone assessing this project should know where the sensitive material is.

**Credentials.** A migration needs a Subversion password and a GitLab token with
`api` and `write_repository` scope. They can come from the config file or, better,
from `env:`, `file:` or `keyring:` indirection. In memory they are held as plain
strings.

**Credential leakage is the primary risk.** Tokens end up in `git remote` URLs,
`svn --password` arguments, and HTTP headers. Everything logged passes through the
redactor in `logging_setup.py`, which scrubs both registered literal secrets and
structural patterns (`scheme://user:secret@host`, `--password=…`, `PRIVATE-TOKEN:`,
`glpat-…`). The push remote is removed from the repository config after use. A path
that defeats this redaction is a vulnerability — please report it.

**Subprocess execution.** The tool drives `git`, `svn` and their siblings. All
invocations pass an argument list, never a shell string, so repository content
cannot inject a command. Tool paths are resolved from `PATH` plus the known install
locations in `tools.py`; on a machine where an attacker controls those directories,
they control what runs.

**The web dashboard has no authentication.** `svn2gitlab serve` binds to
`127.0.0.1` by default. It reads a config that may contain credentials and can start
operations that write to Subversion and GitLab. Binding it to a non-loopback address
exposes all of that to anyone who can reach the port; the CLI warns when you do.
Treat that as a deployment decision, not a defect — but a way to reach it *without*
changing the bind address would be a vulnerability.

**Hook installation.** The cutover installs a `pre-commit` hook in the target
Subversion repository, which requires filesystem write access to that repository.
Any existing hook is backed up and restored by `svn2gitlab unlock`.

**Work directories.** These hold a full copy of the migrated source history and the
SQLite state database. The Windows installer restricts `C:\ProgramData\svn2gitlab`
to local Administrators. On other platforms the directory inherits your umask —
place it accordingly.

## Out of scope

- An operator who already has administrative rights on the host.
- Vulnerabilities in Git, Subversion or GitLab themselves — report those upstream.
- Committing your own credentials to a config file in version control. The shipped
  `.gitignore` excludes `migration.yaml` and `authors.txt` for this reason.
