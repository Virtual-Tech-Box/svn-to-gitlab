# PyInstaller spec for svn2gitlab.
#
# Produces a single self-contained directory (not a one-file exe): a one-file build
# unpacks itself to %TEMP% on every launch, which on a locked-down Windows Server is
# both slow and frequently blocked by AppLocker or antivirus policy. A directory
# build starts instantly and is what the Inno Setup installer packages.
#
# Build:  pyinstaller installer/svn2gitlab.spec --noconfirm --clean

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
PROJECT = SPEC_DIR.parent
SRC = PROJECT / "src"

block_cipher = None

datas = [
    (str(SRC / "svn2gitlab" / "web" / "templates"), "svn2gitlab/web/templates"),
]

# Optional: a `vendor/` directory next to this spec is copied into the bundle so the
# installer can ship Git and Subversion alongside the exe for offline installs. The
# tool discovery code already looks in <bundle>/tools/{git,svn}.
vendor = SPEC_DIR / "vendor"
if vendor.is_dir():
    datas.append((str(vendor), "tools"))

hiddenimports = [
    # uvicorn resolves its protocol implementations by name at runtime, so the
    # analyser cannot see them.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # pydantic v2 builds validators dynamically.
    *collect_submodules("pydantic"),
    "pydantic.deprecated.decorator",
    "anyio._backends._asyncio",
    "sqlite3",
    "encodings.idna",
]

excludes = [
    "tkinter", "matplotlib", "numpy", "pandas", "scipy", "PIL",
    "pytest", "_pytest", "setuptools", "pip", "wheel",
]

a = Analysis(
    # Not svn2gitlab/__main__.py: PyInstaller runs the entry script as a top-level
    # module with no package context, so its relative imports fail.
    [str(SPEC_DIR / "entrypoint.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="svn2gitlab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX-packed binaries are a common antivirus false positive
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(SPEC_DIR / "svn2gitlab.ico") if (SPEC_DIR / "svn2gitlab.ico").is_file() else None,
    version=str(SPEC_DIR / "version_info.txt") if (SPEC_DIR / "version_info.txt").is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="svn2gitlab",
)
