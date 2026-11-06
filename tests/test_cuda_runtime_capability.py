"""Runtime inventory uses the actual manifest and packaged destinations."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

from vidlift import cuda_runtime as cr


def test_manifest_required_dlls_is_not_silently_empty():
    path = cr._manifest_path()
    assert path is not None
    manifest = json.loads(path.read_text())
    assert cr._required_dlls(manifest) == manifest["required_dlls"]
    with pytest.raises(ValueError):
        cr._required_dlls({"required": ["wrong.dll"]})


@pytest.mark.parametrize("shape", ["onefile", "onedir"])
def test_frozen_runtime_location_and_missing_inventory(monkeypatch, tmp_path, shape):
    bundle = tmp_path if shape == "onefile" else tmp_path / "_internal"
    (bundle / "packaging").mkdir(parents=True)
    manifest_path = cr._manifest_path()
    assert manifest_path is not None
    manifest = json.loads(manifest_path.read_text())
    (bundle / "packaging/cuda_runtime_manifest.json").write_text(json.dumps(manifest))
    # Frozen intent is archived by the runtime hook, not inferred from DLLs.
    identity = {
        "identity_schema": 1, "artifact_kind": "cli-onefile" if shape == "onefile" else "gui-onedir",
        "application_version": "test", "build_reference": "synthetic",
        "cuda_bundled": True, "inventory": {},
    }
    notice = bundle / "licenses/synthetic.txt"
    notice.parent.mkdir()
    notice.write_bytes(b"synthetic test notice, not vendor terms")
    inventory = identity["inventory"]
    for path in (notice, bundle / "packaging/cuda_runtime_manifest.json"):
        inventory[path.relative_to(bundle).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in manifest["required_dlls"]:
        inventory["ctranslate2/" + name] = hashlib.sha256(b"synthetic DLL placeholder, no native load").hexdigest()
    monkeypatch.setattr(sys, "_vidlift_build_identity", identity, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "VidLift.exe"))
    monkeypatch.setattr(sys, "platform", "win32")
    assert cr.bundled_runtime_dir() == bundle / "ctranslate2"
    calls = []
    cap = cr.check_capability(compute_types_fn=lambda _: calls.append("probe") or {"float16"})
    assert cap.reason_code == "runtime_missing"
    assert not calls
    runtime = bundle / "ctranslate2"
    runtime.mkdir()
    for name in manifest["required_dlls"]:
        (runtime / name).write_bytes(b"synthetic DLL placeholder, no native load")
    cap = cr.check_capability(compute_types_fn=lambda _: {"float16"},
                              load_check_fn=lambda p: (calls.append(p.name) is None, ""))
    assert cap.available
    assert calls == manifest["required_dlls"]


def test_non_windows_does_not_load_dlls(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    cap = cr.check_capability(dll_dir=tmp_path,
        compute_types_fn=lambda _: [], load_check_fn=lambda p: pytest.fail("DLL load on Linux"))
    assert cap.reason_code == "no_supported_compute_type"


def test_load_and_probe_errors_never_leak_native_payload(monkeypatch, tmp_path):
    manifest = cr._manifest_path()
    assert manifest is not None
    required = cr._required_dlls(json.loads(manifest.read_text()))
    for name in required:
        (tmp_path / name).write_bytes(b"fake")
    monkeypatch.setattr(sys, "platform", "win32")
    cap = cr.check_capability(dll_dir=tmp_path, compute_types_fn=lambda _: {"float16"},
                              load_check_fn=lambda p: (False, "/private/SECRET_TOKEN=abcd"))
    assert cap.reason_code == "runtime_load_error"
    assert "SECRET" not in cap.detail and "/private" not in cap.detail
    def fail(_):
        raise RuntimeError("/private/SECRET_TOKEN=abcd")
    cap = cr.check_capability(dll_dir=tmp_path, compute_types_fn=fail, load_check_fn=lambda p: (True, ""))
    assert cap.reason_code == "gpu_probe_failed" and "SECRET" not in cap.detail


def test_empty_types_require_concrete_vendor_facts_not_a_driver_guess():
    cap = cr.check_capability(compute_types_fn=lambda _: [], driver_probe_fn=lambda: {})
    assert cap.reason_code == "no_supported_compute_type"
    cap = cr.check_capability(compute_types_fn=lambda _: [], driver_probe_fn=lambda: {
        "driver_version": "100.1", "minimum_driver_version": "101.2"})
    assert cap.reason_code == "driver_too_old"
    cap = cr.check_capability(compute_types_fn=lambda _: [], driver_probe_fn=lambda: {"cuda_error_code": 100})
    assert cap.reason_code == "no_device"


@pytest.mark.parametrize("requested", ["auto", "cuda", "cpu"])
def test_resolution_preserves_request_and_wrapper_parity(monkeypatch, requested):
    from vidlift import transcribe
    calls = []
    def probe():
        calls.append(True)
        return cr.Capability(True, None, "", ("float16",))
    monkeypatch.setattr(cr, "check_capability", probe)
    resolution = transcribe.resolve_transcription_capability(requested)
    assert resolution.requested == requested
    assert resolution.resolved == ("cpu" if requested == "cpu" else "cuda")
    assert transcribe.resolve_transcription_device(requested) == resolution.resolved
    assert len(calls) == (0 if requested == "cpu" else 2)
    with pytest.raises(transcribe.TranscriptionError):
        transcribe.resolve_transcription_device("bogus")


@pytest.mark.parametrize("outcome,expected", [
    ("init", "init_timeout"), ("decode", "decode_timeout"), ("absolute", "decode_timeout"),
    ("exit", "gpu_init_failed"), ("init-error", "vram_or_init_failure"),
    ("probe-error", "gpu_probe_failed"), ("progress-error", "decode_failure"),
    ("probe-cpu", "no_supported_compute_type"),
])
def test_watchdog_reason_mapping_table(monkeypatch, outcome, expected):
    from vidlift import transcribe, transcription_watchdog as wd
    transcribe.reset_cuda_failure_cache()
    calls = []
    notes = []
    def worker(target, args, on_message, **kw):
        calls.append(args[0])
        if args[0] == "cpu":
            return "ok", "cpu", "en", None, []
        if outcome in {"init", "decode", "absolute"}:
            raise wd.WorkerTimeout(outcome)
        if outcome == "exit":
            raise wd.WorkerExited("synthetic exit")
        if outcome == "probe-error":
            return "error", "init", 0, "RuntimeError"
        if outcome == "probe-cpu":
            on_message("capability", ("no_supported_compute_type", cr.EMPTY_COMPUTE_TYPES_DETAIL))
            on_message("resolved", "cpu")
            return "ok", "cpu", "en", None, []
        on_message("resolved", "cuda")
        if outcome == "progress-error":
            on_message("progress", object())
            return "error", "decode", 1, "RuntimeError"
        return "error", "init", 0, "RuntimeError"
    monkeypatch.setattr(wd, "supervise_worker", worker)
    try:
        def invoke():
            return transcribe._isolated_transcription("model", "fake.wav", "cuda", "en", False,
                None, None, None, None, batch_id="table", on_fallback=notes.append)
        if outcome == "progress-error":
            with pytest.raises(transcribe.TranscriptionError, match=expected):
                invoke()
            assert calls == ["cuda"]
            assert transcribe._cached_cuda_failure("table") is None
        else:
            result = invoke()
            assert any(expected in note for note in result[-1])
            assert any(expected in note for note in notes)
            assert calls == (["cuda"] if outcome == "probe-cpu" else ["cuda", "cpu"])
            cached = transcribe._cached_cuda_failure("table")
            assert cached is not None and cached[0] == expected
    finally:
        transcribe.reset_cuda_failure_cache()
