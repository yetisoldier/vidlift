"""CUDA selftest artifact shapes without vendor imports or native DLL loads."""
import json
import sys
import types

import numpy  # import before the synthetic Windows platform seam
import pytest

from vidlift import cuda_runtime as cr, transcribe, cli
from test_cuda_inventory_closure import pe


@pytest.fixture
def fake_vad(monkeypatch, tmp_path):
    monkeypatch.setenv("VIDLIFT_TRANSCRIPTION_ENGINE", "1")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "test.onnx").write_bytes(b"synthetic")
    package = types.ModuleType("faster_whisper")
    package.__path__ = []
    utils = types.ModuleType("faster_whisper.utils")
    setattr(utils, "get_assets_path", lambda: str(assets))
    vad = types.ModuleType("faster_whisper.vad")
    setattr(vad, "get_speech_timestamps", lambda *a, **kw: [])
    for key, value in (("faster_whisper", package), ("faster_whisper.utils", utils), ("faster_whisper.vad", vad)):
        monkeypatch.setitem(sys.modules, key, value)
    monkeypatch.setattr(transcribe, "vad_asset_report", lambda: {"vad_asset": "present", "assets_dir": str(assets)})


@pytest.mark.parametrize("shape,expected_ok,reason", [
    ("cpu-artifact", True, "not_bundled"),
    ("no-gpu", True, "no_device"),
    ("missing", False, "runtime_missing"),
    ("corrupt", False, "runtime_load_error"),
    ("available", True, None),
    ("fault", False, "check_failed"),
])
def test_selftest_and_cli_exit_semantics(fake_vad, monkeypatch, tmp_path, capsys, shape, expected_ok, reason):
    runtime = tmp_path / "ctranslate2"
    runtime.mkdir()
    manifest_path = cr._manifest_path()
    assert manifest_path is not None
    manifest = json.loads(manifest_path.read_text())
    for name in manifest["required_dlls"]:
        pe(runtime / name)
    original = cr.check_capability
    def check(**kwargs):
        if shape == "fault":
            raise RuntimeError("private path should not escape")
        return original(**kwargs, compute_types_fn=lambda _: {"float16"},
                        load_check_fn=lambda p: (shape != "corrupt", ""))
    if shape == "missing":
        (runtime / manifest["required_dlls"][0]).unlink()
    if shape == "no-gpu":
        check = lambda **kwargs: cr.Capability(False, "no_device", "CUDA_ERROR_NO_DEVICE (100)")
    monkeypatch.setattr(cr, "bundled_runtime_dir", lambda: None if shape == "cpu-artifact" else runtime)
    monkeypatch.setattr(cr, "check_capability", check)
    monkeypatch.setattr(sys, "platform", "win32")
    report = transcribe.selftest_report()
    assert report["ok"] == expected_ok
    assert report["cuda_runtime"]["reason_code"] == reason
    assert report["checks"]["vad_load_smoke"] == "ok"
    assert cli.main(["--selftest"]) == (0 if expected_ok else 1)
    text = capsys.readouterr().out
    payload = json.loads(text.removeprefix("vidlift-selftest "))
    assert payload["ok"] == expected_ok
    assert "Traceback" not in text and "private path" not in text
