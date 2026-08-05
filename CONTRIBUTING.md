# Contributing

Thanks for considering a contribution. This tool moves people's source history from
one system to another, so the bar for correctness is high — but the codebase is
plain Python and the test suite builds real repositories, so it is straightforward
to work on.

## Getting set up

```bash
git clone https://github.com/Virtual-Tech-Box/svn-to-gitlab.git
cd svn-to-gitlab
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
svn2gitlab doctor
```

You need `git`, `git-svn` and `svn` for the end-to-end tests. Without them those
tests skip automatically and the unit tests still run.

| Platform | Install |
| --- | --- |
| Debian/Ubuntu | `sudo apt install git git-svn subversion git-lfs` |
| Fedora/RHEL | `sudo dnf install git git-svn subversion git-lfs` |
| macOS | `brew install git git-svn subversion git-lfs` |
| Windows | Git for Windows (**standard** installer, not MinGit) + SlikSVN or VisualSVN Server's `bin` |

## Running the tests

```bash
pytest                    # everything, including real conversions
pytest -m "not slow"      # unit tests only, no external tools needed
pytest tests/test_e2e.py  # the end-to-end conversions
```

The end-to-end fixture builds a Subversion repository containing the cases that
break naive converters: a tag created as a pure directory copy, a tag modified
*after* creation, a branch deleted before HEAD, a binary file, a non-ASCII path,
`svn:ignore` properties, a domain-qualified author (`CONTOSO\user`), and a revision
with no author at all. If you fix a conversion bug, add the case to that fixture.

## What good looks like here

**Verification is not optional.** Any change to the conversion or export path must
keep `pytest tests/test_e2e.py` green, and that includes
`test_verification_detects_a_real_difference` — a verification that cannot fail
proves nothing.

**Errors carry a remedy.** Every exception in `errors.py` takes a `remedy` string.
"Push failed with 403" is not an acceptable error message; "the token needs the
`write_repository` scope and at least Developer access" is. If you add a failure
mode, add the fix alongside it.

**Never log a secret.** Credentials reach us through config, argv and remote URLs.
Everything goes through `logging_setup.REDACTOR`. If you add a path where a token
or password could be printed, add a test to `test_web_and_gitlab.py` proving it is
scrubbed.

**Determinism matters.** The export is re-derived on every sync, and identical input
must produce identical commit SHAs — otherwise every sync becomes a force push. That
means no wall-clock timestamps, no random values, and pinned identities on synthetic
commits. `test_stream_filter_is_deterministic` guards the message filter.

**No silent narrowing.** If the tool skips a branch, drops a ref, or cannot do
something it was asked to do, it must say so in the report and the logs. Look at how
`ExportResult.skipped` is populated.

## Style

Match the surrounding code rather than any external style guide. Concretely: type
hints on public functions, dataclasses for structured results, `snake_case`, and
comments that explain *why* rather than restating the code. Line length 100.

There is no enforced formatter. Do not reformat files you are not otherwise
changing — it buries the real diff.

## Pull requests

1. Branch from `main`.
2. Keep the change focused; unrelated fixes belong in separate PRs.
3. Add or update tests. A bug fix without a regression test will be asked for one.
4. Run `pytest` before pushing.
5. Describe *what breaks without this change* in the PR body, not just what you did.

CI runs the suite on Linux, macOS and Windows. It must be green before review.

## Reporting bugs

A migration bug report is only actionable with the shape of the source repository.
Please include:

- the output of `svn2gitlab doctor`;
- the relevant part of your config, **with secrets removed**;
- the `layout` section of `svn2gitlab analyze` output;
- what you expected in Git versus what you got.

Logs live under `<workdir>/logs/` and are already redacted, but read them before
attaching.

## Security

Do not open a public issue for a security problem. See [SECURITY.md](SECURITY.md).

## Licence

By contributing you agree that your contribution is licensed under the Apache
License 2.0, the same licence as this project.
