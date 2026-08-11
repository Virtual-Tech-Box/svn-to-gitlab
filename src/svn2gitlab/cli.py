"""Command line interface.

    svn2gitlab doctor                 check the machine has what it needs
    svn2gitlab init                   write a starter configuration
    svn2gitlab analyze                inspect a repository without changing anything
    svn2gitlab authors                generate or review the author map
    svn2gitlab migrate                run the migration (resumable)
    svn2gitlab sync                   keep GitLab in step with a live SVN repository
    svn2gitlab cutover                final sync, lock SVN, verify, sign off
    svn2gitlab verify                 re-run verification against the current state
    svn2gitlab status                 what has run, and where it got to
    svn2gitlab unlock                 undo a cutover lock
    svn2gitlab serve                  local web dashboard
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (BarColumn, Progress, SpinnerColumn, TaskProgressColumn,
                           TextColumn, TimeElapsedColumn)
from rich.table import Table

from . import __version__
from .config import MigrationConfig, dump_config, load_config
from .errors import Svn2GitlabError
from .logging_setup import get_logger, setup_logging
from .pipeline import STAGES, MigrationPipeline, build_pipelines
from .state import StateStore
from .tools import ToolSet, detect_tools, human_bytes

console = Console(stderr=False)
err_console = Console(stderr=True)
log = get_logger("cli")

app = typer.Typer(
    name="svn2gitlab",
    help="Migrate any Subversion deployment to any GitLab deployment.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
)

CANCEL = threading.Event()


def _install_signal_handlers() -> None:
    def handler(_signum, _frame):
        if CANCEL.is_set():
            err_console.print("[bold red]Forced exit.[/bold red]")
            os._exit(130)
        CANCEL.set()
        err_console.print(
            "\n[yellow]Stopping after the current operation. "
            "Progress is saved - re-run the same command to resume. "
            "Press Ctrl+C again to force quit.[/yellow]")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # not on the main thread, or unsupported on this platform


def _state_path(config: Optional[MigrationConfig], override: Optional[Path] = None) -> Path:
    if override:
        return Path(override)
    if config:
        return config.workdir_path() / "svn2gitlab.db"
    return Path.cwd() / "svn2gitlab.db"


def _bootstrap(config_path: Optional[Path], verbose: bool, quiet: bool,
               log_file: Optional[Path] = None) -> tuple:
    config = load_config(config_path) if config_path else None
    workdir = config.workdir_path() if config else Path.cwd()
    workdir.mkdir(parents=True, exist_ok=True)
    setup_logging(log_file or (workdir / "logs" / "svn2gitlab.log"), verbose=verbose, quiet=quiet)
    state = StateStore(_state_path(config))
    return config, state


def _fail(exc: Exception, verbose: bool = False) -> None:
    if isinstance(exc, Svn2GitlabError):
        err_console.print(Panel(str(exc), title="[bold red]Migration error[/bold red]",
                                border_style="red", expand=False))
    else:
        err_console.print(Panel(f"{type(exc).__name__}: {exc}",
                                title="[bold red]Unexpected error[/bold red]",
                                border_style="red", expand=False))
    if verbose:
        console.print_exception()
    raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #

@app.command()
def doctor(
    tool_dir: List[str] = typer.Option([], "--tool-dir", help="Extra directory to search for executables."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Check that every external tool this migration needs is present and usable."""
    setup_logging(None, console=not json_output)
    tools = detect_tools(tool_dir, refresh=True)

    if json_output:
        console.print_json(json.dumps(tools.to_dict()))
        raise typer.Exit(code=0 if _core_ready(tools) else 1)

    table = Table(title="External tools", show_lines=False)
    table.add_column("Tool")
    table.add_column("Status")
    table.add_column("Needed for", overflow="fold")
    table.add_column("Version / detail", overflow="fold")
    table.add_column("Path", overflow="fold", style="dim")

    for info in tools.all():
        purpose, blocking = _tool_role(info.name, tools)
        if info.available:
            status = "[green]ok[/green]"
        elif blocking:
            status = "[red]missing[/red]"
        else:
            # Absent but not a problem. Styling this like a failure is how a perfectly
            # capable machine ends up looking broken.
            status = "[dim]not installed[/dim]"
        detail = info.version if info.available else (info.detail if blocking else "")
        table.add_row(info.name, status, purpose, detail or "", info.path or "")
    console.print(table)

    if _core_ready(tools):
        console.print("\n[green]This machine can run a migration.[/green]")

        notes = []
        if not tools.svnadmin.available:
            notes.append("svnadmin is not installed, so the native engine, the local fast "
                         "path and the cutover lock are unavailable.")
        if not tools.git_lfs.available:
            notes.append("Git LFS is not installed. Install it before migrating a "
                         "repository with large binaries (`convert.lfs.enabled`).")
        for note in notes:
            console.print(f"[dim]- {note}[/dim]")
        raise typer.Exit(code=0)

    console.print("\n[red]Required tools are missing.[/red]")
    from .tools import _install_hint
    missing = [i.name for i in tools.all()
               if not i.available and _tool_role(i.name, tools)[1]]
    console.print(_install_hint(missing))
    raise typer.Exit(code=1)


