"""Acquire hash-pinned NVIDIA Windows wheels into a build-only staging tree.

Never copies binaries into the repository. Packaging remains fail-closed until
redistribution review and the independently sourced zlib dependency are cleared.
"""
from __future__ import annotations

import hashlib
import fnmatch
import json
import os
import shutil
from pathlib import Path
import struct
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "packaging" / "cuda_runtime_manifest.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def pe_machine(path: Path) -> int | None:
    """Return IMAGE_FILE_HEADER.Machine for PE files, else None."""
    with path.open("rb") as f:
        if f.read(2) != b"MZ":
            return None
        f.seek(0x3C)
        offset = struct.unpack("<I", f.read(4))[0]
        f.seek(offset)
        if f.read(4) != b"PE\0\0":
            raise ValueError(f"Malformed PE signature: {path}")
        return struct.unpack("<H", f.read(2))[0]


def verify_inventory(root: Path, required: list[str]) -> list[Path]:
    dlls = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".dll")
    if root.is_symlink() or any(p.is_symlink() for p in dlls):
        raise ValueError("Runtime inventory must not contain symlinked DLLs")
    reviewed = {name.lower() for name in required}
    if not reviewed or len(reviewed) != len(required):
        raise ValueError("Reviewed DLL inventory must be nonempty and unique")
    by_name: dict[str, list[Path]] = {}
    for dll in dlls:
        by_name.setdefault(dll.name.lower(), []).append(dll)
        if pe_machine(dll) != 0x8664:
            raise ValueError(f"DLL is not a valid x64 PE image: {dll}")
    missing = [name for name in required if len(by_name.get(name.lower(), [])) != 1]
    if missing:
        raise ValueError("Required DLL missing or duplicated: " + ", ".join(missing))
    duplicates = [name for name, paths in by_name.items() if len(paths) > 1]
    if duplicates:
        raise ValueError("DLL basename missing or duplicated: " + ", ".join(duplicates))
    extras = sorted(set(by_name) - reviewed)
    if extras:
        raise ValueError("Unreviewed DLL(s): " + ", ".join(extras))
    return [by_name[name.lower()][0] for name in required]


