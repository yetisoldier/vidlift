"""Archived build intent and paired accidental-corruption regressions."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import types

import pytest

from vidlift import cuda_runtime as runtime


def freeze(monkeypatch, root, enabled=True):
    identity = {"identity_schema": 1, "artifact_kind": "gui-onedir", "application_version": "test",
                "build_reference": "synthetic", "cuda_bundled": enabled, "inventory": {}}
    if enabled:
        files = {"ctranslate2/cudart.dll": b"synthetic DLL, never loaded",
                 "packaging/cuda_runtime_manifest.json": b'{"required_dlls":["cudart.dll"]}',
                 "licenses/notice.txt": b"synthetic notice, not legal approval"}
        for name, data in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            identity["inventory"][name] = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    monkeypatch.setattr(sys, "_vidlift_build_identity", identity, raising=False)
    return identity


@pytest.mark.parametrize("enabled", [False, True])
def test_paired_removed_marker_and_runtime_does_not_masquerade_cpu(tmp_path, monkeypatch, enabled):
    freeze(monkeypatch, tmp_path, enabled)
    if enabled:
        shutil.rmtree(tmp_path / "ctranslate2")
        (tmp_path / "packaging/cuda_runtime_manifest.json").unlink()
    def forbidden(**kwargs):
        pytest.fail("corrupt/missing payload must never probe or load")
    section = runtime.selftest_section(capability_fn=forbidden)
    assert section["status"] == ("error" if enabled else "skipped")
    assert section["reason_code"] == ("runtime_missing" if enabled else "not_bundled")


@pytest.mark.parametrize("mutation,reason", [("absent", "artifact_identity_missing"),
    ("malformed", "artifact_identity_invalid"), ("marker", "runtime_missing"),
    ("notice", "runtime_missing"), ("dll", "runtime_integrity_error"),
    ("marker-hash", "runtime_integrity_error")])
def test_identity_and_every_reviewed_payload_checked_before_probe(tmp_path, monkeypatch, mutation, reason):
    freeze(monkeypatch, tmp_path)
    if mutation == "absent":
        monkeypatch.delattr(sys, "_vidlift_build_identity")
    elif mutation == "malformed":
        monkeypatch.setattr(sys, "_vidlift_build_identity", {"cuda_bundled": True})
    elif mutation == "marker":
        (tmp_path / "packaging/cuda_runtime_manifest.json").unlink()
    elif mutation == "notice":
        (tmp_path / "licenses/notice.txt").unlink()
    else:
        name = "ctranslate2/cudart.dll" if mutation == "dll" else "packaging/cuda_runtime_manifest.json"
        (tmp_path / name).write_bytes(b"corrupt")
    assert runtime.selftest_section()["reason_code"] == reason
    def forbidden(device):
        pytest.fail("native probe before artifact integrity")
    assert runtime.check_capability(compute_types_fn=forbidden).reason_code == reason


def test_valid_enabled_no_device_and_cpu_contradiction(tmp_path, monkeypatch):
    freeze(monkeypatch, tmp_path)
    section = runtime.selftest_section(capability_fn=lambda **kw: runtime.Capability(False, "no_device", "vendor signal"))
    assert section["status"] == "skipped" and section["reason_code"] == "no_device"
    monkeypatch.setattr(sys, "_vidlift_build_identity", dict(sys._vidlift_build_identity, cuda_bundled=False, inventory={}))
    assert runtime.selftest_section()["reason_code"] == "artifact_identity_invalid"


@pytest.mark.parametrize("spec,kind", [("vidlift_gui.spec", "gui-onedir"), ("vidlift_cli.spec", "cli-onefile")])
def test_real_specs_collect_shared_description_and_execute_generated_hook(tmp_path, monkeypatch, spec, kind):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "packaging"))
    import pyinstaller_cuda
    import build_identity
    monkeypatch.delenv("VIDLIFT_CUDA_STAGE", raising=False)
    original_hook = build_identity.runtime_hook
    monkeypatch.setattr(build_identity, "runtime_hook", lambda identity: original_hook(identity, directory=tmp_path))
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_data_files = lambda package: [("synthetic-vad", "faster_whisper/assets")]
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    analysis = []
    class FakeAnalysis:
        def __init__(self, *args, **kwargs):
            analysis.append(kwargs)
            self.pure, self.scripts, self.binaries, self.datas = [], [], kwargs["binaries"], kwargs["datas"]
    globals_ = {"Analysis": FakeAnalysis, "PYZ": lambda *args: None,
                "EXE": lambda *args, **kw: None, "COLLECT": lambda *args, **kw: None}
    exec(compile((root / spec).read_text(), spec, "exec"), globals_)
    assert analysis[0]["binaries"] == []
    hook, = analysis[0]["runtime_hooks"]
    exec(Path(hook).read_text(), {})
    identity = sys._vidlift_build_identity
    assert identity["artifact_kind"] == kind and identity["cuda_bundled"] is False
    assert identity["inventory"] == {}
    assert Path(hook).is_relative_to(tmp_path)
