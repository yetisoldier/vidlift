"""PyInstaller single-file Windows CLI build."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

icon = Path("packaging/app_icon.ico")

a = Analysis(
    ["run_cli.py"],
    pathex=["."],
    binaries=[],
    # v0.4.16.2 (ADR-1/2): faster-whisper resolves its VAD model as
    # <dirname(utils.__file__)>/assets/*.onnx at RUNTIME; under PyInstaller that
    # path is _internal\faster_whisper\assets — the exact destination this
    # collector populates. Whole-package collect: rename- and relocation-immune
    # against upstream asset changes (the leaf is version-suffixed upstream).
    # tests/test_v04162_vad_packaging.py pins this line's presence in BOTH specs.
    datas=collect_data_files("faster_whisper"),
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
