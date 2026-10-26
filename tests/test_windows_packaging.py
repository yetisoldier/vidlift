"""Windows installer bundle discovery contracts."""

from __future__ import annotations

import os
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
    suffix = ".exe" if os.name == "nt" else ""
    app = _executable(tmp_path / f"VidLift{suffix}")
    ffmpeg = _executable(tmp_path / "tools" / f"ffmpeg{suffix}")
    ffprobe = _executable(tmp_path / "tools" / f"ffprobe{suffix}")
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
    suffix = ".exe" if os.name == "nt" else ""
    app = _executable(tmp_path / f"VidLift{suffix}")
    engine = _executable(
        tmp_path / "tools" / "realesrgan" / f"realesrgan-ncnn-vulkan{suffix}"
    )
    monkeypatch.setattr("vidlift.tools_realesrgan.sys.frozen", True, raising=False)
    monkeypatch.setattr("vidlift.tools_realesrgan.sys.executable", str(app))
    monkeypatch.delenv("VIDLIFT_REALESRGAN_NCNN", raising=False)

    assert resolve_realesrgan() == str(engine)


def test_frozen_app_finds_adjacent_ocr_tools(tmp_path, monkeypatch):
    suffix = ".exe" if os.name == "nt" else ""
    app = _executable(tmp_path / f"VidLift{suffix}")
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


# ---- b20: the installer workflow's model hash asserts (v0.4.8 D1) ----------------


_WORKFLOW = (
    Path(__file__).resolve().parent.parent
    / ".github" / "workflows" / "build-windows-installer.yml"
)

_ALL_MODEL_HASHES = (
    # x2plus (the v0.4.0 lane)
    "f26dc4b8d7c467a86fac91f63d361a762ef5ab13ccb0521d3081b1b3152e9a6f",
    "cfb044e3a1fe18750f54f7a3d8c2d49a296b54e2c9489c90513bbf88844e9e69",
    # anime (the v0.4.8 lane, design D1)
    "b88ff4f00ebf019a7fdac17fdd45a7fd3665d37509efc5baf2e4da2e24420a04",
    "548a36f9c3f4ab8da56cd3b13badf23968bee207b396dad14d04b830e5f2ab2d",
)


def test_b20_workflow_hash_asserts_cover_both_model_pairs():
    """b20 (design D1): the build workflow's pwsh hash-assert block names
    ALL FOUR pinned digests (x2plus param+bin, anime param+bin), so a
    substituted model never ships in the installer."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    stage_block = text.split("- name: Stage Real-ESRGAN")[1].split(
        "- name:"
    )[0]
    for digest in _ALL_MODEL_HASHES:
        assert digest in stage_block, (
            f"workflow's Real-ESRGAN staging hash asserts are missing {digest}"
        )
    # every digest is actually GUARDED (an -ne comparison), not just printed
    assert stage_block.count("-ne \"") >= 4


def test_b20_workflow_copies_anime_models_and_validates_installed_pair():
    """b20 (design D1): the workflow copies the vendored anime pair into
    the installer payload and the installed-artifact validation list
    asserts both files land under tools\\realesrgan\\models."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    stage_block = text.split("- name: Stage Real-ESRGAN")[1].split(
        "- name:"
    )[0]
    assert 'Copy-Item packaging\\models\\realesr-animevideov3-x2.param "$target\\models\\"' in stage_block
    assert 'Copy-Item packaging\\models\\realesr-animevideov3-x2.bin "$target\\models\\"' in stage_block
    validate_block = text.split("- name: Validate installed artifact")[1].split(
        "- name:"
    )[0]
    assert (
        '"$install\\tools\\realesrgan\\models\\realesr-animevideov3-x2.param"'
        in validate_block
    )
    assert (
        '"$install\\tools\\realesrgan\\models\\realesr-animevideov3-x2.bin"'
        in validate_block
    )


def test_b20_vendored_anime_artifacts_match_the_workflow_pins():
    """b20's real-artifact check: the vendored packaging/models/ bytes
    hash to EXACTLY the digests the workflow asserts (belt and braces
    against a stale workflow string)."""
    import hashlib

    models = Path(__file__).resolve().parent.parent / "packaging" / "models"
    pairs = {
        "realesr-animevideov3-x2.param": _ALL_MODEL_HASHES[2],
        "realesr-animevideov3-x2.bin": _ALL_MODEL_HASHES[3],
        "realesrgan-x2plus.param": _ALL_MODEL_HASHES[0],
        "realesrgan-x2plus.bin": _ALL_MODEL_HASHES[1],
    }
    for name, digest in pairs.items():
        artifact = models / name
        assert artifact.is_file(), f"vendored artifact missing: {name}"
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digest
