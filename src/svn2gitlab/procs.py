"""Subprocess execution: streaming, cancellable, Windows-safe, credential-aware.

Every external call in the tool goes through here. Highlights:

* stdout/stderr are streamed line by line so a 6-hour `git svn fetch` reports progress
  instead of looking hung;
* a `threading.Event` can cancel a running child (and its whole process tree on
  Windows via taskkill, on POSIX via process group);
* secrets passed via argv are registered with the redactor before the command is
  logged, and passwords are preferentially fed through stdin or environment;
* console windows are suppressed on Windows so a GUI/service launch stays silent.
"""

from __future__ import annotations

import io
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, IO, List, Mapping, Optional, Sequence, Union

from .errors import AbortedError, CommandError
from .logging_setup import REDACTOR, get_logger

log = get_logger("proc")

IS_WINDOWS = os.name == "nt"
CommandArg = Union[str, os.PathLike]

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200


@dataclass
class ProcResult:
    cmd: List[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    lines: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self, remedy: str = "") -> "ProcResult":
        if not self.ok:
            raise CommandError(self.cmd, self.returncode, self.stdout, self.stderr, remedy)
        return self


def quote(cmd: Sequence[CommandArg]) -> str:
    """Human-readable, redacted rendering of a command line."""
    parts = [str(c) for c in cmd]
    if IS_WINDOWS:
        rendered = subprocess.list2cmdline(parts)
    else:
        rendered = " ".join(shlex.quote(p) for p in parts)
    return REDACTOR.scrub(rendered)


def _popen_kwargs(cwd: Optional[Path], env: Optional[Mapping[str, str]]) -> Dict[str, object]:
    kwargs: Dict[str, object] = {
        "cwd": str(cwd) if cwd else None,
        "env": dict(env) if env is not None else None,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _terminate_tree(proc: subprocess.Popen) -> None:
    """Kill the child and everything it spawned.

    `git svn` forks perl which forks svn; killing only the parent leaves orphans
    holding locks on the working copy, so we always take out the tree.
    """
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
                check=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            deadline = time.time() + 10
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.2)
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception as exc:  # pragma: no cover - best effort
        log.debug("failed to terminate process tree %s: %s", proc.pid, exc)
    finally:
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


def _reader(stream: IO[bytes], sink: Optional[List[str]], on_line: Optional[Callable[[str], None]], tag: str) -> None:
    try:
        wrapper = io.TextIOWrapper(stream, encoding="utf-8", errors="replace", newline="")
        buf = ""
        while True:
            chunk = wrapper.read(1)
            if not chunk:
                break
            # git/svn use bare \r for in-place progress updates; treat both as breaks.
            if chunk in ("\n", "\r"):
                if buf:
                    if sink is not None:
                        sink.append(buf)
                    if on_line:
                        try:
                            on_line(buf)
                        except Exception:
                            log.debug("line callback raised", exc_info=True)
                    buf = ""
            else:
                buf += chunk
        if buf:
            if sink is not None:
                sink.append(buf)
            if on_line:
                try:
                    on_line(buf)
                except Exception:
                    log.debug("line callback raised", exc_info=True)
    except Exception as exc:  # pragma: no cover
        log.debug("reader thread for %s died: %s", tag, exc)


