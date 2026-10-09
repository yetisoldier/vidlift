"""Real-ESRGAN engine tool discovery and model hash pinning (clause g).

The ncnn engine artifacts are CONSUMED, never re-derived: the
realesrgan-x2plus param/bin pair was converted from the official
RealESRGAN_x2plus.pth (release v0.2.1) through the official
onnx2ncnn -> ncnnoptimize chain (with the documented pixel_unshuffle
fold) and verified against a PyTorch float64 reference plus the real
engine binary. Provenance and numeric evidence:
``/home/yetisoldier/projects/x2plus-ncnn-verified/VERIFICATION-REPORT.md``
(2026-10-04). The sha256 values below are pinned from that report and
verified at first use per process, BEFORE any frame is extracted
(design docs/designs/real-engine-adapter.md Section 9): a corrupted or
substituted model is never run silently.

Binary resolution mirrors ``vidlift.run.find_tools`` semantics
(roadmap packaging discipline): explicit option ->
``VIDLIFT_REALESRGAN_NCNN`` environment override -> bundled
``tools/realesrgan/`` location -> a named :class:`MissingToolError`
listing everything that was tried. Never a silent PATH guess mid-run.
Model dir resolution mirrors the sibling: explicit ->
``VIDLIFT_REALESRGAN_MODELS`` -> ``models/`` beside the binary.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

from .run import MissingToolError, UpscaleError

__all__ = [
    "ENGINE_TOOL_NAME",
    "MODEL_ARTIFACTS",
    "resolve_realesrgan",
    "resolve_realesrgan_models",
    "verify_model_artifacts",
]

ENGINE_TOOL_NAME = "realesrgan-ncnn-vulkan"

# The ONLY source of engine model artifact identity (clause g). sha256
# values pinned from x2plus VERIFICATION-REPORT.md (2026-10-04): the
# fp16 ncnn conversion of RealESRGAN_x2plus.pth v0.2.1 (a true native-2x
# RRDBNet; -s 2 IS its native mode). The animevideov3-x2 row gets pinned
# here the same way when the anime profile milestone opens.
MODEL_ARTIFACTS: dict[str, dict[str, tuple[int, str]]] = {
    "realesrgan-x2plus": {
        "param": (
            110_995,
            "f26dc4b8d7c467a86fac91f63d361a762ef5ab13ccb0521d3081b1b3152e9a6f",
        ),
        "bin": (
            33_434_888,
            "cfb044e3a1fe18750f54f7a3d8c2d49a296b54e2c9489c90513bbf88844e9e69",
        ),
    },
}

# First use per process verifies both hashes once (design Section 9);
# keyed on (models dir, model, expected digests) so distinct fixture
# tables in tests never alias each other.
_HASH_CACHE: set[tuple[str, str, str, str]] = set()


def _bundled_realesrgan_dirs() -> list[Path]:
    """Bundled engine locations, frozen-executable first (packaged app),
    then the repo/tools layout for development runs."""
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(Path(meipass) / "tools" / "realesrgan")
        dirs.append(Path(sys.executable).resolve().parent / "tools" / "realesrgan")
    dirs.append(Path(__file__).resolve().parent.parent / "tools" / "realesrgan")
    return dirs


def resolve_realesrgan(explicit: str | None = None) -> str:
    """Locate the realesrgan-ncnn-vulkan binary with a named error, never
    a silent guess. Resolution order (design Section 9): explicit option
    -> VIDLIFT_REALESRGAN_NCNN env -> bundled tools/realesrgan/."""
    binary = explicit or os.environ.get("VIDLIFT_REALESRGAN_NCNN")
    if binary:
        if shutil.which(binary):
            return binary
        raise MissingToolError(
            ENGINE_TOOL_NAME,
            f"{ENGINE_TOOL_NAME} not found or not executable at '{binary}': "
            "pass --realesrgan, fix the path, or clear VIDLIFT_REALESRGAN_NCNN",
        )
    tried: list[str] = []
    for directory in _bundled_realesrgan_dirs():
        candidate = directory / ENGINE_TOOL_NAME
        tried.append(str(candidate))
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise MissingToolError(
        ENGINE_TOOL_NAME,
        f"{ENGINE_TOOL_NAME} not found; tried bundled locations: "
        + "; ".join(tried)
        + ". Pass --realesrgan or set VIDLIFT_REALESRGAN_NCNN",
    )


def resolve_realesrgan_models(binary: str, explicit: str | None = None) -> str:
    """Locate the model dir: explicit -> VIDLIFT_REALESRGAN_MODELS env ->
    models/ beside the binary (the engine's own convention)."""
    candidate = explicit or os.environ.get("VIDLIFT_REALESRGAN_MODELS")
    if candidate:
        path = Path(candidate)
        if path.is_dir():
            return str(path)
        raise MissingToolError(
            "realesrgan-models",
            f"Real-ESRGAN model directory not found at '{candidate}': pass "
            "--realesrgan-models or fix VIDLIFT_REALESRGAN_MODELS",
        )
    beside = Path(binary).resolve().parent / "models"
    if beside.is_dir():
        return str(beside)
    raise MissingToolError(
        "realesrgan-models",
        "Real-ESRGAN models not found; expected a models folder beside "
        f"{binary} or set VIDLIFT_REALESRGAN_MODELS",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_model_artifacts(
    models_dir: str,
    model_name: str,
    *,
    artifacts: dict[str, dict[str, tuple[int, str]]] | None = None,
) -> bool:
    """Verify the pinned model hashes once per process (clause g).

    Checks size first, then sha256, for both the .param and .bin
    artifacts. Any mismatch raises a named :class:`UpscaleError` naming
    the file and both digests BEFORE the caller extracts a single frame;
    the fail-open path then states the mismatch as its reason. Success
    is cached per process (design Section 9: verify once, <1 s for the
    33 MB bin). ``artifacts`` injects a fixture table for tests; the
    default is the pinned :data:`MODEL_ARTIFACTS`.
    """
    table = artifacts if artifacts is not None else MODEL_ARTIFACTS
    expected = table.get(model_name)
    if expected is None:
        raise UpscaleError(
            f"no pinned artifacts for model '{model_name}'; refusing to run "
            "an unpinned engine model"
        )
    key = (
        str(models_dir),
        model_name,
        expected["param"][1],
        expected["bin"][1],
    )
    if key in _HASH_CACHE:
        return True
    for kind, (size, digest) in expected.items():
        path = Path(models_dir) / f"{model_name}.{kind}"
        if not path.is_file():
            raise UpscaleError(
                f"{model_name}.{kind} not found in {models_dir}; refusing to "
                "run the engine without its verified model"
            )
        actual_size = path.stat().st_size
        if actual_size != size:
            raise UpscaleError(
                f"{model_name}.{kind} size {actual_size} does not match the "
                f"pinned {size} bytes; refusing to run a substituted model"
            )
        actual = _sha256(path)
        if actual != digest:
            raise UpscaleError(
                f"{model_name}.{kind} sha256 mismatch: expected {digest}, "
                f"got {actual}; refusing to run a substituted model"
            )
    _HASH_CACHE.add(key)
    return True