def _pe_imports(path: Path) -> set[str]:
    """Read normal AND delay-load PE32+ import descriptors, fail on truncation.

    No vendor DLL is loaded. RVAs must map to file-backed section bytes, not
    virtual zero-fill. A missing import table is valid, a malformed one is not.
    """
    data = path.read_bytes()
    def unpack(fmt, offset):
        if offset < 0 or offset + struct.calcsize(fmt) > len(data):
            raise ValueError(f"Truncated PE import metadata: {path.name}")
        return struct.unpack_from(fmt, data, offset)
    if data[:2] != b"MZ":
        raise ValueError(f"Not a PE image: {path.name}")
    pe, = unpack("<I", 0x3C)
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError(f"Invalid PE signature: {path.name}")
    machine, sections = unpack("<HH", pe + 4)
    optional_size, = unpack("<H", pe + 20)
    optional = pe + 24
    magic, = unpack("<H", optional)
    if machine != 0x8664 or magic != 0x20B or optional_size < 112:
        raise ValueError(f"Expected x64 PE32+ import metadata: {path.name}")
    image_base, = unpack("<Q", optional + 24)
    directory_count, = unpack("<I", optional + 108)
    table = optional + optional_size
    regions = []
    for i in range(sections):
        _, rva, size, raw = unpack("<IIII", table + 40 * i + 8)
        regions.append((rva, size, raw))
    def offset(rva, length=1):
        for base, size, raw in regions:
            if base <= rva and rva + length <= base + size:
                result = raw + rva - base
                if result + length <= len(data):
                    return result
        raise ValueError(f"Unmapped PE import RVA: {path.name}")
    def name_at(rva):
        start = offset(rva)
        chars = bytearray()
        for i in range(260):
            byte = data[offset(rva + i)]
            if byte == 0:
                try:
                    name = chars.decode("ascii").lower()
                except UnicodeError as exc:
                    raise ValueError(f"Invalid PE import name: {path.name}") from exc
                if not name or "/" in name or "\\" in name:
                    raise ValueError(f"Invalid PE import name: {path.name}")
                return name
            chars.append(byte)
        raise ValueError(f"Unterminated PE import name: {path.name}")
    imports = set()
    for index, stride in ((1, 20), (13, 32)):
        if directory_count <= index:
            continue
        if 112 + (index + 1) * 8 > optional_size:
            raise ValueError(f"Truncated PE directory table: {path.name}")
        rva, size = unpack("<II", optional + 112 + index * 8)
        if not rva and not size:
            continue
        if not rva or size < stride:
            raise ValueError(f"Invalid PE import directory: {path.name}")
        terminated = False
        for i in range(min(size // stride, 65536)):
            fields = unpack("<" + "I" * (stride // 4), offset(rva + i * stride, stride))
            if not any(fields):
                terminated = True
                break
            name_rva = fields[3] if index == 1 else fields[1]
            if index == 13 and not fields[0] & 1:
                name_rva -= image_base
            imports.add(name_at(name_rva))
        if not terminated:
            raise ValueError(f"Unterminated PE import descriptors: {path.name}")
    return imports


def pe_import_closure(dlls: list[Path]) -> dict[str, set[str]]:
    return {dll.name.lower(): _pe_imports(dll) for dll in dlls}


def validate_import_closure(dlls: list[Path], system_allowlist: list[str]) -> dict[str, set[str]]:
    closure = pe_import_closure(dlls)
    staged = set(closure)
    for source, imports in closure.items():
        missing = sorted(name for name in imports if name not in staged and not any(
            fnmatch.fnmatchcase(name, allowed.lower()) for allowed in system_allowlist))
        if missing:
            raise ValueError(f"Unresolved PE imports for {source}: {', '.join(missing)}")
    return closure


def collect_licenses(destination: Path, manifest: dict) -> list[Path]:
    """Collect only explicitly reviewed, hash-pinned notice/EULA inputs.

    Empty metadata is the current E1/E2 state: no-op, never an approval flag.
    Records must be supplied by the exact-artifact legal review, not inferred
    from filenames or guessed from a downloaded wheel's contents.
    """
    records = manifest.get("license_files", [])
    if not records:
        print("license collection disabled pending legal clearance", file=sys.stderr)
        return []
    collected = []
    names = set()
    for record in records:
        source = (destination / "extracted" / record["source"]).resolve()
        root = (destination / "extracted").resolve()
        name = record["name"]
        if not source.is_relative_to(root) or Path(name).name != name or name in {"", ".", ".."} or "/" in name or "\\" in name or ":" in name:
            raise ValueError("Unsafe reviewed license path")
        if name.lower() in names:
            raise ValueError("Duplicate reviewed license basename")
        names.add(name.lower())
        if sha256(source) != record["sha256"]:
            raise ValueError(f"License SHA-256 mismatch: {name}")
        target = destination / "licenses" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        collected.append(target)
    return collected


def acquire(destination: Path) -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if sys.platform != "win32":
        raise RuntimeError("CUDA wheel acquisition is restricted to Windows build hosts")
    if os.environ.get("VIDLIFT_NVIDIA_REDISTRIBUTION_APPROVED") != "1":
        raise RuntimeError("Fail-closed: NVIDIA redistribution approval is not recorded")
    destination.mkdir(parents=True, exist_ok=True)
    wheel_dir = destination / "wheels"
    extract_dir = destination / "extracted"
    wheel_dir.mkdir(exist_ok=True)
    extract_dir.mkdir(exist_ok=True)
    for artifact in manifest["artifacts"]:
        wheel = wheel_dir / artifact["filename"]
        if not wheel.exists():
            request = urllib.request.Request(artifact["url"], headers={"User-Agent": "VidLift-build-acquisition/1"})
            with urllib.request.urlopen(request, timeout=120) as response, wheel.open("wb") as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
        actual = sha256(wheel)
        if actual != artifact["sha256"]:
            wheel.unlink(missing_ok=True)
            raise RuntimeError(f"SHA-256 mismatch for {artifact['filename']}: {actual}")
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(extract_dir)
    reviewed = manifest["required_dlls"] + manifest.get("reviewed_transitive_dlls", [])
    dlls = verify_inventory(extract_dir, reviewed)
    closure = validate_import_closure(dlls, manifest["windows_system_dlls"])
    licenses = collect_licenses(destination, manifest)
    if not licenses:
        raise RuntimeError("Fail-closed: exact runtime/license review and pinned zlib closure remain pending")
    return {"wheels": len(manifest["artifacts"]), "dll_count": len(dlls),
            "sha256_verified": True, "license_count": len(licenses),
            "import_closure": {name: sorted(imports) for name, imports in closure.items()}}


def main() -> int:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(os.environ.get("RUNNER_TEMP", ".")) / "vidlift-cuda-runtime"
    try:
        print(json.dumps(acquire(target), sort_keys=True))
    except Exception as exc:
        print(f"CUDA acquisition blocked: {exc}", file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
