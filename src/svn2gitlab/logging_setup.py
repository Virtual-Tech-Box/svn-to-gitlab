"""Logging with secret redaction and an in-memory ring buffer for the web UI.

Passwords and tokens end up in URLs (`https://oauth2:TOKEN@gitlab.com/...`) and in
`svn --password` arguments. Everything logged goes through the redactor first, so a
log file handed to a client never leaks a credential.
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
import re
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional

_LOGGER_NAME = "svn2gitlab"

# Patterns that are secrets regardless of whether we registered the literal value.
_STRUCTURAL_PATTERNS = [
    # scheme://user:secret@host
    (re.compile(r"(?P<pre>[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)[^\s@]+(?P<post>@)"), r"\g<pre>***\g<post>"),
    # --password foo / --password=foo
    (re.compile(r"(--password[= ])(\S+)"), r"\1***"),
    # PRIVATE-TOKEN: foo
    (re.compile(r"((?:PRIVATE-TOKEN|Authorization|Bearer)\s*[:=]\s*)(\S+)", re.I), r"\1***"),
    # GitLab PAT shapes
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{10,}\b"), "glpat-***"),
    (re.compile(r"\bglrt-[A-Za-z0-9_\-]{10,}\b"), "glrt-***"),
]


class Redactor:
    """Holds literal secrets registered at runtime plus structural patterns."""

    def __init__(self) -> None:
        self._literals: List[str] = []
        self._lock = threading.Lock()

    def add(self, secret: Optional[str]) -> None:
        if not secret or len(secret) < 4:
            return
        with self._lock:
            if secret not in self._literals:
                self._literals.append(secret)
                # Longest first so overlapping secrets redact fully.
                self._literals.sort(key=len, reverse=True)

    def add_all(self, secrets: Iterable[Optional[str]]) -> None:
        for s in secrets:
            self.add(s)

    def scrub(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            literals = list(self._literals)
        for secret in literals:
            if secret in text:
                text = text.replace(secret, "***")
        for pattern, repl in _STRUCTURAL_PATTERNS:
            text = pattern.sub(repl, text)
        return text


REDACTOR = Redactor()


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = REDACTOR.scrub(str(record.msg))
            # Only strings are scrubbed: coercing everything to str would break
            # numeric format specifiers such as %d in the message template.
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: (REDACTOR.scrub(v) if isinstance(v, str) else v)
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(REDACTOR.scrub(a) if isinstance(a, str) else a
                                        for a in record.args)
        except Exception:  # never let logging break the migration
            pass
        return True


class RingBufferHandler(logging.Handler):
    """Keeps the last N formatted records so the web UI can replay recent history."""

    def __init__(self, capacity: int = 5000) -> None:
        super().__init__()
        self.buffer: Deque[Dict[str, object]] = deque(maxlen=capacity)
        self._seq = 0
        self._lock = threading.Lock()
        self.subscribers: List[queue.Queue] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "seq": 0,
                "time": record.created,
                "level": record.levelname,
                "logger": record.name,
                "job": getattr(record, "job", None),
                "stage": getattr(record, "stage", None),
                "message": self.format(record),
            }
            with self._lock:
                self._seq += 1
                entry["seq"] = self._seq
                self.buffer.append(entry)
                subs = list(self.subscribers)
            for q in subs:
                try:
                    q.put_nowait(entry)
                except Exception:
                    pass
        except Exception:
            pass

    def since(self, seq: int) -> List[Dict[str, object]]:
        with self._lock:
            return [e for e in self.buffer if int(e["seq"]) > seq]


RING = RingBufferHandler()


class _ContextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        job = getattr(record, "job", None)
        stage = getattr(record, "stage", None)
        prefix = ""
        if job:
            prefix = f"[{job}"
            prefix += f"/{stage}] " if stage else "] "
        elif stage:
            prefix = f"[{stage}] "
        record.ctx = prefix
        return super().format(record)


def setup_logging(
    log_file: Optional[Path] = None,
    verbose: bool = False,
    quiet: bool = False,
    console: bool = True,
) -> logging.Logger:
    """Configure the package logger. Safe to call more than once."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    redaction = RedactingFilter()

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO))
        stream.setFormatter(_ContextFormatter("%(asctime)s %(levelname)-7s %(ctx)s%(message)s", "%H:%M:%S"))
        stream.addFilter(redaction)
        logger.addHandler(stream)

    if log_file:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            str(log_file), maxBytes=32 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        rotating.setLevel(logging.DEBUG)
        rotating.setFormatter(
            _ContextFormatter("%(asctime)s %(levelname)-7s %(name)s %(ctx)s%(message)s", "%Y-%m-%d %H:%M:%S")
        )
        rotating.addFilter(redaction)
        logger.addHandler(rotating)

    RING.setLevel(logging.DEBUG)
    RING.setFormatter(_ContextFormatter("%(ctx)s%(message)s"))
    RING.addFilter(redaction)
    logger.addHandler(RING)
    return logger


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)
