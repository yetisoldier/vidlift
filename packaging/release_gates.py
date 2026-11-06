"""Isolated release evidence helpers. No installation or network operations.

Size baseline is deliberate reviewed input, never auto-written. Sentinel mode
refuses non-CI Qt organisations and never resolves a user's default state root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def measure_size(installer, install_tree, baseline, *, override=False, approval_reference=""):
    baseline = json.loads(Path(baseline).read_text())
    compressed = Path(installer).stat().st_size
    tree = Path(install_tree)
    if not tree.is_dir():
        raise ValueError("Expanded installed tree missing")
    expanded = sum(p.stat().st_size for p in tree.rglob("*") if p.is_file())
    previous = baseline.get("expanded_install_bytes")
    issues = []
    if not isinstance(previous, int) or isinstance(previous, bool) or previous < 0 or not baseline.get("reference_artifact"):
        previous = None
        issues.append("reviewed_size_baseline_missing")
    growth = expanded - previous if previous is not None else None
    if compressed > baseline["installer_limit_bytes"]:
        issues.append("installer_limit_exceeded")
    if growth is not None and growth > baseline["expanded_growth_limit_bytes"]:
        issues.append("expanded_growth_limit_exceeded")
    approved_override = bool(override and approval_reference.strip())
    if override and not approved_override:
        issues.append("override_requires_approval_reference")
    return {"installer_bytes": compressed, "expanded_install_bytes": expanded,
            "baseline_expanded_bytes": previous, "expanded_growth_bytes": growth,
            "issues": issues, "override_applied": approved_override,
            "approval_reference": approval_reference if approved_override else None,
            "ok": not issues or approved_override}


def _ci_settings(organisation):
    if not organisation.startswith("VidLift-CI-"):
        raise ValueError("Sentinel preferences require a VidLift-CI- organisation")
    from PyQt6.QtCore import QSettings
    return QSettings(organisation, "VidLift")


def seed_sentinels(root, organisation):
    from vidlift.mcp_bridge.registry import Registry
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise ValueError("Refusing to seed over an existing state directory")
    settings = _ci_settings(organisation)
    settings.setValue("subs_device", "cpu")
    settings.sync()
    registry = Registry(root)
    record = registry.enqueue(kind="subs", paths=[str(root / "synthetic-input.mkv")], options={"device": "cpu"})
    registry.transition(record, "running")
    registry.transition(record, "succeeded")
    log = root / "logs/ci-sentinel.log"
    marker = root / "whisper/models/ci-sentinel.marker"
    for path, content in ((log, b"synthetic log sentinel\n"), (marker, b"synthetic model marker\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    paths = [registry.path, log, marker]
    manifest = {"files": {str(p.relative_to(root)): digest(p) for p in paths},
                "preference_bytes": json.dumps(settings.value("subs_device")),
                "organisation": organisation}
    return manifest


def verify_sentinels(root, manifest):
    root = Path(root)
    settings = _ci_settings(manifest["organisation"])
    settings.sync()
    issues = []
    for name, expected in manifest["files"].items():
        path = root / name
        if not path.is_file() or digest(path) != expected:
            issues.append("sentinel_changed:" + name)
    if json.dumps(settings.value("subs_device")) != manifest["preference_bytes"]:
        issues.append("preference_changed")
    if any(p.is_file() and p.suffix.lower() == ".dll" for p in root.rglob("*")):
        issues.append("runtime_dll_in_user_state")
    return {"ok": not issues, "issues": issues, "sentinels": len(manifest["files"]) + 1}


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    size = sub.add_parser("size")
    size.add_argument("--installer", required=True)
    size.add_argument("--install-tree", required=True)
    size.add_argument("--baseline", required=True)
    size.add_argument("--override", action="store_true")
    size.add_argument("--approval-reference", default="")
    for mode in ("seed", "verify"):
        command = sub.add_parser(mode)
        command.add_argument("--root", required=True)
        command.add_argument("--manifest", required=True)
        command.add_argument("--organisation", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "size":
        result = measure_size(args.installer, args.install_tree, args.baseline,
                              override=args.override, approval_reference=args.approval_reference)
    elif args.command == "seed":
        manifest = seed_sentinels(args.root, args.organisation)
        Path(args.manifest).write_text(json.dumps(manifest, indent=2))
        result = {"ok": True, "sentinels": len(manifest["files"]) + 1}
    else:
        result = verify_sentinels(args.root, json.loads(Path(args.manifest).read_text()))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, sort_keys=True))
    if args.command == "size" and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            for key in ("installer_bytes", "expanded_install_bytes", "expanded_growth_bytes"):
                stream.write(f"{key}={result[key]}\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    # The workflow calls this script from the checkout, not an installed package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
