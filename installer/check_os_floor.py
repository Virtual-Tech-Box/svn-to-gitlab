"""Assert a built Windows binary will actually load on the oldest supported OS.

Setting `MinVersion` in the Inno Setup script only controls whether *setup* agrees
to run. It says nothing about the frozen executable, which carries its own floor in
the PE optional header: the Windows loader refuses to start an image whose
`MajorSubsystemVersion.MinorSubsystemVersion` is newer than the running OS.

That distinction matters here because the release is built on a much newer runner
than the oldest target. A build that quietly stamps 10.0.22000 would install
happily on Windows Server 2016 and then fail to start, which is the worst possible
place to discover it — on a client's server, mid-migration.

Usage:
    python installer/check_os_floor.py dist/svn2gitlab/svn2gitlab.exe
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Tuple

# Windows Server 2016 / Windows 10 1607. Nothing in this project needs newer.
MAX_ALLOWED = (10, 0)

_PE_SIGNATURE = b"PE\0\0"
_MAGIC_PE32 = 0x10B
_MAGIC_PE32_PLUS = 0x20B


class NotAPortableExecutable(Exception):
    pass


def read_os_floor(path: Path) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    """Return ((os_major, os_minor), (subsystem_major, subsystem_minor))."""
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise NotAPortableExecutable(f"{path} is not a Windows executable (no MZ header)")

    # e_lfanew at 0x3C points at the PE signature.
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset:pe_offset + 4] != _PE_SIGNATURE:
        raise NotAPortableExecutable(f"{path} has no PE signature at 0x{pe_offset:x}")

    # COFF header is 20 bytes; the optional header follows it.
    optional = pe_offset + 4 + 20
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic not in (_MAGIC_PE32, _MAGIC_PE32_PLUS):
        raise NotAPortableExecutable(f"unexpected optional-header magic 0x{magic:x}")

    # Both PE32 and PE32+ place these at the same offsets from the optional header:
    # MajorOperatingSystemVersion at +40, MajorSubsystemVersion at +48.
    os_major, os_minor = struct.unpack_from("<HH", data, optional + 40)
    sub_major, sub_minor = struct.unpack_from("<HH", data, optional + 48)
    return (os_major, os_minor), (sub_major, sub_minor)


def main(argv) -> int:
    if len(argv) < 2:
        print("usage: check_os_floor.py <path-to-exe>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    if not path.is_file():
        print(f"not found: {path}", file=sys.stderr)
        return 2

    try:
        os_version, subsystem = read_os_floor(path)
    except NotAPortableExecutable as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    print(f"{path.name}: required OS {os_version[0]}.{os_version[1]}, "
          f"subsystem {subsystem[0]}.{subsystem[1]}")

    # The subsystem version is the one the loader enforces.
    if subsystem > MAX_ALLOWED:
        print(
            f"\nERROR: this binary demands Windows {subsystem[0]}.{subsystem[1]} or newer,\n"
            f"so it will not start on Windows Server 2016 "
            f"({MAX_ALLOWED[0]}.{MAX_ALLOWED[1]}.14393).\n"
            f"Build the release on an older runner (windows-2022 rather than\n"
            f"windows-latest), or raise the documented minimum OS.",
            file=sys.stderr)
        return 1

    print(f"OK: starts on Windows Server 2016 ({MAX_ALLOWED[0]}.{MAX_ALLOWED[1]}) and later.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
