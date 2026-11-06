"""Optional PyInstaller collection of verified staged CUDA DLLs."""
from __future__ import annotations

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acquire_cuda_runtime import verify_inventory, validate_import_closure, sha256


def cuda_binaries() -> list[tuple[str, str]]:
    stage = os.environ.get("VIDLIFT_CUDA_STAGE")
    if not stage:
        return []
    root = Path(stage) / "extracted"
    manifest_path = Path(__file__).with_name("cuda_runtime_manifest.json")
    import json

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dlls = verify_inventory(root, manifest["required_dlls"] + manifest.get("reviewed_transitive_dlls", []))
    if os.environ.get("VIDLIFT_NVIDIA_REDISTRIBUTION_APPROVED") != "1":
        raise RuntimeError("Fail-closed: NVIDIA redistribution approval is not recorded")
    if not manifest.get("license_files"):
        raise RuntimeError("Fail-closed: reviewed license-notice metadata is pending clearance")
    for record in manifest["license_files"]:
        if sha256(Path(stage) / "licenses" / record["name"]) != record["sha256"]:
            raise ValueError("Reviewed license-notice SHA-256 mismatch")
    validate_import_closure(dlls, manifest["windows_system_dlls"])
    return [(str(path), "ctranslate2") for path in dlls]


def cuda_datas(*, validated_binaries=None) -> list[tuple[str, str]]:
    """Only GPU-enabled artifacts carry the reviewed inventory marker."""
    if not os.environ.get("VIDLIFT_CUDA_STAGE"):
        return []
    import json
    manifest_path = Path(__file__).with_name("cuda_runtime_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if validated_binaries is None:
        cuda_binaries()  # data-only callers still use the identical gate
    stage = Path(os.environ["VIDLIFT_CUDA_STAGE"])
    return [(str(manifest_path), "packaging")] + [
        (str(stage / "licenses" / record["name"]), "licenses")
        for record in manifest["license_files"]]


def collect_build(artifact_kind):
    """One validation result supplies Analysis AND its embedded inventory."""
    from build_identity import make_identity, runtime_hook
    binaries = cuda_binaries()
    datas = cuda_datas(validated_binaries=binaries)
    identity = make_identity(artifact_kind, binaries, datas)
    return binaries, datas, runtime_hook(identity)
