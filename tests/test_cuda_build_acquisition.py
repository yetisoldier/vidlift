from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import struct
import os

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("acquire_cuda_runtime", ROOT / "packaging/acquire_cuda_runtime.py")
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(module)


def test_manifest_is_exact_win_amd64_pinned_inventory():
    manifest = json.loads((ROOT / "packaging/cuda_runtime_manifest.json").read_text())
    assert manifest["target"] == "win_amd64"
    assert len(manifest["artifacts"]) == 3
    for artifact in manifest["artifacts"]:
        assert artifact["filename"].endswith("win_amd64.whl")
        assert len(artifact["sha256"]) == 64
        int(artifact["sha256"], 16)
        assert artifact["version"] in artifact["filename"]
    assert "zlibwapi.dll" in manifest["required_dlls"]


def test_pe_machine_rejects_non_x64_inventory(tmp_path):
    pe = tmp_path / "sample.dll"
    data = bytearray(0x80 + 6)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x40)
    data[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", data, 0x44, 0x8664)
    pe.write_bytes(data)
    assert module.pe_machine(pe) == 0x8664
    struct.pack_into("<H", data, 0x44, 0x14C)
    pe.write_bytes(data)
    assert module.pe_machine(pe) == 0x14C


def test_inventory_requires_unique_x64_dlls(tmp_path):
    dll = tmp_path / "cudart64_12.dll"
    data = bytearray(0x80 + 6)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x40)
    data[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", data, 0x44, 0x8664)
    dll.write_bytes(data)
    assert module.verify_inventory(tmp_path, [dll.name]) == [dll]
    (tmp_path / "duplicate").mkdir()
    (tmp_path / "duplicate" / dll.name).write_bytes(data)
    try:
        module.verify_inventory(tmp_path, [dll.name])
    except ValueError as exc:
        assert "missing or duplicated" in str(exc)
    else:
        raise AssertionError("duplicate basename must fail closed")


def test_acquisition_fails_closed_without_legal_clearance(monkeypatch, tmp_path):
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.delenv("VIDLIFT_NVIDIA_REDISTRIBUTION_APPROVED", raising=False)
    try:
        module.acquire(tmp_path / "stage")
    except RuntimeError as exc:
        assert "redistribution approval" in str(exc)
    else:
        raise AssertionError("acquisition must require explicit legal gate")


def test_pyinstaller_collection_is_empty_by_default(monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "packaging"))
    import pyinstaller_cuda
    monkeypatch.delenv("VIDLIFT_CUDA_STAGE", raising=False)
    assert pyinstaller_cuda.cuda_binaries() == []


def test_pyinstaller_collection_requires_complete_validated_inventory(monkeypatch, tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "packaging"))
    import pyinstaller_cuda
    monkeypatch.setenv("VIDLIFT_CUDA_STAGE", str(tmp_path))
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    try:
        pyinstaller_cuda.cuda_binaries()
    except ValueError as exc:
        assert "missing or duplicated" in str(exc)
    else:
        raise AssertionError("incomplete stage must fail closed")
