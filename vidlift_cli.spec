"""PyInstaller single-file Windows CLI build."""

from pathlib import Path

icon = Path("packaging/app_icon.ico")

a = Analysis(
    ["run_cli.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=[
        "vidlift",
        "vidlift.cli",
        "vidlift.benchmark",
        "vidlift.engine_ncnn",
        "vidlift.mcp_server",
        "vidlift.mcp_bridge.server",
        "vidlift.subtitles",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="vidlift-cli",
    console=True,
    icon=str(icon) if icon.exists() else None,
    version="packaging/version_info.txt",
)
