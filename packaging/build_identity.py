"""Embed validated artifact intent as a PyInstaller script, not loose data.

The temporary runtime hook is archived in the executable. Hashes detect
accidental payload corruption, not malicious replacement of the executable.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tomllib


def make_identity(artifact_kind, binaries, datas):
    if artifact_kind not in {"gui-onedir", "cli-onefile"}:
        raise ValueError("invalid artifact kind")
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    identity = {"identity_schema": 1, "artifact_kind": artifact_kind,
                "application_version": tomllib.loads(project.read_text())["project"]["version"],
                "build_reference": os.environ.get("GITHUB_SHA", "unpublished-local-build"),
                "cuda_bundled": bool(binaries), "inventory": {}}
    for source, destination in [*binaries, *datas]:
        path = Path(source)
        relative = (Path(destination) / path.name).as_posix()
        if relative in identity["inventory"]:
            raise ValueError("duplicate build inventory")
        identity["inventory"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if binaries and not any(name.startswith("licenses/") for name in identity["inventory"]):
        raise ValueError("enabled identity requires reviewed notices")
    if not binaries and datas:
        raise ValueError("CPU identity cannot carry CUDA data")
    return identity


def runtime_hook(identity, *, directory=None):
    fd, name = tempfile.mkstemp(prefix="vidlift-build-identity-", suffix=".py", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("# Frozen artifact intent. Generated after validated collection.\nimport sys\n")
        stream.write("sys._vidlift_build_identity = " + repr(identity) + "\n")
    return name
