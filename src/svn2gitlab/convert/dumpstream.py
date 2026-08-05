"""Streaming parser for the Subversion dump format.

This is the input side of the native conversion engine — the thing that lets us
convert without `git svn`, and therefore without Perl.

Why this is tractable at all: `svnadmin dump` emits **fulltext** node content by
default. Only `svnrdump` and `svnadmin dump --deltas` produce svndiff-compressed
deltas, and decoding those would be a project in itself. The acquisition stage
already lands a local repository for every source (hotcopy, svnsync or
svnrdump-into-a-local-repo), so we can always take a fulltext dump from local disk
and never meet a delta. If one turns up anyway we say so plainly rather than
producing subtly wrong history.

Format reference: subversion/include/svn_repos.h and `notes/dump-load-format.txt`.
A record is a block of `Header: value` lines, a blank line, then exactly
`Content-length` bytes of body: property block first, then text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import BinaryIO, Dict, Iterator, List, Optional, Tuple

from ..errors import ConversionError
from ..logging_setup import get_logger

log = get_logger("dump")

PROPS_END = b"PROPS-END\n"

# Node actions we understand.
ACTION_ADD = "add"
ACTION_DELETE = "delete"
ACTION_CHANGE = "change"
ACTION_REPLACE = "replace"

_HEADER = re.compile(rb"^([A-Za-z][A-Za-z0-9-]*):\s*(.*)$")


@dataclass
class Node:
    """One path changed in one revision."""

    path: str
    action: str
    kind: str = ""                       # "file" | "dir" | "" (absent on delete)
    copyfrom_rev: Optional[int] = None
    copyfrom_path: Optional[str] = None
    properties: Dict[str, bytes] = field(default_factory=dict)
    deleted_properties: List[str] = field(default_factory=list)
    has_properties: bool = False         # whether the record carried a prop block
    content: Optional[bytes] = None      # None when the record carried no text
    content_sha1: str = ""

    @property
    def is_dir(self) -> bool:
        return self.kind == "dir"

    @property
    def is_copy(self) -> bool:
        return self.copyfrom_rev is not None and self.copyfrom_path is not None

    @property
    def is_executable(self) -> bool:
        return "svn:executable" in self.properties

    @property
    def is_symlink(self) -> bool:
        return self.properties.get("svn:special") is not None

    def mode(self) -> str:
        if self.is_symlink:
            return "120000"
        return "100755" if self.is_executable else "100644"


@dataclass
class Revision:
    number: int
    properties: Dict[str, bytes] = field(default_factory=dict)
    nodes: List[Node] = field(default_factory=list)

    def _prop(self, name: str) -> str:
        raw = self.properties.get(name)
        return raw.decode("utf-8", "replace") if raw is not None else ""

    @property
    def author(self) -> str:
        return self._prop("svn:author")

    @property
    def date(self) -> str:
        return self._prop("svn:date")

    @property
    def message(self) -> str:
        return self._prop("svn:log")


def _parse_properties(block: bytes) -> Tuple[Dict[str, bytes], List[str]]:
    """Parse a `K/V` property block terminated by `PROPS-END`.

    Incremental dumps also carry `D <len>` entries for properties deleted in this
    revision, which matter for things like clearing `svn:executable`.
    """
    props: Dict[str, bytes] = {}
    deleted: List[str] = []
    offset = 0
    length = len(block)

    while offset < length:
        if block.startswith(b"PROPS-END", offset):
            break
        newline = block.find(b"\n", offset)
        if newline < 0:
            break
        line = block[offset:newline]
        offset = newline + 1

        if not line:
            continue
        marker, _, size_text = line.partition(b" ")
        try:
            size = int(size_text)
        except ValueError:
            raise ConversionError(
                f"malformed property block near {line[:40]!r}",
                "The dump file appears to be corrupt. Re-create it with "
                "`svnadmin dump`, or fall back to the git-svn engine with "
                "`convert.engine: git-svn`.",
            )

        name = block[offset:offset + size].decode("utf-8", "surrogateescape")
        offset += size + 1  # value/name plus its newline

        if marker == b"D":
            deleted.append(name)
            continue
        if marker != b"K":
            continue

        # The value follows as `V <len>\n<bytes>\n`.
        newline = block.find(b"\n", offset)
        if newline < 0:
            break
        value_line = block[offset:newline]
        offset = newline + 1
        _, _, value_size_text = value_line.partition(b" ")
        try:
            value_size = int(value_size_text)
        except ValueError:
            raise ConversionError(f"malformed property value for {name!r}")
        props[name] = block[offset:offset + value_size]
        offset += value_size + 1

    return props, deleted


class DumpReader:
    """Iterate a dump stream revision by revision, holding one revision in memory."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.uuid = ""
        self.format_version = 0
        self._pending: Optional[Dict[str, str]] = None

    # -- low-level ------------------------------------------------------------

    def _read_headers(self) -> Optional[Dict[str, str]]:
        """Read a header block, skipping blank lines. None at end of stream."""
        if self._pending is not None:
            headers, self._pending = self._pending, None
            return headers

        headers: Dict[str, str] = {}
        while True:
            line = self.stream.readline()
            if not line:
                return headers or None
            stripped = line.strip()
            if not stripped:
                if headers:
                    return headers      # blank line terminates a header block
                continue                # leading blank lines between records
            match = _HEADER.match(stripped)
            if not match:
                # Not a header: content we were not expecting. Treat as end of block.
                if headers:
                    return headers
                continue
            headers[match.group(1).decode("ascii")] = match.group(2).decode(
                "utf-8", "surrogateescape")

    def _read_exact(self, count: int) -> bytes:
        chunks: List[bytes] = []
        remaining = count
        while remaining > 0:
            chunk = self.stream.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != count:
            raise ConversionError(
                f"dump stream ended early: expected {count} bytes, got {len(data)}",
                "The dump is truncated. Re-create it, and check for a full disk or an "
                "interrupted `svnadmin dump`.",
            )
        return data

    # -- iteration ------------------------------------------------------------

    def __iter__(self) -> Iterator[Revision]:
        return self.revisions()

    def revisions(self) -> Iterator[Revision]:
        current: Optional[Revision] = None

        while True:
            headers = self._read_headers()
            if headers is None:
                break

            if "SVN-fs-dump-format-version" in headers:
                self.format_version = int(headers["SVN-fs-dump-format-version"])
                if self.format_version > 3:
                    raise ConversionError(
                        f"unsupported dump format version {self.format_version}",
                        "This build understands versions 1-3. Use "
                        "`convert.engine: git-svn` for anything newer.",
                    )
                continue

            if "UUID" in headers:
                self.uuid = headers["UUID"]
                continue

            if "Revision-number" in headers:
                if current is not None:
                    yield current
                current = Revision(number=int(headers["Revision-number"]))
                prop_length = int(headers.get("Prop-content-length", 0))
                content_length = int(headers.get("Content-length", prop_length))
                body = self._read_exact(content_length)
                if prop_length:
                    current.properties, _ = _parse_properties(body[:prop_length])
                continue

            if "Node-path" in headers:
                if current is None:
                    raise ConversionError("dump contains a node before any revision")
                current.nodes.append(self._read_node(headers))
                continue

            # Anything else is a record type we do not model; skip its body.
            content_length = int(headers.get("Content-length", 0))
            if content_length:
                self._read_exact(content_length)

        if current is not None:
            yield current

    def _read_node(self, headers: Dict[str, str]) -> Node:
        if headers.get("Text-delta", "").lower() == "true":
            raise ConversionError(
                f"node {headers.get('Node-path')!r} carries a text delta, which the "
                "native engine cannot apply",
                "Produce the dump with `svnadmin dump` (fulltext) rather than "
                "`svnrdump dump` or `--deltas`. The acquisition stage does this "
                "automatically; if you supplied a dump by hand, re-create it. "
                "Alternatively set `convert.engine: git-svn`.",
            )

        node = Node(
            path=headers["Node-path"].strip("/"),
            action=headers.get("Node-action", "").lower(),
            kind=headers.get("Node-kind", "").lower(),
            content_sha1=headers.get("Text-content-sha1", ""),
        )
        if "Node-copyfrom-rev" in headers:
            node.copyfrom_rev = int(headers["Node-copyfrom-rev"])
            node.copyfrom_path = headers.get("Node-copyfrom-path", "").strip("/")

        prop_length = int(headers.get("Prop-content-length", 0))
        text_length = int(headers.get("Text-content-length", 0))
        content_length = int(headers.get("Content-length", prop_length + text_length))

        body = self._read_exact(content_length) if content_length else b""
        if prop_length:
            node.has_properties = True
            node.properties, node.deleted_properties = _parse_properties(body[:prop_length])
        if "Text-content-length" in headers:
            node.content = body[prop_length:prop_length + text_length]
        return node


def scan_copy_sources(stream: BinaryIO) -> Dict[int, List[str]]:
    """First pass: which revisions are used as copy sources, and from which paths.

    Cheap enough to run over the whole dump, and it lets the converter know in
    advance which historical states it will be asked about.
    """
    sources: Dict[int, List[str]] = {}
    reader = DumpReader(stream)
    for revision in reader.revisions():
        for node in revision.nodes:
            if node.is_copy and node.copyfrom_rev is not None:
                sources.setdefault(node.copyfrom_rev, []).append(node.copyfrom_path or "")
    return sources


def svn_date_to_git(date: str) -> str:
    """`2019-04-11T08:12:33.123456Z` -> a git-friendly `<unix ts> +0000`."""
    from datetime import datetime, timezone

    if not date:
        return "0 +0000"
    text = date.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return f"{int(parsed.timestamp())} +0000"
        except ValueError:
            continue
    return "0 +0000"
