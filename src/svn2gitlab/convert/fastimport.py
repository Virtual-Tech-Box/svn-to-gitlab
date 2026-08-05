"""A bidirectional `git fast-import` driver.

The interesting part is copy resolution. A Subversion branch or tag is a directory
copy: `Node-copyfrom-rev: 4, Node-copyfrom-path: trunk` means "this new path is
whatever `trunk` looked like at r4". Converting that faithfully needs the historical
tree, and the dump does not carry it.

Holding every revision's tree in memory does not scale — a 100k-file repository with
a few hundred copy sources would need tens of millions of entries. Instead we use
fast-import's own `ls` command, which answers "what was at this path in that commit"
against the objects it has already written. The tree we get back is then attached at
the destination with a single `M 040000 <tree-sha> <path>`, so copying a 30,000-file
branch costs one line of output rather than 30,000.

That requires reading fast-import's responses while still writing to it, hence the
bidirectional pipe and the careful flushing here.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..errors import ConversionError
from ..logging_setup import get_logger
from ..procs import IS_WINDOWS

log = get_logger("fastimport")


@dataclass
class LsResult:
    mode: str
    kind: str        # "blob" | "tree" | "missing"
    dataref: str

    @property
    def missing(self) -> bool:
        return self.kind == "missing"

    @property
    def is_tree(self) -> bool:
        return self.kind == "tree"


class FastImport:
    """Writes a fast-import stream and reads back `ls` answers."""

    def __init__(self, git_exe: str, repo: Path, env: Optional[Dict[str, str]] = None) -> None:
        self.git_exe = git_exe
        self.repo = Path(repo)
        self.env = env
        self._proc: Optional[subprocess.Popen] = None
        self._mark = 0
        self._stderr: List[str] = []
        self._stderr_thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> "FastImport":
        return self.start()

    def __exit__(self, exc_type, *_exc) -> None:
        self.close(abort=exc_type is not None)

    def start(self) -> "FastImport":
        cmd = [
            self.git_exe, "fast-import",
            "--quiet",
            "--done",
            # Marks are how we refer back to commits when resolving copies.
            "--force",
        ]
        kwargs = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            cmd, cwd=str(self.repo), env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, **kwargs,
        )

        def drain() -> None:
            assert self._proc and self._proc.stderr
            for raw in self._proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._stderr.append(line)
                    log.debug("fast-import: %s", line)

        self._stderr_thread = threading.Thread(target=drain, daemon=True)
        self._stderr_thread.start()
        return self

    def close(self, abort: bool = False) -> None:
        if not self._proc:
            return
        try:
            if not abort and self._proc.stdin:
                self._write(b"done\n")
                self._proc.stdin.close()
            elif self._proc.stdin:
                self._proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        returncode = self._proc.wait(timeout=1800)
        if self._stderr_thread:
            self._stderr_thread.join(timeout=30)
        if returncode != 0 and not abort:
            detail = "\n    ".join(self._stderr[-15:])
            raise ConversionError(
                f"git fast-import failed (exit {returncode})"
                + (f"\n    {detail}" if detail else ""),
                "This usually means the generated stream was malformed. Re-run with "
                "`convert.engine: git-svn` to fall back to the reference converter, "
                "and please report the repository shape that triggered it.",
            )
        self._proc = None

    # -- writing -------------------------------------------------------------

    def _write(self, data: bytes) -> None:
        if not self._proc or not self._proc.stdin:
            raise ConversionError("fast-import is not running")
        try:
            self._proc.stdin.write(data)
        except BrokenPipeError:
            detail = "\n    ".join(self._stderr[-15:])
            raise ConversionError(
                "git fast-import closed the stream unexpectedly"
                + (f"\n    {detail}" if detail else ""))

    def next_mark(self) -> int:
        self._mark += 1
        return self._mark

    def blob(self, content: bytes) -> int:
        """Write a blob and return its mark."""
        mark = self.next_mark()
        self._write(b"blob\nmark :%d\ndata %d\n" % (mark, len(content)))
        self._write(content)
        self._write(b"\n")
        return mark

    def begin_commit(
        self,
        ref: str,
        author: Tuple[str, str, str],      # (name, email, "<ts> +0000")
        committer: Tuple[str, str, str],
        message: bytes,
        parent: Optional[str] = None,      # ":mark" or a sha
        merge_parents: Optional[List[str]] = None,
    ) -> int:
        mark = self.next_mark()
        name, email, when = author
        cname, cemail, cwhen = committer
        self._write(f"commit {ref}\nmark :{mark}\n".encode("utf-8"))
        self._write(f"author {name} <{email}> {when}\n".encode("utf-8", "replace"))
        self._write(f"committer {cname} <{cemail}> {cwhen}\n".encode("utf-8", "replace"))
        self._write(b"data %d\n" % len(message))
        self._write(message)
        if parent:
            self._write(f"from {parent}\n".encode("ascii"))
        for extra in merge_parents or []:
            self._write(f"merge {extra}\n".encode("ascii"))
        return mark

    def end_commit(self) -> None:
        self._write(b"\n")

    # -- tree mutation within the current commit -----------------------------

    def filemodify(self, mode: str, dataref: str, path: str) -> None:
        self._write(f"M {mode} {dataref} ".encode("ascii"))
        self._write(_quote_path(path))
        self._write(b"\n")

    def filedelete(self, path: str) -> None:
        self._write(b"D ")
        self._write(_quote_path(path))
        self._write(b"\n")

    def deleteall(self) -> None:
        self._write(b"deleteall\n")

    def reset(self, ref: str, target: Optional[str] = None) -> None:
        self._write(f"reset {ref}\n".encode("utf-8"))
        if target:
            self._write(f"from {target}\n\n".encode("ascii"))

    def annotated_tag(self, name: str, target: str, tagger: Tuple[str, str, str],
                      message: bytes) -> None:
        tname, temail, twhen = tagger
        self._write(f"tag {name}\nfrom {target}\n".encode("utf-8"))
        self._write(f"tagger {tname} <{temail}> {twhen}\n".encode("utf-8", "replace"))
        self._write(b"data %d\n" % len(message))
        self._write(message)
        self._write(b"\n")

    # -- reading back --------------------------------------------------------

    def ls(self, commit: str, path: str) -> LsResult:
        """Ask what lives at `path` in `commit` (a ':mark' or sha).

        Answers arrive on fast-import's stdout as
        `<mode> <type> <dataref>\\t<path>` or `missing <path>`.
        """
        if not self._proc or not self._proc.stdout:
            raise ConversionError("fast-import is not running")
        query = f"ls {commit} ".encode("ascii") + _quote_path(path) + b"\n"
        self._write(query)
        self._proc.stdin.flush()  # type: ignore[union-attr]

        line = self._proc.stdout.readline()
        if not line:
            detail = "\n    ".join(self._stderr[-15:])
            raise ConversionError(
                "fast-import stopped responding while resolving a copy"
                + (f"\n    {detail}" if detail else ""))
        text = line.decode("utf-8", "surrogateescape").rstrip("\n")
        if text.startswith("missing"):
            return LsResult(mode="", kind="missing", dataref="")
        head, _, _rest = text.partition("\t")
        parts = head.split(" ")
        if len(parts) < 3:
            raise ConversionError(f"unexpected fast-import ls response: {text!r}")
        mode, kind, dataref = parts[0], parts[1], parts[2]
        return LsResult(mode=mode, kind=kind, dataref=dataref)

    def checkpoint(self) -> None:
        """Flush objects so far. Expensive; only needed before external git reads."""
        self._write(b"checkpoint\n")
        if self._proc and self._proc.stdin:
            self._proc.stdin.flush()


def _quote_path(path: str) -> bytes:
    """Encode a path for fast-import, quoting only when required.

    fast-import treats a leading double quote as the start of a C-style quoted
    string, and cannot represent a literal newline unquoted.
    """
    raw = path.encode("utf-8", "surrogateescape")
    if raw.startswith(b'"') or b"\n" in raw:
        escaped = raw.replace(b"\\", b"\\\\").replace(b'"', b'\\"').replace(b"\n", b"\\n")
        return b'"' + escaped + b'"'
    return raw
