"""Exception hierarchy.

Every error the operator can act on carries a `remedy` so the CLI and the web UI
can show *what to do*, not just what broke.
"""

from __future__ import annotations


class Svn2GitlabError(Exception):
    """Base class for all tool errors."""

    def __init__(self, message: str, remedy: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy

    def __str__(self) -> str:
        if self.remedy:
            return f"{self.message}\n  -> {self.remedy}"
        return self.message


class ConfigError(Svn2GitlabError):
    """Configuration file is missing, malformed or internally inconsistent."""


class ToolMissingError(Svn2GitlabError):
    """A required external executable (git, svn, svnadmin, git-lfs) was not found."""


class CommandError(Svn2GitlabError):
    """An external command exited non-zero."""

    def __init__(self, cmd, returncode: int, stdout: str = "", stderr: str = "", remedy: str = ""):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        tail = (stderr or stdout or "").strip().splitlines()[-12:]
        detail = "\n    ".join(tail)
        super().__init__(
            f"command failed (exit {returncode}): {' '.join(str(c) for c in cmd)}"
            + (f"\n    {detail}" if detail else ""),
            remedy,
        )


class SvnError(Svn2GitlabError):
    """Subversion-side failure."""


class ConversionError(Svn2GitlabError):
    """Conversion or history-rewrite failure."""


class GitLabError(Svn2GitlabError):
    """GitLab API or push failure."""

    def __init__(self, message: str, status: int = 0, body: str = "", remedy: str = ""):
        self.status = status
        self.body = body
        super().__init__(message, remedy)


class VerificationError(Svn2GitlabError):
    """Post-migration verification found differences."""


class AbortedError(Svn2GitlabError):
    """Operator cancelled the run."""
