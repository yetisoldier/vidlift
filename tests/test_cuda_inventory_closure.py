"""Fail-closed inventory/import closure with synthetic PE32+ bytes only."""
import importlib.util
import json
from pathlib import Path
import struct

import pytest

from test_cuda_build_acquisition import module, ROOT


def pe(path, imports=(), delay=()):
    data = bytearray(0x1400)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", data, 0x84, 0x8664, 1)
    struct.pack_into("<H", data, 0x94, 240)
    optional = 0x98
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<Q", data, optional + 24, 0x180000000)
    struct.pack_into("<I", data, optional + 108, 16)
    section = optional + 240
    struct.pack_into("<IIII", data, section + 8, 0x1000, 0x1000, 0x1000, 0x400)
    for index, names, table_rva, strings_rva, stride in (
        (1, imports, 0x1000, 0x1300, 20), (13, delay, 0x1100, 0x1500, 32)
    ):
        if not names:
            continue
        struct.pack_into("<II", data, optional + 112 + index * 8, table_rva, (len(names) + 1) * stride)
        for i, name in enumerate(names):
            name_rva = strings_rva + i * 128
            where = 0x400 + table_rva - 0x1000 + i * stride
            fields = (0, 0, 0, name_rva, 0) if index == 1 else (1, name_rva, 0, 0, 0, 0, 0, 0)
            struct.pack_into("<" + "I" * len(fields), data, where, *fields)
            encoded = name.encode() + b"\0"
            start = 0x400 + name_rva - 0x1000
            data[start:start + len(encoded)] = encoded
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_extra_dll_and_uppercase_extension_fail(tmp_path):
    pe(tmp_path / "required.dll")
    pe(tmp_path / "EXTRA.DLL")
    with pytest.raises(ValueError, match="Unreviewed.*extra.dll"):
        module.verify_inventory(tmp_path, ["required.dll"])


def test_duplicate_unreviewed_basename_fails(tmp_path):
    pe(tmp_path / "required.dll")
    pe(tmp_path / "one/extra.dll")
    pe(tmp_path / "two/EXTRA.DLL")
    with pytest.raises(ValueError, match="duplicated.*extra.dll"):
        module.verify_inventory(tmp_path, ["required.dll"])


def test_import_closure_and_delay_load_transitive_resolution(tmp_path):
    first = pe(tmp_path / "first.dll", ["KERNEL32.dll", "next.dll"], ["zlibwapi.dll"])
    second = pe(tmp_path / "next.dll", ["api-ms-win-core-test.dll"])
    third = pe(tmp_path / "zlibwapi.dll", ["ntdll.dll"])
    allow = ["kernel32.dll", "ntdll.dll", "api-ms-win-*.dll"]
    with pytest.raises(ValueError, match="zlibwapi.dll"):
        module.validate_import_closure([first, second], allow)
    closure = module.validate_import_closure([first, second, third], allow)
    assert closure["first.dll"] == {"kernel32.dll", "next.dll", "zlibwapi.dll"}
    assert module.verify_inventory(tmp_path, ["next.dll", "first.dll", "zlibwapi.dll"]) == [second, first, third]


def test_malformed_import_metadata_fails_closed(tmp_path):
    image = pe(tmp_path / "bad.dll", ["kernel32.dll"])
    data = bytearray(image.read_bytes())
    struct.pack_into("<I", data, 0x400 + 12, 0xFFFF0000)
    image.write_bytes(data)
    with pytest.raises(ValueError, match="Unmapped"):
        module.pe_import_closure([image])


def test_license_collection_stays_disabled_pending_review(tmp_path, capsys):
    assert module.collect_licenses(tmp_path, {"license_files": []}) == []
    assert "disabled pending legal clearance" in capsys.readouterr().err
    assert not (tmp_path / "licenses").exists()


def test_synthetic_reviewed_license_copy_is_hash_checked(tmp_path):
    path = tmp_path / "extracted/vendor/LICENSE.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"Synthetic test notice, not vendor terms")
    manifest = {"license_files": [{"source": "vendor/LICENSE.txt", "name": "synthetic.txt", "sha256": module.sha256(path)}]}
    copied = module.collect_licenses(tmp_path, manifest)
    assert copied[0].read_bytes() == path.read_bytes()
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        module.collect_licenses(tmp_path, manifest)


def test_specs_share_collectors_without_shadowing_packaging_library(monkeypatch):
    for spec in ("vidlift_gui.spec", "vidlift_cli.spec"):
        text = (ROOT / spec).read_text()
        assert "from pyinstaller_cuda import collect_build" in text
        assert "binaries=cuda_binaries," in text
        assert '+ cuda_datas,' in text
        assert "runtime_hooks=[identity_hook]" in text
        assert "collect_build(" in text
    import sys
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    import pyinstaller_cuda
    monkeypatch.delenv("VIDLIFT_CUDA_STAGE", raising=False)
    assert pyinstaller_cuda.cuda_binaries() == []
    assert pyinstaller_cuda.cuda_datas() == []


def test_complete_synthetic_inventory_cannot_bypass_redistribution_gate(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "packaging"))
    import pyinstaller_cuda
    manifest = json.loads((ROOT / "packaging/cuda_runtime_manifest.json").read_text())
    for name in manifest["required_dlls"]:
        pe(tmp_path / "extracted" / name)
    monkeypatch.setenv("VIDLIFT_CUDA_STAGE", str(tmp_path))
    monkeypatch.delenv("VIDLIFT_NVIDIA_REDISTRIBUTION_APPROVED", raising=False)
    with pytest.raises(RuntimeError, match="redistribution approval"):
        pyinstaller_cuda.cuda_binaries()
    with pytest.raises(RuntimeError, match="redistribution approval"):
        pyinstaller_cuda.cuda_datas()
