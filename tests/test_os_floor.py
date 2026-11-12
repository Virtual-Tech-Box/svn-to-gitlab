"""The PE floor checker that guards Windows Server 2016 support.

The check only means something if it can fail, and it is parsing binary offsets by
hand, so both the parsing and the verdict are exercised against synthetic images
here. CI then runs it against the real frozen executable.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

from check_os_floor import (MAX_ALLOWED, NotAPortableExecutable,  # noqa: E402
                            main, read_os_floor)


def make_pe(os_version=(6, 0), subsystem=(6, 0), plus: bool = True) -> bytes:
    """A minimal image with just enough structure for the fields we read."""
    pe_offset = 0x80
    data = bytearray(b"\0" * 0x200)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe_offset)
    data[pe_offset:pe_offset + 4] = b"PE\0\0"

    optional = pe_offset + 4 + 20
    struct.pack_into("<H", data, optional, 0x20B if plus else 0x10B)
    struct.pack_into("<HH", data, optional + 40, *os_version)
    struct.pack_into("<HH", data, optional + 48, *subsystem)
    return bytes(data)


def write(tmp_path, content: bytes, name: str = "app.exe") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_reads_versions_from_a_pe32_plus_image(tmp_path):
    path = write(tmp_path, make_pe(os_version=(6, 0), subsystem=(6, 0)))
    assert read_os_floor(path) == ((6, 0), (6, 0))


def test_reads_versions_from_a_pe32_image(tmp_path):
    path = write(tmp_path, make_pe(os_version=(10, 0), subsystem=(10, 0), plus=False))
    assert read_os_floor(path) == ((10, 0), (10, 0))


def test_a_server_2016_compatible_binary_passes(tmp_path):
    path = write(tmp_path, make_pe(subsystem=MAX_ALLOWED))
    assert main(["check_os_floor.py", str(path)]) == 0


def test_an_older_floor_also_passes(tmp_path):
    """6.0 (Vista era) is older than our floor and therefore fine."""
    path = write(tmp_path, make_pe(subsystem=(6, 0)))
    assert main(["check_os_floor.py", str(path)]) == 0


def test_a_binary_demanding_windows_11_is_rejected(tmp_path):
    """The failure this guard exists for: installs, then will not start."""
    path = write(tmp_path, make_pe(subsystem=(10, 1)))
    assert main(["check_os_floor.py", str(path)]) == 1


def test_a_much_newer_floor_is_rejected(tmp_path):
    path = write(tmp_path, make_pe(subsystem=(11, 0)))
    assert main(["check_os_floor.py", str(path)]) == 1


def test_a_non_windows_binary_is_reported_clearly(tmp_path):
    path = write(tmp_path, b"\x7fELF" + b"\0" * 256)
    with pytest.raises(NotAPortableExecutable, match="not a Windows executable"):
        read_os_floor(path)


def test_a_missing_pe_signature_is_reported(tmp_path):
    broken = bytearray(make_pe())
    broken[0x80:0x84] = b"XXXX"
    path = write(tmp_path, bytes(broken))
    with pytest.raises(NotAPortableExecutable, match="no PE signature"):
        read_os_floor(path)


def test_missing_file_exits_two(tmp_path):
    assert main(["check_os_floor.py", str(tmp_path / "nope.exe")]) == 2
