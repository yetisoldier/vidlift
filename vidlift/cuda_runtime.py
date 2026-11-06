"""D2 structured CUDA capability preflight + D6 reason codes (v0.4.20).

Pure and injectable: every external touchpoint (the reviewed DLL
inventory, loader-semantics loadability, the compute-type probe) is a
parameter with an honest default, so Linux tests drive every branch
deterministically without a GPU or NVIDIA binaries. Import-safe on all
platforms — no Windows-only imports at module level.

The preflight is a PREFLIGHT (ADR-2): actual engine construction and the
first decode stay guarded in the spawned child exactly as D3 requires;
``available=True`` never claims kernel-level success. Device count
remains diagnostic only, never qualifying.
"""
from __future__ import annotations

import json
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# ---- D6 binding vocabulary ---------------------------------------------
# Conservative-mapping rule (ADR-2): a code is emitted only when the
# evidence reliably supports it. The two RESERVED codes below must NOT be
# emitted until a reliable vendor signal exists — honesty over coverage:
#   - device_power_gated: only on an explicit vendor power-state signal
#     (none exists in our stack today).
#   - cudnn_version_unsupported: only once the pinned cuDNN wheel pair is
#     frozen and a metadata comparison can be made.
REASON_RUNTIME_MISSING = "runtime_missing"
REASON_RUNTIME_LOAD_ERROR = "runtime_load_error"
REASON_CUDNN_VERSION_UNSUPPORTED = "cudnn_version_unsupported"  # reserved
REASON_DRIVER_MISSING = "driver_missing"
REASON_DRIVER_TOO_OLD = "driver_too_old"
REASON_NO_DEVICE = "no_device"
REASON_DEVICE_POWER_GATED = "device_power_gated"  # reserved
REASON_NO_SUPPORTED_COMPUTE_TYPE = "no_supported_compute_type"
REASON_VRAM_OR_INIT_FAILURE = "vram_or_init_failure"
REASON_INIT_TIMEOUT = "init_timeout"
REASON_DECODE_FAILURE = "decode_failure"
REASON_DECODE_TIMEOUT = "decode_timeout"
# Unknown native errors map here with sanitized detail:
REASON_GPU_PROBE_FAILED = "gpu_probe_failed"
REASON_GPU_INIT_FAILED = "gpu_init_failed"

ALL_REASON_CODES = frozenset({
    REASON_RUNTIME_MISSING,
    REASON_RUNTIME_LOAD_ERROR,
    REASON_CUDNN_VERSION_UNSUPPORTED,
    REASON_DRIVER_MISSING,
    REASON_DRIVER_TOO_OLD,
    REASON_NO_DEVICE,
    REASON_DEVICE_POWER_GATED,
    REASON_NO_SUPPORTED_COMPUTE_TYPE,
    REASON_VRAM_OR_INIT_FAILURE,
    REASON_INIT_TIMEOUT,
    REASON_DECODE_FAILURE,
    REASON_DECODE_TIMEOUT,
    REASON_GPU_PROBE_FAILED,
    REASON_GPU_INIT_FAILED,
})

# The conservative detail for an empty compute-type probe: driver_missing /
# driver_too_old require a reliable vendor signal (a concrete driver version
# below the pinned runtime's documented floor); absent that signal we do NOT
# guess.
EMPTY_COMPUTE_TYPES_DETAIL = "CUDA compute types empty; driver state unknown"


@dataclass(frozen=True)
class Capability:
    """The structured preflight result (D2).

    ``reason_code`` is None iff ``available`` is True — the invariant is
    enforced so a malformed construction can never render as both."""

    available: bool
    reason_code: str | None
    detail: str
    supported_compute_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.available == (self.reason_code is not None):
            raise ValueError(
                "Capability invariant violated: reason_code must be None "
                "iff available is True"
            )
        object.__setattr__(self, "detail", self.detail[:200])


# ---- reviewed runtime inventory -----------------------------------------

