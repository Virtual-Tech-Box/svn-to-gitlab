"""Commit-message rewriting via the git fast-import stream.

`git filter-branch` would take hours on a large history and is deprecated;
`git-filter-repo` is an extra dependency we cannot assume on a locked-down Windows
Server. Instead we stream `git fast-export --no-data` through a small parser and
back into `git fast-import` in the *same* repository. `--no-data` means blobs are
referenced by SHA rather than re-serialised, so a 20 GB repository rewrites in
minutes and no file content is ever copied.

The rewrite is deterministic: identical input produces identical commit SHAs. That
is what allows the export repository to be re-derived on every incremental sync and
still fast-forward on push instead of forcing.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import IO, Callable, Optional

from ..logging_setup import get_logger
from ..procs import pipe
from .git import Git

log = get_logger("filter")

_SVN_ID_LINE = re.compile(rb"^git-svn-id:\s*(?P<url>\S+)@(?P<rev>\d+)\s+(?P<uuid>\S+)\s*$", re.M)


@dataclass
class FilterStats:
    commits: int = 0
    tags: int = 0
    messages_rewritten: int = 0
    trailers_added: int = 0

    def to_dict(self) -> dict:
        return {
            "commits": self.commits,
            "tags": self.tags,
            "messages_rewritten": self.messages_rewritten,
            "trailers_added": self.trailers_added,
        }


def rewrite_message(raw: bytes, strip_svn_id: bool, revision_trailer: bool,
                    stats: FilterStats) -> bytes:
    """Remove the `git-svn-id:` trailer, optionally replacing it with `Svn-Revision:`."""
    match = _SVN_ID_LINE.search(raw)
    if not match:
        return raw
    revision = match.group("rev")
    if not strip_svn_id:
        return raw

    cleaned = _SVN_ID_LINE.sub(b"", raw)
    cleaned = cleaned.rstrip(b"\r\n \t")
    stats.messages_rewritten += 1

    if revision_trailer:
        # Blank line before a trailer block, unless the message is now empty.
        if cleaned:
            cleaned += b"\n\n"
        cleaned += b"Svn-Revision: " + revision
        stats.trailers_added += 1
    if cleaned:
        cleaned += b"\n"
    else:
        # fast-import rejects a zero-length message on some git versions.
        cleaned = b"(no message)\n"
    return cleaned


class _StreamFilter:
    """Line-oriented fast-import stream rewriter.

    The stream is mostly newline-delimited commands, with `data <n>` introducing an
    exactly-n-byte payload that must be consumed as raw bytes - not lines. Only the
    payloads that follow a `commit` or `tag` header are messages we may touch.
    """

    def __init__(self, strip_svn_id: bool, revision_trailer: bool,
                 on_commit: Optional[Callable[[int], None]] = None) -> None:
        self.strip_svn_id = strip_svn_id
        self.revision_trailer = revision_trailer
        self.on_commit = on_commit
        self.stats = FilterStats()

    def run(self, raw_source: IO[bytes], raw_sink: IO[bytes]) -> None:
        # Popen gives us unbuffered raw streams; readline() on those reads one byte at
        # a time, which turns a 5-minute rewrite into an overnight job.
        source: IO[bytes] = io.BufferedReader(raw_source, buffer_size=1 << 20)  # type: ignore[arg-type]
        sink: IO[bytes] = io.BufferedWriter(raw_sink, buffer_size=1 << 20)      # type: ignore[arg-type]
        try:
            self._run(source, sink)
        finally:
            try:
                sink.flush()
                # detach() releases the raw stream instead of closing it: closing the
                # buffer would close the caller's pipe (and, in tests, the BytesIO we
                # are about to read back). procs.pipe() owns closing the real handle.
                sink.detach()
                source.detach()
            except Exception:
                pass

    def _run(self, source: IO[bytes], sink: IO[bytes]) -> None:
        context = ""   # "commit" | "tag" | "blob" | ""
        while True:
            line = _readline(source)
            if not line:
                break

            if line.startswith(b"commit "):
                context = "commit"
                self.stats.commits += 1
                if self.on_commit and self.stats.commits % 1000 == 0:
                    self.on_commit(self.stats.commits)
            elif line.startswith(b"tag "):
                context = "tag"
                self.stats.tags += 1
            elif line.startswith(b"blob"):
                context = "blob"

            if line.startswith(b"data "):
                payload = self._read_payload(source, line)
                if context in ("commit", "tag") and payload is not None:
                    payload = rewrite_message(payload, self.strip_svn_id,
                                              self.revision_trailer, self.stats)
                    sink.write(b"data %d\n" % len(payload))
                    sink.write(payload)
                    # After the message, a commit body continues with from/merge/filemodify.
                    context = ""
                    continue
                sink.write(line)
                if payload is not None:
                    sink.write(payload)
                context = ""
                continue

            sink.write(line)

    @staticmethod
    def _read_payload(source: IO[bytes], data_line: bytes) -> Optional[bytes]:
        spec = data_line[len(b"data "):].strip()
        if spec.startswith(b"<<"):
            # Delimited form: read until the delimiter line. git fast-export does not
            # emit this, but the format allows it and a hand-made stream might.
            delimiter = spec[2:]
            chunks = []
            while True:
                line = _readline(source)
                if not line or line.rstrip(b"\n") == delimiter:
                    break
                chunks.append(line)
            return b"".join(chunks)
        try:
            length = int(spec)
        except ValueError:
            return None
        return _read_exactly(source, length)


def _readline(stream: IO[bytes]) -> bytes:
    return stream.readline()


def _read_exactly(stream: IO[bytes], length: int) -> bytes:
    chunks = []
    remaining = length
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def strip_svn_metadata(
    git: Git,
    strip_svn_id: bool = True,
    revision_trailer: bool = True,
    cancel: Optional[Event] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> FilterStats:
    """Rewrite every commit message in the repository in place.

    Operates on `--all`, so branches and tags are rewritten consistently and tags
    keep pointing at their (new) commits.
    """
    if not strip_svn_id:
        return FilterStats()

    total = git.commit_count("--all")
    filt = _StreamFilter(
        strip_svn_id=strip_svn_id,
        revision_trailer=revision_trailer,
        on_commit=(lambda n: on_progress(min(0.99, n / total), f"rewrote {n:,} of {total:,} commits"))
        if (on_progress and total) else None,
    )

    export_cmd = [
        git.exe, "fast-export", "--all", "--no-data",
        "--signed-tags=strip", "--tag-of-filtered-object=rewrite",
        "--reencode=yes", "--use-done-feature",
    ]
    import_cmd = [git.exe, "fast-import", "--force", "--quiet", "--done"]

    log.info("rewriting commit messages for %d commit(s)", total)
    pipe(export_cmd, import_cmd, cwd=git.repo, env=git._env(), cancel=cancel,
         transform=filt.run)

    # fast-import rewrote the refs underneath the working tree; realign index and HEAD.
    git.run(["update-ref", "-d", "refs/notes/fast-import"], check=False)
    if (git.repo / ".git").is_dir():
        git.run(["reset", "--hard", "HEAD"], check=False, timeout=None)
    log.info("rewrote %d message(s) across %d commit(s) and %d annotated tag(s)",
             filt.stats.messages_rewritten, filt.stats.commits, filt.stats.tags)
    if on_progress:
        on_progress(1.0, f"rewrote {filt.stats.messages_rewritten:,} commit messages")
    return filt.stats


def verify_no_svn_metadata(git: Git, sample: int = 500) -> int:
    """Count commits that still carry a `git-svn-id` trailer (should be zero)."""
    result = git.run(["log", "--all", f"--max-count={sample}", "--format=%B"], check=False)
    return len(_SVN_ID_LINE.findall(result.stdout.encode("utf-8", "replace")))