def run(
    cmd: Sequence[CommandArg],
    cwd: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
    input_text: Optional[str] = None,
    timeout: Optional[float] = None,
    check: bool = True,
    on_stdout: Optional[Callable[[str], None]] = None,
    on_stderr: Optional[Callable[[str], None]] = None,
    cancel: Optional[threading.Event] = None,
    secrets: Sequence[Optional[str]] = (),
    log_command: bool = True,
    remedy: str = "",
    collect_stdout: bool = True,
) -> ProcResult:
    """Run a command to completion, streaming both output channels.

    `cancel` is polled while the child runs; when set, the process tree is killed and
    `AbortedError` is raised. This is what makes the web UI's Stop button real.

    Set `collect_stdout=False` with an `on_stdout` callback for commands whose output
    is unbounded (`svn log` over 200k revisions) so nothing accumulates in memory.
    """
    REDACTOR.add_all(secrets)
    argv = [str(c) for c in cmd]
    cwd_path = Path(cwd) if cwd else None
    if log_command:
        log.debug("exec: %s%s", quote(argv), f" (cwd={cwd_path})" if cwd_path else "")

    merged_env = os.environ.copy()
    if env:
        merged_env.update({k: str(v) for k, v in env.items()})

    started = time.time()
    kwargs = _popen_kwargs(cwd_path, merged_env)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            **kwargs,  # type: ignore[arg-type]
        )
    except FileNotFoundError as exc:
        raise CommandError(argv, 127, "", str(exc), remedy or f"{argv[0]} was not found on PATH") from exc
    except PermissionError as exc:
        raise CommandError(argv, 126, "", str(exc), remedy or f"{argv[0]} is not executable") from exc

    out_lines: List[str] = []
    err_lines: List[str] = []
    out_sink: Optional[List[str]] = out_lines if collect_stdout else None
    threads = [
        threading.Thread(target=_reader, args=(proc.stdout, out_sink, on_stdout, "stdout"), daemon=True),
        threading.Thread(target=_reader, args=(proc.stderr, err_lines, on_stderr, "stderr"), daemon=True),
    ]
    for t in threads:
        t.start()

    if input_text is not None and proc.stdin:
        try:
            proc.stdin.write(input_text.encode("utf-8"))
            proc.stdin.close()
        except Exception:
            pass

    aborted = False
    timed_out = False
    deadline = started + timeout if timeout else None
    try:
        while True:
            try:
                proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel is not None and cancel.is_set():
                aborted = True
                _terminate_tree(proc)
                break
            if deadline and time.time() > deadline:
                timed_out = True
                _terminate_tree(proc)
                break
    finally:
        for t in threads:
            t.join(timeout=15)

    duration = time.time() - started
    result = ProcResult(
        cmd=argv,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout="\n".join(out_lines),
        stderr="\n".join(err_lines),
        duration=duration,
        lines=out_lines,
    )

    if aborted:
        raise AbortedError(f"cancelled: {quote(argv)}", "Run was stopped by the operator.")
    if timed_out:
        raise CommandError(argv, result.returncode, result.stdout, result.stderr,
                           remedy or f"timed out after {timeout:.0f}s")
    if check:
        result.check(remedy)
    return result


def run_capture(cmd: Sequence[CommandArg], **kwargs) -> str:
    """Run and return stripped stdout. Raises on failure."""
    kwargs.setdefault("check", True)
    return run(cmd, **kwargs).stdout.strip()