def _manifest_path() -> Path | None:
    """``packaging/cuda_runtime_manifest.json`` next to the package root,
    or None when this artifact ships no reviewed runtime inventory. The
    manifest's ``required_dlls`` list is the reviewed DLL set (S6 owns its
    contents; this module only consumes it)."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    candidate = root / "packaging" / "cuda_runtime_manifest.json"
    return candidate if candidate.is_file() else None


def _required_dlls(manifest: dict) -> list[str]:
    required = manifest.get("required_dlls")
    transitive = manifest.get("reviewed_transitive_dlls", [])
    if not isinstance(required, list) or not isinstance(transitive, list):
        raise ValueError("invalid required_dlls inventory")
    required = required + transitive
    if not isinstance(required, list) or not required or any(
        not isinstance(name, str) or Path(name).name != name
        or "/" in name or "\\" in name or ":" in name or not name.lower().endswith(".dll")
        for name in required
    ):
        raise ValueError("invalid required_dlls inventory")
    if len({name.lower() for name in required}) != len(required):
        raise ValueError("duplicate required_dlls inventory")
    return required


def artifact_identity():
    """Source is explicit; a frozen artifact must carry its archived hook."""
    if not getattr(sys, "frozen", False):
        return {"context": "source"}
    return getattr(sys, "_vidlift_build_identity", None)


def _artifact_root():
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else Path(sys.executable).parent / "_internal"


def artifact_integrity(*, identity_fn=None):
    """None on success/source; otherwise a sanitized pre-load fault."""
    identity = (identity_fn or artifact_identity)()
    if not getattr(sys, "frozen", False):
        return None
    if identity is None:
        return Capability(False, "artifact_identity_missing", "Frozen build identity is missing")
    try:
        if (not isinstance(identity, dict) or identity.get("identity_schema") != 1 or
                identity.get("artifact_kind") not in {"gui-onedir", "cli-onefile"} or
                type(identity.get("cuda_bundled")) is not bool or
                not isinstance(identity.get("application_version"), str) or
                not isinstance(identity.get("build_reference"), str) or
                not isinstance(identity.get("inventory"), dict)):
            raise ValueError("malformed identity")
        inventory = identity["inventory"]
        root = _artifact_root()
        if not identity["cuda_bundled"]:
            if inventory or (root / "packaging/cuda_runtime_manifest.json").exists() or any(
                    (root / "ctranslate2").glob("cu*.dll")):
                raise ValueError("CPU identity contradicts CUDA payload")
            return None
        if ("packaging/cuda_runtime_manifest.json" not in inventory or
                not any(name.startswith("licenses/") for name in inventory) or
                not any(name.startswith("ctranslate2/") and name.endswith(".dll") for name in inventory)):
            raise ValueError("incomplete reviewed inventory")
        for name, expected in inventory.items():
            if (not isinstance(name, str) or "\\" in name or ":" in name or
                    name.startswith("/") or ".." in name.split("/") or
                    name.split("/")[0] not in {"ctranslate2", "licenses", "packaging"} or
                    not isinstance(expected, str) or len(expected) != 64 or
                    any(ch not in "0123456789abcdef" for ch in expected)):
                raise ValueError("invalid inventory path/hash")
            path = root / name
            if not path.is_file():
                return Capability(False, REASON_RUNTIME_MISSING, "Missing bundled file: " + name)
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return Capability(False, "runtime_integrity_error", "Bundled hash mismatch: " + name)
        return None
    except (OSError, ValueError, TypeError, AttributeError):
        return Capability(False, "artifact_identity_invalid", "Frozen build identity or inventory is invalid")


def bundled_runtime_dir() -> Path | None:
    """Fixed extraction layout from embedded intent, never guessed by files."""
    identity = artifact_identity()
    if getattr(sys, "frozen", False) and isinstance(identity, dict) and identity.get("cuda_bundled") is True:
        return _artifact_root() / "ctranslate2"
    return None


def _legacy_runtime_discovery() -> Path | None:
    """The CUDA runtime directory for THIS artifact, or None when none is
    bundled (source/dev installs — ctranslate2 uses system CUDA via its own
    loader). PyInstaller Windows artifacts carry the reviewed set under
    ``ctranslate2/`` under _MEIPASS (onefile or onedir _internal).
    A bundled manifest marks an enabled payload, including a corrupt payload
    whose entire runtime directory was deleted."""
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)  # onefile temp extraction
    roots = []
    if meipass:
        roots.append(Path(meipass))
    try:
        roots.append(Path(sys.executable).parent)
    except (OSError, TypeError):
        pass
    for root in roots:
        for candidate in (root / "ctranslate2",
                          root / "_internal" / "ctranslate2"):
            try:
                marker = candidate.parent / "packaging" / "cuda_runtime_manifest.json"
                if marker.is_file() or (candidate.is_dir() and
                        any(candidate.glob("cud*.dll"))):
                    return candidate
            except OSError:
                continue
    return None


# ---- injectable defaults -------------------------------------------------

def _default_load_check(dll_path):
    """Loader-semantics loadability of one DLL (Windows only).

    ``os.add_dll_directory`` is held for the check and released after —
    never a global PATH mutation. Returns ``(ok, detail)``."""
    import ctypes

    handle = os.add_dll_directory(str(dll_path.parent))
    try:
        ctypes.CDLL(str(dll_path))
        return True, ""
    except OSError:
        return False, f"{dll_path.name}: loader rejected DLL"
    finally:
        handle.close()


def _default_compute_types(device: str) -> list[str]:
    import ctranslate2

    types = ctranslate2.get_supported_compute_types(device)
    return list(types or [])


# ---- the single public preflight -----------------------------------------

def check_capability(
    *,
    dll_dir=None,
    compute_types_fn=None,
    load_check_fn=None,
    driver_probe_fn=None,
) -> Capability:
    """The D2 capability preflight. Steps, in order:

    1. Reviewed-inventory completeness — when a CUDA runtime directory
       applies to this artifact (Windows + manifest present + dll_dir
       known) and any required DLL is missing → ``runtime_missing``.
    2. Loader-semantics loadability of each required DLL (the
       add_dll_directory handle held for the check; non-Windows platforms
       skip both DLL steps — capability degrades to step 3 only, which is
       honest: source/venv Linux builds have no bundled runtime). A load
       failure → ``runtime_load_error``.
    3. ``compute_types_fn("cuda")`` (default: ctranslate2) must be
       non-empty; empty → ``no_supported_compute_type`` with the
       conservative detail — driver_missing/driver_too_old are emitted
       only on a reliable vendor signal, which this preflight does not
       claim.

    Every external touchpoint is an injectable parameter so Linux tests
    drive every branch deterministically without a GPU or NVIDIA
    binaries."""
    fault = artifact_integrity()
    if fault is not None:
        return fault
    if sys.platform == "win32":
        dll_dir = Path(dll_dir) if dll_dir is not None else bundled_runtime_dir()
        if dll_dir is not None:
            try:
                manifest_path = _manifest_path()
                if manifest_path is None:
                    raise ValueError("absent manifest")
                required = _required_dlls(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, AttributeError):
                return Capability(False, REASON_RUNTIME_MISSING, "runtime inventory missing or invalid")
            missing = [name for name in required if not (dll_dir / name).is_file()]
            if missing:
                return Capability(False, REASON_RUNTIME_MISSING,
                                  "missing CUDA runtime DLL(s): " + ", ".join(missing))
            load_check = load_check_fn or _default_load_check
            for name in required:
                try:
                    ok, _detail = load_check(dll_dir / name)
                except Exception:
                    ok = False
                if not ok:
                    # Never relay raw loader errors, which can contain paths,
                    # environment values or secrets. Artifact-relative name only.
                    return Capability(False, REASON_RUNTIME_LOAD_ERROR,
                                      f"{name}: loader rejected DLL")

    probe = compute_types_fn or _default_compute_types
    probe_error = None
    try:
        types = sorted(set(probe("cuda") or []))
    except Exception as exc:
        types = []
        probe_error = type(exc).__name__
        # CUDA_ERROR_NO_DEVICE is an explicit vendor signal, not a count guess.
        if getattr(exc, "cuda_error_code", None) == 100:
            return Capability(False, REASON_NO_DEVICE, "CUDA_ERROR_NO_DEVICE (100)")
    if types:
        return Capability(True, None, "", tuple(types))
    if driver_probe_fn is not None:
        try:
            facts = driver_probe_fn()
            if facts.get("cuda_error_code") == 100:
                return Capability(False, REASON_NO_DEVICE, "CUDA_ERROR_NO_DEVICE (100)")
            # Versions must be concrete vendor facts against an explicitly
            # supplied documented floor. No floor is invented before E2.
            version = tuple(int(x) for x in facts["driver_version"].split("."))
            floor = tuple(int(x) for x in facts["minimum_driver_version"].split("."))
            width = max(len(version), len(floor))
            if version + (0,) * (width - len(version)) < floor + (0,) * (width - len(floor)):
                return Capability(False, REASON_DRIVER_TOO_OLD,
                                  "vendor driver version below documented runtime floor")
        except (KeyError, TypeError, ValueError, AttributeError, OSError):
            pass  # no reliable fact, no diagnosis
    if probe_error:
        return Capability(False, REASON_GPU_PROBE_FAILED,
                          f"compute-type probe failed ({probe_error})")
    return Capability(False, REASON_NO_SUPPORTED_COMPUTE_TYPE, EMPTY_COMPUTE_TYPES_DETAIL)


def selftest_section(*, capability_fn=None, runtime_dir_fn=None) -> dict:
    """CUDA section only. The caller owns the never-raises report envelope."""
    fault = artifact_integrity()
    if fault is not None:
        return {"status": "error", "reason_code": fault.reason_code, "detail": fault.detail,
                "resolved_device": "cpu"}
    runtime = (runtime_dir_fn or bundled_runtime_dir)()
    if runtime is None:
        return {"status": "skipped", "reason_code": "not_bundled"}
    cap = (capability_fn or check_capability)(dll_dir=runtime)
    if cap.available:
        return {"status": "ok", "reason_code": None,
                "resolved_device": "cuda",
                "supported_compute_types": list(cap.supported_compute_types)}
    status = "error" if cap.reason_code in {REASON_RUNTIME_MISSING, REASON_RUNTIME_LOAD_ERROR,
        "runtime_integrity_error", "artifact_identity_missing", "artifact_identity_invalid"} else "skipped"
    return {"status": status, "reason_code": cap.reason_code, "detail": cap.detail,
            "resolved_device": "cpu"}
