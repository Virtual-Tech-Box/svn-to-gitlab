"""Entry point for the frozen (PyInstaller) build.

`svn2gitlab/__main__.py` uses a relative import, which is correct for
`python -m svn2gitlab` but fails when PyInstaller runs the file as a top-level
script with no parent package. This module is the frozen build's script instead and
imports absolutely.

It also enables multiprocessing freeze support: without it, any library that spawns
a worker process would re-run the whole CLI in the child on Windows.
"""

from __future__ import annotations

import multiprocessing
import sys


def _run() -> None:
    from svn2gitlab.cli import main
    main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        _run()
    except KeyboardInterrupt:
        sys.exit(130)
