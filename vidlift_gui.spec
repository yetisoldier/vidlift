"""PyInstaller one-folder Windows GUI build."""

from pathlib import Path

icon = Path("packaging/app_icon.ico")

a = Analysis(
    ["run_gui.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=["vidlift", "vidlift.gui", "vidlift.plan", "vidlift.probe"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VidLift",
    console=False,
    icon=str(icon) if icon.exists() else None,
    version="packaging/version_info.txt",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="VidLift",
)
