# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from importlib.util import find_spec

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

repo_root = Path(SPECPATH).parents[1]
optional_hiddenimports = [
    "aiosqlite",
    "httpx",
    "websockets",
    "pydantic",
    "pydantic_settings",
    "structlog",
]
hiddenimports = sorted(
    {
        *collect_submodules("build_engine"),
        *(name for name in optional_hiddenimports if find_spec(name) is not None),
    }
)
datas = []
for package in ("tzdata",):
    package_spec = find_spec(package)
    if package_spec is not None and package_spec.submodule_search_locations is not None:
        datas += collect_data_files(package)

a = Analysis(
    [str(repo_root / "src" / "build_engine" / "__main__.py")],
    pathex=[str(repo_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="build-engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
