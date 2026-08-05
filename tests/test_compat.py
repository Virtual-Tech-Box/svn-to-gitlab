"""Guards for the oldest Python this project claims to support.

`requires-python = ">=3.9"`, but development happens on a much newer interpreter,
where syntax that is a hard `SyntaxError` on 3.9 compiles silently. The specific
trap that reached CI: an f-string expression containing a backslash was illegal
until 3.12 (PEP 701), and the starter-configuration template interpolates a Windows
path.

`ast.parse(..., feature_version=...)` does *not* catch that — it only gates grammar
additions — so these tests scan the source text directly.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIN_PYTHON = (3, 9)


def python_sources() -> List[Path]:
    roots = [PROJECT_ROOT / "src", PROJECT_ROOT / "tests"]
    files = [p for root in roots for p in sorted(root.rglob("*.py"))]
    entrypoint = PROJECT_ROOT / "installer" / "entrypoint.py"
    if entrypoint.is_file():
        files.append(entrypoint)
    return files


def fstring_expressions(source: str) -> Iterator[Tuple[int, str]]:
    """Yield `(lineno, expression_text)` for every `{...}` inside an f-string literal.

    Tokenising rather than parsing, because on Python 3.12+ the parser happily
    accepts what older versions reject, so the AST tells us nothing about 3.9.

    Two tokenisations have to be handled: before 3.12 an f-string arrives as a single
    STRING token whose text we walk ourselves; from 3.12 (PEP 701) it is split into
    FSTRING_START / expression tokens / FSTRING_END, and the expression parts are
    ordinary tokens. Getting this wrong makes the guard silently match nothing, which
    is how the original bug reached CI in the first place.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return

    if hasattr(tokenize, "FSTRING_START"):
        yield from _scan_pep701(tokens)
    else:
        yield from _scan_single_token(tokens)


def _scan_pep701(tokens) -> Iterator[Tuple[int, str]]:
    """Python 3.12+: expression parts are real tokens between FSTRING_START/END."""
    fstring_depth = 0
    brace_depth = 0
    buffer: List[str] = []
    start_line = 0

    for token in tokens:
        if token.type == tokenize.FSTRING_START:
            fstring_depth += 1
            continue
        if token.type == tokenize.FSTRING_END:
            fstring_depth = max(0, fstring_depth - 1)
            continue
        if not fstring_depth:
            continue

        if token.type == tokenize.OP and token.string == "{":
            brace_depth += 1
            if brace_depth == 1:
                buffer, start_line = [], token.start[0]
                continue
        elif token.type == tokenize.OP and token.string == "}":
            if brace_depth == 1:
                brace_depth = 0
                yield start_line, "".join(buffer)
                continue
            brace_depth = max(0, brace_depth - 1)

        if brace_depth >= 1:
            buffer.append(token.string)


def _scan_single_token(tokens) -> Iterator[Tuple[int, str]]:
    """Python 3.11 and earlier: the whole f-string is one STRING token."""
    for token in tokens:
        if token.type != tokenize.STRING:
            continue
        prefix = token.string[: len(token.string) - len(token.string.lstrip("rbufRBUF"))]
        if "f" not in prefix.lower():
            continue
        text = token.string
        index, depth, start = 0, 0, 0
        while index < len(text):
            char = text[index]
            if char == "{":
                if index + 1 < len(text) and text[index + 1] == "{" and depth == 0:
                    index += 2
                    continue
                if depth == 0:
                    start = index + 1
                depth += 1
            elif char == "}":
                if depth == 0 and index + 1 < len(text) and text[index + 1] == "}":
                    index += 2
                    continue
                if depth > 0:
                    depth -= 1
                    if depth == 0:
                        yield token.start[0], text[start:index]
            index += 1


@pytest.mark.parametrize("path", python_sources(), ids=lambda p: str(p.relative_to(PROJECT_ROOT)))
def test_no_backslash_inside_fstring_expressions(path: Path) -> None:
    """Illegal before Python 3.12, and this project supports 3.9."""
    source = path.read_text(encoding="utf-8")
    offenders = [(line, expr) for line, expr in fstring_expressions(source) if "\\" in expr]
    assert not offenders, (
        f"{path.relative_to(PROJECT_ROOT)} has a backslash inside an f-string expression, "
        f"which is a SyntaxError on Python < 3.12: "
        + "; ".join(f"line {line}: {{{expr}}}" for line, expr in offenders)
        + ". Compute the value into a variable before the f-string."
    )


def test_the_guard_actually_detects_the_bug() -> None:
    """A guard that cannot fire is worse than no guard."""
    bad = 'x = f"{1 if a else r\'C:\\Repo\'}"'
    assert any("\\" in expr for _line, expr in fstring_expressions(bad))

    good = 'path = r"C:\\Repo"\nx = f"{path}"'
    assert not any("\\" in expr for _line, expr in fstring_expressions(good))

    # Literal text outside the braces may contain backslashes on any version.
    literal = 'x = f"DOMAIN\\\\user {value}"'
    assert not any("\\" in expr for _line, expr in fstring_expressions(literal))


@pytest.mark.parametrize("path", python_sources(), ids=lambda p: str(p.relative_to(PROJECT_ROOT)))
def test_sources_parse_on_the_minimum_supported_grammar(path: Path) -> None:
    """Catches grammar added after 3.9 — match statements, PEP 604 unions at runtime."""
    source = path.read_text(encoding="utf-8")
    try:
        ast.parse(source, filename=str(path), feature_version=MIN_PYTHON)
    except SyntaxError as exc:
        pytest.fail(f"{path.relative_to(PROJECT_ROOT)}:{exc.lineno} uses syntax newer than "
                    f"Python {'.'.join(map(str, MIN_PYTHON))}: {exc.msg}")


def test_declared_minimum_matches_this_guard() -> None:
    """If someone raises requires-python, these guards must move with it."""
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'requires-python\s*=\s*"[>=~^]*\s*(\d+)\.(\d+)"', pyproject)
    assert match, "could not read requires-python from pyproject.toml"
    declared = (int(match.group(1)), int(match.group(2)))
    assert declared == MIN_PYTHON, (
        f"pyproject declares Python {declared[0]}.{declared[1]} but tests/test_compat.py "
        f"guards {MIN_PYTHON[0]}.{MIN_PYTHON[1]}. Update MIN_PYTHON."
    )