def run_bytes(
    cmd: Sequence[CommandArg],
    cwd: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
    input_bytes: Optional[bytes] = None,
    timeout: Optional[float] = None,
    check: bool = True,
) -> bytes:
    """Capture raw stdout without any text decoding.

    Only for output with a known, bounded size - `git cat-file --batch` over a list of
    LFS pointer blobs, for example. Never point this at unbounded output.
    """
    argv = [str(c) for c in cmd]
    merged_env = os.environ.copy()
    if env:
        merged_env.update({k: str(v) for k, v in env.items()})
    log.debug("exec(bytes): %s", quote(argv))
    kwargs = _popen_kwargs(Path(cwd) if cwd else None, merged_env)
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,  # type: ignore[arg-type]
    )
    try:
        out, err = proc.communicate(input=input_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_tree(proc)
        raise CommandError(argv, -1, "", f"timed out after {timeout}s")
    if check and proc.returncode != 0:
        raise CommandError(argv, proc.returncode, "", err.decode("utf-8", "replace"))
    return out


def pipe(
    producer: Sequence[CommandArg],
    consumer: Sequence[CommandArg],
    cwd: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
    cancel: Optional[threading.Event] = None,
    on_stderr: Optional[Callable[[str], None]] = None,
    transform: Optional[Callable[[IO[bytes], IO[bytes]], None]] = None,
    timeout: Optional[float] = None,
) -> None:
    """Run `producer | consumer`, optionally with a Python `transform` in the middle.

    Used for the `git fast-export | rewrite | git fast-import` history filter, where a
    shell pipeline would be both unportable and unable to host Python logic.
    """
    prod_argv = [str(c) for c in producer]
    cons_argv = [str(c) for c in consumer]
    cwd_path = Path(cwd) if cwd else None
    merged_env = os.environ.copy()
    if env:
        merged_env.update({k: str(v) for k, v in env.items()})
    log.debug("pipe: %s | %s%s", quote(prod_argv),
              ("<python filter> | " if transform else "") + quote(cons_argv),
              f" (cwd={cwd_path})" if cwd_path else "")

    kwargs = _popen_kwargs(cwd_path, merged_env)
    p1 = subprocess.Popen(prod_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, bufsize=0, **kwargs)  # type: ignore[arg-type]
    if transform is None:
        p2 = subprocess.Popen(cons_argv, stdin=p1.stdout, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, bufsize=0, **kwargs)  # type: ignore[arg-type]
        if p1.stdout:
            p1.stdout.close()  # let p1 receive SIGPIPE if p2 dies
    else:
        p2 = subprocess.Popen(cons_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, bufsize=0, **kwargs)  # type: ignore[arg-type]

    err1: List[str] = []
    err2: List[str] = []
    out2: List[str] = []
    threads = [
        threading.Thread(target=_reader, args=(p1.stderr, err1, on_stderr, "p1err"), daemon=True),
        threading.Thread(target=_reader, args=(p2.stderr, err2, on_stderr, "p2err"), daemon=True),
        threading.Thread(target=_reader, args=(p2.stdout, out2, None, "p2out"), daemon=True),
    ]
    for t in threads:
        t.start()

    transform_error: List[BaseException] = []
    if transform is not None:
        def _pump() -> None:
            try:
                transform(p1.stdout, p2.stdin)  # type: ignore[arg-type]
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                transform_error.append(exc)
            finally:
                try:
                    if p2.stdin:
                        p2.stdin.close()
                except Exception:
                    pass
        pump = threading.Thread(target=_pump, daemon=True)
        pump.start()
        threads.append(pump)

    deadline = time.time() + timeout if timeout else None
    aborted = False
    while True:
        if p1.poll() is not None and p2.poll() is not None:
            break
        if cancel is not None and cancel.is_set():
            aborted = True
            _terminate_tree(p2)
            _terminate_tree(p1)
            break
        if deadline and time.time() > deadline:
            _terminate_tree(p2)
            _terminate_tree(p1)
            raise CommandError(cons_argv, -1, "", "\n".join(err2), f"pipeline timed out after {timeout:.0f}s")
        time.sleep(0.2)

    for t in threads:
        t.join(timeout=30)

    if aborted:
        raise AbortedError("cancelled pipeline", "Run was stopped by the operator.")
    if transform_error:
        raise transform_error[0]
    if p1.returncode not in (0, None):
        raise CommandError(prod_argv, p1.returncode or -1, "", "\n".join(err1))
    if p2.returncode not in (0, None):
        raise CommandError(cons_argv, p2.returncode or -1, "\n".join(out2), "\n".join(err2))


def which(name: str, extra_dirs: Sequence[Union[str, Path]] = ()) -> Optional[str]:
    """`shutil.which` that also looks in explicit directories (installer-bundled tools)."""
    import shutil

    found = shutil.which(name)
    if found:
        return found
    exts = os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").split(";") if IS_WINDOWS else [""]
    for directory in extra_dirs:
        base = Path(directory)
        if not base.is_dir():
            continue
        for ext in exts:
            candidate = base / f"{name}{ext.lower()}"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            candidate = base / f"{name}{ext}"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def is_frozen() -> bool:
    """True when running from the PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def bundle_dir() -> Optional[Path]:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return None
