# PyInstaller spec for TeamLooker — builds a single self-contained executable.
# Build from the repo root:  pyinstaller packaging/teamlooker.spec --noconfirm
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.dirname(SPECPATH)  # repo root (packaging/..)

datas = [(os.path.join(ROOT, "teamlooker", "ui", "static"), "teamlooker/ui/static")]
hiddenimports = collect_submodules("pynput") + collect_submodules("aiohttp")

a = Analysis(
    [os.path.join(ROOT, "teamlooker", "__main__.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "_pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TeamLooker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    icon=os.path.join(ROOT, "assets", "teamlooker.ico"),
)
