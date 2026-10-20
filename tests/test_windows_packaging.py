"""Windows installer bundle discovery contracts."""

from __future__ import annotations

import stat
from pathlib import Path

import vidlift.run as run_module
import vidlift.subtitles as subtitle_module
from vidlift.run import find_tools
from vidlift.tools_realesrgan import _engine_binary_names, resolve_realesrgan


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_frozen_app_finds_adjacent_ffmpeg_and_ffprobe(tmp_path, monkeypatch):
    app = _executable(tmp_path / "VidLift")
    ffmpeg = _executable(tmp_path / "tools" / "ffmpeg")
    ffprobe = _executable(tmp_path / "tools" / "ffprobe")
    monkeypatch.setattr(run_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(run_module.sys, "executable", str(app))

    tools = find_tools()

    assert tools.ffmpeg == str(ffmpeg)
    assert tools.ffprobe == str(ffprobe)


def test_windows_engine_name_prefers_exe():
    assert _engine_binary_names("nt") == [
        "realesrgan-ncnn-vulkan.exe",
        "realesrgan-ncnn-vulkan",
    ]


def test_frozen_app_finds_adjacent_realesrgan(tmp_path, monkeypatch):
    app = _executable(tmp_path / "VidLift")
    engine = _executable(
        tmp_path / "tools" / "realesrgan" / "realesrgan-ncnn-vulkan"
    )
    monkeypatch.setattr("vidlift.tools_realesrgan.sys.frozen", True, raising=False)
    monkeypatch.setattr("vidlift.tools_realesrgan.sys.executable", str(app))
    monkeypatch.delenv("VIDLIFT_REALESRGAN_NCNN", raising=False)

    assert resolve_realesrgan() == str(engine)


def test_frozen_app_finds_adjacent_ocr_tools(tmp_path, monkeypatch):
    app = _executable(tmp_path / "VidLift")
    seconv = _executable(tmp_path / "tools" / "SubtitleEdit" / "seconv.exe")
    tesseract = _executable(
        tmp_path
        / "tools"
        / "SubtitleEdit"
        / "Tesseract"
        / "tesseract.exe"
    )
    monkeypatch.setattr(subtitle_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(subtitle_module.sys, "executable", str(app))

    assert subtitle_module.resolve_seconv() == str(seconv)
    assert subtitle_module.resolve_tesseract() == str(tesseract)
