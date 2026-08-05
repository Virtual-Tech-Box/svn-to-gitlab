"""Cutover: making Subversion read-only and finishing the migration.

The only reliable way to stop commits to a Subversion repository, without taking the
server down for everyone else, is a `pre-commit` hook that exits non-zero. Everything
here is written so it can be undone: the previous hook is preserved, and
`unlock_repository` puts it back exactly as it was.

Locking requires filesystem access to the repository, which means running on the SVN
server. For a remote-only migration we cannot lock, and we say so plainly rather than
pretending the cutover is complete.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from ..errors import SvnError
from ..logging_setup import get_logger
from ..procs import IS_WINDOWS

log = get_logger("cutover")

MARKER = "svn2gitlab-readonly"
BACKUP_SUFFIX = ".svn2gitlab-backup"


@dataclass
class CutoverResult:
    locked: bool = False
    hook_path: str = ""
    backup_path: str = ""
    already_locked: bool = False
    exempt_users: List[str] = field(default_factory=list)
    message: str = ""
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "locked": self.locked,
            "hook_path": self.hook_path,
            "backup_path": self.backup_path,
            "already_locked": self.already_locked,
            "exempt_users": self.exempt_users,
            "message": self.message,
            "warnings": self.warnings,
        }


def _hook_path(repo: Path) -> Path:
    name = "pre-commit.bat" if IS_WINDOWS else "pre-commit"
    return repo / "hooks" / name


def _other_hook_path(repo: Path) -> Path:
    """The variant we are *not* writing, which may still exist and run."""
    name = "pre-commit" if IS_WINDOWS else "pre-commit.bat"
    return repo / "hooks" / name


def is_locked(repo: Path) -> bool:
    hook = _hook_path(repo)
    if not hook.is_file():
        return False
    try:
        return MARKER in hook.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _windows_hook(message: str, exempt: Sequence[str]) -> str:
    lines = [
        "@ECHO OFF",
        f"REM {MARKER} - installed by svn2gitlab during migration to GitLab.",
        "REM Remove this file (or run `svn2gitlab unlock`) to allow commits again.",
        "REM Argument 1 is the repository path, argument 2 the transaction name.",
        "SETLOCAL",
        "SET REPOS=%1",
        "SET TXN=%2",
    ]
    if exempt:
        lines += [
            'FOR /F "usebackq delims=" %%A IN (`svnlook author -t %TXN% %REPOS%`) DO SET AUTHOR=%%A',
        ]
        for user in exempt:
            safe = user.replace('"', "")
            lines.append(f'IF /I "%AUTHOR%"=="{safe}" EXIT /B 0')
    for line in message.splitlines() or [message]:
        lines.append(f"ECHO {line.replace('%', '%%')} 1>&2")
    lines.append("EXIT /B 1")
    return "\r\n".join(lines) + "\r\n"


def _posix_hook(message: str, exempt: Sequence[str]) -> str:
    lines = [
        "#!/bin/sh",
        f"# {MARKER} - installed by svn2gitlab during migration to GitLab.",
        "# Remove this file (or run `svn2gitlab unlock`) to allow commits again.",
        'REPOS="$1"',
        'TXN="$2"',
    ]
    if exempt:
        lines.append('AUTHOR="$(svnlook author -t "$TXN" "$REPOS" 2>/dev/null)"')
        lines.append("case \"$AUTHOR\" in")
        lines.append("  " + "|".join(u.replace('"', "") for u in exempt) + ") exit 0 ;;")
        lines.append("esac")
    escaped = message.replace("\\", "\\\\").replace('"', '\\"')
    lines.append(f'echo "{escaped}" 1>&2')
    lines.append("exit 1")
    return "\n".join(lines) + "\n"


def lock_repository(
    repo: Path,
    message: str,
    exempt_users: Sequence[str] = (),
    dry_run: bool = False,
) -> CutoverResult:
    """Install a pre-commit hook that rejects every new commit."""
    result = CutoverResult(exempt_users=list(exempt_users), message=message)
    repo = Path(repo)
    if not (repo / "format").is_file() or not (repo / "db").is_dir():
        raise SvnError(
            f"{repo} is not a Subversion repository, so it cannot be locked",
            "Locking needs filesystem access to the repository. Run the cutover on the "
            "Subversion server with `source.local_path` set, or disable "
            "`sync.lock_svn_on_cutover` and lock it yourself.",
        )

    hooks_dir = repo / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = _hook_path(repo)
    result.hook_path = str(hook)

    if is_locked(repo):
        result.already_locked = True
        result.locked = True
        log.info("%s is already locked by a previous cutover", repo)
        return result

    other = _other_hook_path(repo)
    if other.is_file():
        result.warnings.append(
            f"a second pre-commit hook exists at {other}; Subversion runs only one of them, "
            "so review which is active on this platform"
        )

    if hook.is_file():
        backup = hook.with_suffix(hook.suffix + BACKUP_SUFFIX) if hook.suffix else \
            Path(str(hook) + BACKUP_SUFFIX)
        if not dry_run:
            shutil.copy2(hook, backup)
        result.backup_path = str(backup)
        result.warnings.append(
            f"the existing pre-commit hook was backed up to {backup} and replaced; "
            "its checks are not running while the repository is locked"
        )
        log.warning("existing pre-commit hook backed up to %s", backup)

    content = _windows_hook(message, exempt_users) if IS_WINDOWS \
        else _posix_hook(message, exempt_users)

    if dry_run:
        log.info("[dry-run] would write %s (%d bytes)", hook, len(content))
        result.locked = False
        return result

    hook.write_text(content, encoding="ascii", errors="replace")
    if not IS_WINDOWS:
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    result.locked = True
    log.info("locked %s - commits will now be rejected", repo)
    return result


def unlock_repository(repo: Path, dry_run: bool = False) -> CutoverResult:
    """Undo `lock_repository`, restoring any hook it displaced."""
    result = CutoverResult()
    repo = Path(repo)
    hook = _hook_path(repo)
    result.hook_path = str(hook)

    if not hook.is_file():
        result.message = "no pre-commit hook is installed; nothing to undo"
        return result
    if not is_locked(repo):
        result.warnings.append(
            "the pre-commit hook was not installed by svn2gitlab; leaving it untouched")
        result.message = "left an unrelated pre-commit hook in place"
        return result

    backup = Path(str(hook) + BACKUP_SUFFIX)
    if not backup.is_file() and hook.suffix:
        backup = hook.with_suffix(hook.suffix + BACKUP_SUFFIX)

    if dry_run:
        result.message = f"[dry-run] would remove {hook}" + (
            f" and restore {backup}" if backup.is_file() else "")
        return result

    hook.unlink()
    if backup.is_file():
        shutil.move(str(backup), str(hook))
        result.backup_path = str(backup)
        result.message = "removed the migration lock and restored the previous pre-commit hook"
    else:
        result.message = "removed the migration lock; commits are allowed again"
    log.info("unlocked %s", repo)
    return result


# --------------------------------------------------------------------------- #
# Scheduling the recurring sync
# --------------------------------------------------------------------------- #

def scheduled_task_command(
    executable: str,
    config_path: Path,
    repository: str,
    interval_minutes: int,
    task_name: str = "",
) -> List[str]:
    """The command that registers the sync with the platform scheduler.

    Returned rather than executed: registering a scheduled task is a change to the
    machine, and the operator should see and approve the exact command.
    """
    task_name = task_name or f"svn2gitlab-sync-{repository}"
    if IS_WINDOWS:
        action = f'"{executable}" sync --config "{config_path}" --repo {repository} --once'
        return [
            "schtasks", "/Create", "/TN", task_name, "/TR", action,
            "/SC", "MINUTE", "/MO", str(interval_minutes), "/RL", "HIGHEST", "/F",
        ]
    minutes = f"*/{interval_minutes}" if interval_minutes < 60 else "0"
    hours = "*" if interval_minutes < 60 else f"*/{max(1, interval_minutes // 60)}"
    cron = f"{minutes} {hours} * * * {executable} sync --config {config_path} --repo {repository} --once"
    return ["crontab-entry", cron]


def remove_scheduled_task_command(repository: str, task_name: str = "") -> List[str]:
    task_name = task_name or f"svn2gitlab-sync-{repository}"
    if IS_WINDOWS:
        return ["schtasks", "/Delete", "/TN", task_name, "/F"]
    return ["crontab-remove", task_name]