def _tool_role(name: str, tools: ToolSet) -> tuple:
    """(what the tool is for, whether its absence actually blocks a migration).

    Only the second value drives the red "missing" styling. Reporting an optional
    tool as missing makes a working machine look broken, which is exactly the wrong
    signal from a command whose entire job is to tell you where you stand.
    """
    roles = {
        "git": ("everything", True),
        "svn": ("everything", True),
        "svnadmin": ("conversion, local fast path, cutover lock", True),
        "git-lfs": ("only with convert.lfs.enabled", False),
        "svnrdump": ("mirroring a remote repository locally", False),
        "svnsync": ("mirroring a remote repository locally", False),
        "svnlook": ("inspecting a local repository", False),
    }
    return roles.get(name, ("", False))


def _core_ready(tools: ToolSet) -> bool:
    """Everything a migration needs: git, an svn client, and svnadmin for the dump."""
    return (tools.git.available and tools.svn.available
            and tools.svnadmin.available)


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #

@app.command()
def init(
    output: Path = typer.Option(Path("migration.yaml"), "--output", "-o",
                                help="Where to write the configuration."),
    svn_url: Optional[str] = typer.Option(None, "--svn-url", help="Subversion repository URL."),
    svn_path: Optional[str] = typer.Option(None, "--svn-path",
                                           help="Local repository path (enables the fast path)."),
    gitlab_url: str = typer.Option("https://gitlab.com", "--gitlab-url"),
    namespace: Optional[str] = typer.Option(None, "--namespace", help="GitLab group path."),
    project: Optional[str] = typer.Option(None, "--project", help="GitLab project path."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
) -> None:
    """Write a commented starter configuration."""
    if output.exists() and not force:
        err_console.print(f"[red]{output} already exists.[/red] Use --force to overwrite.")
        raise typer.Exit(code=1)
    content = _starter_config(svn_url, svn_path, gitlab_url, namespace, project)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    console.print(f"[green]Wrote {output}[/green]")
    console.print("\nNext steps:")
    console.print("  1. Set your GitLab token:  [cyan]set GITLAB_TOKEN=glpat-...[/cyan]")
    console.print(f"  2. Review the file:       [cyan]{output}[/cyan]")
    console.print(f"  3. Inspect the source:    [cyan]svn2gitlab analyze -c {output}[/cyan]")
    console.print(f"  4. Migrate:               [cyan]svn2gitlab migrate -c {output}[/cyan]")


def _starter_config(svn_url, svn_path, gitlab_url, namespace, project) -> str:
    # Every interpolated value is computed here rather than inline: an f-string
    # expression could not contain a backslash before Python 3.12, and the Windows
    # path in the local_path example does.
    url_line = f"url: {svn_url}" if svn_url else "# url: https://svn.example.com/svn/myrepo"
    path_line = (f"local_path: {svn_path}" if svn_path
                 else r"# local_path: C:\Repositories\myrepo")
    namespace_line = f"namespace: {namespace}" if namespace else "# namespace: mygroup/subgroup"
    project_line = f"project: {project}" if project else "# project: myproject"
    name = project or "migration"

    return f"""# svn2gitlab migration configuration
# Every value below can be overridden per repository in the `repositories:` list.
version: 1
name: {name}

# Working directory. Needs roughly 4x the size of the Subversion repository:
# the mirror, the publishable export, and a temporary verification export.
workdir: ./svn2gitlab-work

source:
  # Use `url` for any remote repository, or `local_path` when this tool runs on the
  # Subversion server itself. `local_path` is dramatically faster and is required for
  # the cutover lock.
  {url_line}
  {path_line}
  # username: DOMAIN\\svcmigration
  # password: env:SVN_PASSWORD          # env: / file: / keyring: indirection supported
  trust_server_cert: true               # VisualSVN commonly uses a self-signed certificate

  layout:
    mode: auto                          # auto | standard | custom | flat | multi
    # trunk: trunk
    # branches: [branches]
    # tags: [tags]
    # ignore_paths: '^(build|vendor)/'  # regex, applied to repository-relative paths

  fast_path: auto                       # auto | hotcopy | rdump | none

authors:
  file: authors.txt
  default_domain: example.com
  policy: generate                      # generate | strict

convert:
  default_branch: main
  strip_svn_metadata: true              # remove git-svn-id from published messages
  keep_revision_trailer: true           # keep `Svn-Revision: NNN` for traceability
  tag_style: annotated                  # annotated | lightweight | branches
  generate_gitignore: true              # translate svn:ignore into a committed .gitignore

  lfs:
    enabled: false                      # turn on for repositories holding large binaries
    size_threshold_mb: 50
    extensions: [zip, exe, dll, msi, pdf, psd, mp4, iso]

target:
  gitlab_url: {gitlab_url}
  token: env:GITLAB_TOKEN               # needs the `api` and `write_repository` scopes
  {namespace_line}
  {project_line}
  visibility: private
  create_namespace: true
  protect_default_branch: true

verify:
  mode: full                            # full | tree | counts | off
  verify_all_branches: false
  fail_on_mismatch: true

sync:
  enabled: false                        # set true to keep GitLab fresh until cutover
  interval_minutes: 60
  lock_svn_on_cutover: true

# Migrate many repositories in one run. Each entry inherits everything above.
# repositories:
#   - name: core
#     source: {{ subpath: core }}
#     target: {{ project: core }}
#   - name: tools
#     source: {{ subpath: tools }}
#     target: {{ project: tools }}
"""


# --------------------------------------------------------------------------- #
# analyze
# --------------------------------------------------------------------------- #

@app.command()
def analyze(
    config_path: Path = typer.Option(..., "--config", "-c", exists=True),
    repo: List[str] = typer.Option([], "--repo", "-r", help="Limit to these repositories."),
    json_output: Optional[Path] = typer.Option(None, "--json", help="Write the analysis as JSON."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Inspect the Subversion source and report what a migration will involve.

    Read-only: nothing is created, converted or pushed.
    """
    _install_signal_handlers()
    try:
        config, state = _bootstrap(config_path, verbose, False)
        pipelines = build_pipelines(config, state, only=list(repo) or None, cancel=CANCEL,
                                    dry_run=True, create_jobs=False)
        results = {}
        for pipeline in pipelines:
            console.rule(f"[bold]{pipeline.repo.name}[/bold]")
            with _progress_bar() as (progress, task):
                pipeline._on_progress = lambda stage, f, m: progress.update(
                    task, completed=f * 100, description=f"{stage}: {m}")
                pipeline._current_stage = "analyze"
                analysis = pipeline._stage_analyze()
            results[pipeline.repo.name] = analysis
            _print_analysis(pipeline.repo.name, analysis)
        if json_output:
            json_output.parent.mkdir(parents=True, exist_ok=True)
            json_output.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
            console.print(f"\n[green]Wrote {json_output}[/green]")
    except Exception as exc:  # noqa: BLE001
        _fail(exc, verbose)


def _print_analysis(name: str, data: dict) -> None:
    layout = data.get("layout") or {}
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_row("HEAD revision", f"r{data.get('head_revision', 0):,}")
    table.add_row("Revisions", f"{data.get('revision_count', 0):,}")
    table.add_row("Authors", str(len(data.get("authors") or {})))
    table.add_row("Layout", f"{layout.get('mode')} ({layout.get('confidence')})")
    table.add_row("Trunk", str(layout.get("trunk") or "-"))
    table.add_row("Branches", f"{len(data.get('branch_names') or [])}")
    table.add_row("Tags", f"{len(data.get('tag_names') or [])}")
    table.add_row("Files at HEAD", f"{data.get('head_file_count', 0):,}")
    table.add_row("Size at HEAD", human_bytes(data.get("head_size_bytes", 0)))
    if data.get("first_commit"):
        table.add_row("History spans", f"{data['first_commit'][:10]} to "
                                       f"{(data.get('last_commit') or '')[:10]}")
    console.print(table)

    for rationale in layout.get("rationale") or []:
        console.print(f"  [dim]layout: {rationale}[/dim]")

    findings = data.get("findings") or []
    if findings:
        console.print()
        order = {"blocker": 0, "warning": 1, "info": 2}
        colour = {"blocker": "red", "warning": "yellow", "info": "cyan"}
        for finding in sorted(findings, key=lambda f: order.get(f.get("level"), 3)):
            level = finding.get("level", "info")
            console.print(f"[{colour.get(level, 'white')}]{level.upper():8}[/] "
                          f"{finding.get('message')}")
            if finding.get("remedy"):
                console.print(f"         [dim]{finding['remedy']}[/dim]")


# --------------------------------------------------------------------------- #
# authors
# --------------------------------------------------------------------------- #

@app.command()
def authors(
    config_path: Path = typer.Option(..., "--config", "-c", exists=True),
    repo: List[str] = typer.Option([], "--repo", "-r"),
    write: bool = typer.Option(False, "--write", help="Write the author map file."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Generate or review the SVN-to-Git author map.

    Getting these addresses right is what makes GitLab attribute imported commits to
    real user accounts, so review the generated file before migrating.
    """
    _install_signal_handlers()
    try:
        config, state = _bootstrap(config_path, verbose, False)
        pipelines = build_pipelines(config, state, only=list(repo) or None, cancel=CANCEL,
                                    dry_run=True, create_jobs=False)
        for pipeline in pipelines:
            console.rule(f"[bold]{pipeline.repo.name}[/bold]")
            pipeline._current_stage = "analyze"
            with _progress_bar() as (progress, task):
                pipeline._on_progress = lambda stage, f, m: progress.update(
                    task, completed=f * 100, description=f"{stage}: {m}")
                pipeline._stage_analyze()
                if write:
                    pipeline._current_stage = "authors"
                    result = pipeline._stage_authors()
                else:
                    result = None

            table = Table("SVN user", "Commits", "Mapped identity")
            from .svn.authors import AuthorMap, build_author_map
            existing = AuthorMap.load(pipeline.repo.authors_file)
            amap, synthesised = build_author_map(pipeline.analysis.authors.keys(),
                                                 pipeline.repo.authors, existing)
            generated = set(synthesised)
            for user, count in list(pipeline.analysis.authors.items())[:200]:
                # Authorless revisions are recorded under the literal key
                # "(no author)", which is how we store them too.
                identity = amap.get(user or "(no author)")
                label = str(identity) if identity else "[red]unmapped[/red]"
                if user in generated:
                    label = f"[yellow]{label}[/yellow] (generated)"
                table.add_row(user or "(no author)", f"{count:,}", label)
            console.print(table)

            if result:
                console.print(f"[green]Wrote {result['authors_file']}[/green]")
                if result.get("synthesised"):
                    console.print(f"[yellow]{len(result['synthesised'])} address(es) were "
                                  "generated from the username. Review them before "
                                  "migrating.[/yellow]")
            else:
                console.print("[dim]Run with --write to save this map.[/dim]")
    except Exception as exc:  # noqa: BLE001
        _fail(exc, verbose)


# --------------------------------------------------------------------------- #
# migrate
# --------------------------------------------------------------------------- #

@app.command()
def migrate(
    config_path: Path = typer.Option(..., "--config", "-c", exists=True),
    repo: List[str] = typer.Option([], "--repo", "-r"),
    stage: List[str] = typer.Option([], "--stage", help=f"Run only these stages ({', '.join(STAGES)})."),
    restart_from: str = typer.Option("", "--restart-from", help="Discard this stage and everything after it."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Convert locally but do not push."),
    force_push: bool = typer.Option(False, "--force-push", help="Allow a non-fast-forward push."),
    no_resume: bool = typer.Option(False, "--no-resume", help="Start a new job instead of resuming."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Run the migration. Interrupt safely at any point; re-run to resume."""
    _install_signal_handlers()
    exit_code = 0
    try:
        config, state = _bootstrap(config_path, verbose, quiet)
        pipelines = build_pipelines(
            config, state, only=list(repo) or None, cancel=CANCEL,
            dry_run=dry_run, resume=not no_resume,
        )
        if dry_run:
            console.print("[yellow]Dry run: converting locally, nothing will be pushed.[/yellow]\n")

        workers = min(config.parallelism, len(pipelines))
        run_kwargs = {"stages": list(stage) or None, "restart_from": restart_from,
                      "force_push": force_push}

        if workers > 1:
            outcomes = _run_parallel(pipelines, workers, run_kwargs)
        else:
            outcomes = _run_sequential(pipelines, run_kwargs)

        for outcome in outcomes:
            if not outcome.ok:
                exit_code = 1
        _print_summary(outcomes)
    except Exception as exc:  # noqa: BLE001
        _fail(exc, verbose)
    raise typer.Exit(code=exit_code)


def _run_sequential(pipelines, run_kwargs) -> list:
    outcomes = []
    for pipeline in pipelines:
        console.rule(f"[bold]{pipeline.repo.name}[/bold] -> "
                     f"{pipeline.repo.target.full_path()}")
        with _progress_bar() as (progress, task):
            pipeline._on_progress = lambda stage_name, f, m: progress.update(
                task, completed=f * 100, description=f"[cyan]{stage_name}[/cyan] {m}")
            outcome = pipeline.run(**run_kwargs)
        outcomes.append(outcome)
        _print_outcome(outcome)
        if CANCEL.is_set():
            break
    return outcomes


def _run_parallel(pipelines, workers: int, run_kwargs) -> list:
    """Migrate several repositories at once, one progress row each.

    The bottleneck in a batch migration is almost always the Subversion server or
    the disk, not this process - so `parallelism` is worth raising only when both
    have headroom.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    console.print(f"Migrating {len(pipelines)} repositories, {workers} at a time.\n")
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.fields[repo]}[/bold]"),
        BarColumn(bar_width=22),
        TaskProgressColumn(),
        TextColumn("{task.description}"),
        TimeElapsedColumn(),
        console=console,
    )
    outcomes: List = []
    with progress:
        tasks = {p.repo.name: progress.add_task("queued", total=100, repo=p.repo.name)
                 for p in pipelines}

        def make_reporter(name):
            def report(stage_name, fraction, message):
                progress.update(tasks[name], completed=fraction * 100,
                                description=f"[cyan]{stage_name}[/cyan] {message}"[:70])
            return report

        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="migrate") as pool:
            futures = {}
            for pipeline in pipelines:
                pipeline._on_progress = make_reporter(pipeline.repo.name)
                futures[pool.submit(pipeline.run, **run_kwargs)] = pipeline

            for future in as_completed(futures):
                pipeline = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001 - one repo must not sink the batch
                    log.exception("%s failed unexpectedly", pipeline.repo.name)
                    from .pipeline import MigrationOutcome
                    outcome = MigrationOutcome(repository=pipeline.repo.name,
                                               status="failed", error=str(exc))
                outcomes.append(outcome)
                progress.update(tasks[pipeline.repo.name], completed=100,
                                description="[green]done[/green]" if outcome.ok
                                else "[red]failed[/red]")

    for outcome in outcomes:
        console.rule(f"[bold]{outcome.repository}[/bold]")
        _print_outcome(outcome)
    return outcomes


def _print_outcome(outcome) -> None:
    table = Table("Stage", "Status", "Duration", "Detail", show_lines=False)
    colours = {"succeeded": "green", "failed": "red", "cancelled": "yellow", "skipped": "dim"}
    for stage in outcome.stages:
        colour = colours.get(stage.status, "white")
        detail = stage.error or stage.message
        table.add_row(stage.name, f"[{colour}]{stage.status}[/{colour}]",
                      _fmt_seconds(stage.duration), (detail or "")[:90])
    console.print(table)

    if outcome.ok:
        url = outcome.push.project_url if outcome.push else ""
        console.print(f"[green]Migration complete[/green]" + (f" -> {url}" if url else ""))
    else:
        console.print(f"[red]Migration {outcome.status}[/red]")
        if outcome.error:
            err_console.print(Panel(outcome.error, border_style="red", expand=False))
    if outcome.reports.get("latest_html"):
        console.print(f"[dim]Report: {outcome.reports['latest_html']}[/dim]")


def _print_summary(outcomes) -> None:
    if len(outcomes) < 2:
        return
    console.rule("[bold]Summary[/bold]")
    table = Table("Repository", "Status", "Duration", "GitLab project")
    for outcome in outcomes:
        colour = "green" if outcome.ok else "red"
        table.add_row(outcome.repository, f"[{colour}]{outcome.status}[/{colour}]",
                      _fmt_seconds(outcome.duration),
                      outcome.push.project_path if outcome.push else "")
    console.print(table)


# --------------------------------------------------------------------------- #
# sync / cutover / verify
# --------------------------------------------------------------------------- #

@app.command()
def sync(
    config_path: Path = typer.Option(..., "--config", "-c", exists=True),
    repo: List[str] = typer.Option([], "--repo", "-r"),
    once: bool = typer.Option(False, "--once", help="Run a single round and exit."),
    watch: bool = typer.Option(False, "--watch", help="Keep syncing on the configured interval."),
    no_push: bool = typer.Option(False, "--no-push", help="Fetch and re-derive without pushing."),
    force_push: bool = typer.Option(False, "--force-push"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Bring GitLab up to date with new Subversion commits."""
    _install_signal_handlers()
    from .sync.incremental import SyncRunner

    exit_code = 0
    try:
        config, state = _bootstrap(config_path, verbose, False)
        pipelines = build_pipelines(config, state, only=list(repo) or None, cancel=CANCEL,
                                    create_jobs=False)
        for pipeline in pipelines:
            console.rule(f"[bold]{pipeline.repo.name}[/bold]")
            runner = SyncRunner(pipeline, state, cancel=CANCEL)
            if watch and not once:
                interval = pipeline.repo.sync.interval_minutes
                console.print(f"Syncing every {interval} minute(s). Press Ctrl+C to stop.")
                results = runner.run_forever(interval, on_round=_print_sync)
                if any(not r.ok for r in results):
                    exit_code = 1
            else:
                with _progress_bar() as (progress, task):
                    result = runner.run_once(
                        push=not no_push, force_push=force_push,
                        on_progress=lambda f, m: progress.update(
                            task, completed=f * 100, description=m))
                _print_sync(result)
                if not result.ok:
                    exit_code = 1
    except Exception as exc:  # noqa: BLE001
        _fail(exc, verbose)
    raise typer.Exit(code=exit_code)


def _print_sync(result) -> None:
    if result.up_to_date:
        console.print(f"[green]Up to date[/green] at r{result.current_revision:,}")
    elif result.ok:
        console.print(f"[green]Synced[/green] {result.new_revisions:,} revision(s) "
                      f"through r{result.current_revision:,}"
                      + (" [yellow](forced push)[/yellow]" if result.forced else ""))
    else:
        err_console.print(f"[red]Sync failed:[/red] {result.error}")
    for warning in result.warnings:
        console.print(f"[yellow]{warning}[/yellow]")


@app.command()
def cutover(
    config_path: Path = typer.Option(..., "--config", "-c", exists=True),
    repo: List[str] = typer.Option([], "--repo", "-r"),
    no_lock: bool = typer.Option(False, "--no-lock", help="Do not make Subversion read-only."),
    no_verify: bool = typer.Option(False, "--no-verify"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Final sync, make Subversion read-only, verify, and record the handover.

    This is the irreversible step of the migration: after it, developers commit to
    GitLab and Subversion rejects writes. `svn2gitlab unlock` reverses the lock.
    """
    _install_signal_handlers()
    from .sync.incremental import SyncRunner

    exit_code = 0
    try:
        config, state = _bootstrap(config_path, verbose, False)
        pipelines = build_pipelines(config, state, only=list(repo) or None, cancel=CANCEL,
                                    create_jobs=False)
        if not yes and not dry_run:
            names = ", ".join(p.repo.name for p in pipelines)
            console.print(Panel(
                f"About to cut over: [bold]{names}[/bold]\n\n"
                "This performs a final sync, makes the Subversion repository reject new "
                "commits, and verifies the result. Developers must be told to switch to "
                "GitLab before you continue.",
                title="[yellow]Confirm cutover[/yellow]", border_style="yellow", expand=False))
            if not typer.confirm("Proceed?"):
                console.print("Cancelled.")
                raise typer.Exit(code=1)

        for pipeline in pipelines:
            console.rule(f"[bold]{pipeline.repo.name}[/bold]")
            runner = SyncRunner(pipeline, state, cancel=CANCEL)
            with _progress_bar() as (progress, task):
                outcome = runner.cutover(
                    lock_svn=not no_lock and pipeline.repo.sync.lock_svn_on_cutover,
                    verify=not no_verify,
                    dry_run=dry_run,
                    on_progress=lambda f, m: progress.update(task, completed=f * 100,
                                                             description=m),
                )
            _print_cutover(outcome)
            if outcome.get("status") != "complete":
                exit_code = 1
    except Exception as exc:  # noqa: BLE001
        _fail(exc, verbose)
    raise typer.Exit(code=exit_code)


def _print_cutover(outcome: dict) -> None:
    status = outcome.get("status")
    lock = outcome.get("lock") or {}
    if lock.get("locked"):
        console.print(f"[green]Subversion is now read-only[/green] ({lock.get('hook_path')})")
    for warning in lock.get("warnings") or []:
        console.print(f"[yellow]{warning}[/yellow]")

    final = outcome.get("final_sync") or {}
    if final:
        console.print(f"Final sync: r{final.get('current_revision', 0):,} "
                      f"({final.get('new_revisions', 0)} new revision(s))")

    verification = outcome.get("verification") or {}
    if verification:
        if verification.get("ok"):
            console.print("[green]Verification passed: content matches Subversion.[/green]")
        else:
            console.print(f"[red]Verification found "
                          f"{verification.get('total_differences')} difference(s).[/red]")

    if status == "complete":
        console.print("[bold green]Cutover complete.[/bold green]")
    else:
        console.print(f"[red]Cutover did not complete: {status}[/red]")
        if outcome.get("error"):
            err_console.print(outcome["error"])


@app.command()
def verify(
    config_path: Path = typer.Option(..., "--config", "-c", exists=True),
    repo: List[str] = typer.Option([], "--repo", "-r"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Re-run verification against the current converted state."""
    _install_signal_handlers()
    exit_code = 0
    try:
        config, state = _bootstrap(config_path, verbose, False)
        pipelines = build_pipelines(config, state, only=list(repo) or None, cancel=CANCEL,
                                    create_jobs=False)
        for pipeline in pipelines:
            console.rule(f"[bold]{pipeline.repo.name}[/bold]")
            with _progress_bar() as (progress, task):
                report = pipeline.verify(
                    on_progress=lambda f, m: progress.update(task, completed=f * 100,
                                                             description=m))
            _print_verification(report)
            if not report.ok:
                exit_code = 1
    except Exception as exc:  # noqa: BLE001
        _fail(exc, verbose)
    raise typer.Exit(code=exit_code)


def _print_verification(report) -> None:
    table = Table("Branch", "SVN path", "Revision", "Files", "Matched", "LFS", "Result")
    for branch in report.branches:
        if branch.skipped:
            result = f"[yellow]skipped: {branch.skip_reason}[/yellow]"
        elif branch.ok:
            result = "[green]identical[/green]"
        else:
            result = f"[red]{len(branch.differences)} difference(s)[/red]"
        table.add_row(branch.branch, branch.svn_path or "/", f"r{branch.svn_revision:,}",
                      f"{branch.files_compared:,}", f"{branch.files_matched:,}",
                      f"{branch.lfs_files:,}", result)
    console.print(table)

    for branch in report.branches:
        for diff in branch.differences[:20]:
            console.print(f"  [red]{diff.kind}[/red] {diff.path}: {diff.detail}")

    if report.missing_tags:
        console.print(f"[yellow]{len(report.missing_tags)} tag(s) missing in Git: "
                      f"{', '.join(report.missing_tags[:10])}[/yellow]")
    console.print(f"\nRevision coverage: {report.revision_coverage * 100:.1f}% "
                  f"({report.git_commits_with_revision:,} commits for "
                  f"{report.svn_revision_count:,} revisions)")


# --------------------------------------------------------------------------- #
# status / unlock / config
# --------------------------------------------------------------------------- #

@app.command()
def status(
    config_path: Path = typer.Option(..., "--config", "-c", exists=True),
    repo: List[str] = typer.Option([], "--repo", "-r"),
    limit: int = typer.Option(10, "--limit"),
) -> None:
    """Show job history, stage progress and sync state."""
    try:
        config, state = _bootstrap(config_path, False, True)
        wanted = {r.lower() for r in repo} if repo else None

        jobs = state.list_jobs(limit=limit)
        if jobs:
            table = Table("Job", "Repository", "Status", "Started", "Duration", "Error")
            for job in jobs:
                if wanted and job["repo"].lower() not in wanted:
                    continue
                colour = {"succeeded": "green", "failed": "red",
                          "running": "cyan"}.get(job["status"], "yellow")
                started = time.strftime("%Y-%m-%d %H:%M",
                                        time.localtime(job["started_at"] or job["created_at"]))
                duration = ((job["finished_at"] or time.time()) - (job["started_at"] or 0)) \
                    if job["started_at"] else 0
                table.add_row(job["id"][:8], job["repo"],
                              f"[{colour}]{job['status']}[/{colour}]", started,
                              _fmt_seconds(duration), (job["error"] or "")[:60])
            console.print(table)

            newest = jobs[0]
            stages = state.list_stages(newest["id"])
            if stages:
                console.print(f"\n[bold]Stages for job {newest['id'][:8]} "
                              f"({newest['repo']})[/bold]")
                stage_table = Table("Stage", "Status", "Progress", "Duration", "Detail")
                for stage_record in stages:
                    colour = {"succeeded": "green", "failed": "red",
                              "running": "cyan"}.get(stage_record.status.value, "yellow")
                    stage_table.add_row(
                        stage_record.name,
                        f"[{colour}]{stage_record.status.value}[/{colour}]",
                        f"{stage_record.progress * 100:.0f}%",
                        _fmt_seconds(stage_record.duration),
                        (stage_record.message or stage_record.error or "")[:60])
                console.print(stage_table)
        else:
            console.print("[dim]No jobs recorded yet.[/dim]")

        sync_rows = state.list_sync_state()
        if sync_rows:
            console.print("\n[bold]Sync state[/bold]")
            sync_table = Table("Repository", "Last SVN rev", "Last sync", "Last push",
                               "Failures", "Cutover")
            for row in sync_rows:
                if wanted and row["repo"].lower() not in wanted:
                    continue
                sync_table.add_row(
                    row["repo"], str(row["last_svn_rev"] or "-"),
                    _fmt_time(row["last_sync_at"]), _fmt_time(row["last_push_at"]),
                    str(row["consecutive_failures"] or 0),
                    _fmt_time(row["cutover_at"]) if row["cutover_at"] else "-")
            console.print(sync_table)
    except Exception as exc:  # noqa: BLE001
        _fail(exc, False)


@app.command()
def unlock(
    config_path: Path = typer.Option(..., "--config", "-c", exists=True),
    repo: List[str] = typer.Option([], "--repo", "-r"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Remove a cutover lock and allow Subversion commits again."""
    from .sync.cutover import unlock_repository

    try:
        config, state = _bootstrap(config_path, False, False)
        for resolved in config.all_resolved(list(repo) or None):
            if not resolved.source.local_path:
                console.print(f"[yellow]{resolved.name}: no local_path configured; the lock "
                              "must be removed on the Subversion server.[/yellow]")
                continue
            result = unlock_repository(Path(resolved.source.local_path), dry_run=dry_run)
            console.print(f"[green]{resolved.name}:[/green] {result.message}")
            for warning in result.warnings:
                console.print(f"  [yellow]{warning}[/yellow]")
    except Exception as exc:  # noqa: BLE001
        _fail(exc, False)


@app.command("config")
def show_config(
    config_path: Path = typer.Option(..., "--config", "-c", exists=True),
    show_secrets: bool = typer.Option(False, "--show-secrets"),
) -> None:
    """Validate the configuration and print it fully resolved."""
    try:
        config = load_config(config_path)
        console.print(dump_config(config, redact=not show_secrets))
        console.print(f"[green]Configuration is valid[/green] "
                      f"({len(config.repositories)} repository/repositories, "
                      f"workdir {config.workdir_path()})")
    except Exception as exc:  # noqa: BLE001
        _fail(exc, False)


@app.command()
def serve(
    config_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    host: str = typer.Option("127.0.0.1", "--host",
                             help="Bind address. Keep it on localhost unless the machine is "
                                  "trusted - the dashboard has no authentication."),
    port: int = typer.Option(8720, "--port"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Run the local web dashboard."""
    from .web.server import run_server

    setup_logging(Path.cwd() / "svn2gitlab-web.log", verbose=False)
    console.print(f"[green]Dashboard:[/green] http://{host}:{port}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print("[yellow]Warning: the dashboard has no authentication. Binding it to a "
                      "non-loopback address exposes stored credentials to anyone who can reach "
                      "this port.[/yellow]")
    run_server(config_path=config_path, host=host, port=port, open_browser=open_browser)


@app.command()
def version() -> None:
    """Print the tool version and the versions of the tools it drives."""
    console.print(f"svn2gitlab {__version__}")
    console.print(f"python     {sys.version.split()[0]} ({sys.platform})")
    tools = detect_tools()
    for info in tools.all():
        console.print(f"{info.name:<10} {info.version or '[dim]not found[/dim]'}")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

class _ProgressContext:
    def __init__(self) -> None:
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=28),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        self.task = None

    def __enter__(self):
        self.progress.__enter__()
        self.task = self.progress.add_task("starting", total=100)
        return self.progress, self.task

    def __exit__(self, *exc_info):
        return self.progress.__exit__(*exc_info)


def _progress_bar() -> _ProgressContext:
    return _ProgressContext()


def _fmt_seconds(seconds: float) -> str:
    seconds = float(seconds or 0)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _fmt_time(value: Optional[float]) -> str:
    if not value:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        err_console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
