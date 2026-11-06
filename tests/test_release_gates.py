"""Release-gate logic exercised locally with sparse/synthetic artifacts only."""
import importlib.util
import json
from pathlib import Path
import uuid

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_gates", ROOT / "packaging/release_gates.py")
assert SPEC is not None and SPEC.loader is not None
gates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gates)


@pytest.fixture
def size_inputs(tmp_path):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"synthetic installer")
    tree = tmp_path / "installed"
    tree.mkdir()
    (tree / "payload.txt").write_bytes(b"synthetic payload")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"expanded_install_bytes": 0,
        "reference_artifact": "synthetic fixture", "installer_limit_bytes": 2_000_000_000,
        "expanded_growth_limit_bytes": 1_800_000_000}))
    return installer, tree, baseline


def test_measures_actual_artifacts_without_updating_baseline(size_inputs):
    installer, tree, baseline = size_inputs
    original = baseline.read_bytes()
    result = gates.measure_size(*size_inputs)
    assert result["ok"]
    assert result["installer_bytes"] == installer.stat().st_size
    assert result["expanded_install_bytes"] == (tree / "payload.txt").stat().st_size
    assert baseline.read_bytes() == original


@pytest.mark.parametrize("arm", ["compressed", "expanded", "missing-baseline"])
def test_size_gate_fails_closed_and_requires_explicit_reference(size_inputs, arm):
    installer, tree, baseline = size_inputs
    if arm == "compressed":
        with installer.open("r+b") as stream:
            stream.truncate(2_000_000_001)  # sparse, never read/allocate gigabytes
    elif arm == "expanded":
        with (tree / "payload.txt").open("r+b") as stream:
            stream.truncate(1_800_000_001)
    else:
        data = json.loads(baseline.read_text())
        data["expanded_install_bytes"] = None
        baseline.write_text(json.dumps(data))
    assert not gates.measure_size(*size_inputs)["ok"]
    assert not gates.measure_size(*size_inputs, override=True)["ok"]
    approved = gates.measure_size(*size_inputs, override=True, approval_reference="synthetic product approval fixture")
    assert approved["ok"] and approved["override_applied"] and approved["issues"]


def test_size_cli_persists_failure_evidence_and_outputs(size_inputs, tmp_path, monkeypatch):
    installer, tree, _ = size_inputs
    outputs = tmp_path / "github-output"
    summary = tmp_path / "summary.json"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    assert gates.main(["--output", str(summary), "size", "--installer", str(installer),
        "--install-tree", str(tree), "--baseline", str(ROOT / "packaging/size-baseline.json")]) == 1
    assert json.loads(summary.read_text())["issues"] == ["reviewed_size_baseline_missing"]
    assert "expanded_install_bytes=" in outputs.read_text()


@pytest.mark.parametrize("changed", [None, "jobs.json", "logs/ci-sentinel.log", "whisper/models/ci-sentinel.marker", "preference", "runtime.dll"])
def test_sentinel_roundtrip_and_corruption_detection(tmp_path, changed):
    root = tmp_path / "state"
    org = "VidLift-CI-" + uuid.uuid4().hex
    manifest = gates.seed_sentinels(root, org)
    assert gates.verify_sentinels(root, manifest) == {"ok": True, "issues": [], "sentinels": 4}
    if changed == "preference":
        settings = gates._ci_settings(org)
        settings.setValue("subs_device", "cuda")
        settings.sync()
    elif changed:
        (root / changed).write_bytes(b"synthetic mutation")
    result = gates.verify_sentinels(root, manifest)
    assert result["ok"] == (changed is None)
    with pytest.raises(ValueError, match="existing"):
        gates.seed_sentinels(root, org)
    with pytest.raises(ValueError, match="CI-"):
        gates._ci_settings("YetiWurks")


def test_workflow_structure_and_nonpublishing_gates():
    workflow = yaml.load((ROOT / ".github/workflows/build-windows-installer.yml").read_text(), Loader=yaml.BaseLoader)
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    for name in ("enable_cuda_runtime", "nvidia_redistribution_approved", "size_gate_override"):
        assert inputs[name]["default"] == "false"
    build = workflow["jobs"]["build"]
    steps = {step["name"]: step for step in build["steps"] if "name" in step}
    acquire = steps["Acquire CUDA runtime (opt-in, gated)"]
    assert "inputs.enable_cuda_runtime && inputs.nvidia_redistribution_approved" in acquire["if"]
    assert "VIDLIFT_NVIDIA_REDISTRIBUTION_APPROVED" in acquire["env"]
    assert "NOT RUN (no GPU runner)" in steps["Record external gate disposition"]["run"]
    assert "tests/test_gui.py" not in steps["Test"]["run"]
    validate = steps["Validate installed artifact"]["run"]
    assert "prior-installer.exe" in validate and "Prior installer SHA-256 mismatch" in validate
    assert validate.index("release_gates.py --output output\\seed-sentinels.json") < validate.index("$process = Start-Process $setup")
    assert "release_gates.py --output output\\update-sentinels.json verify" in validate
    assert "No-GPU fallback must report an explicit CPU reason" in validate
    assert "size-baseline.json" in steps["Measure installer and expanded-install size gate"]["run"]
    gpu = workflow["jobs"]["gpu-gate"]
    assert gpu["needs"] == "build" and "inputs.gpu_runner != ''" in gpu["if"]
    assert build["outputs"]["version"] == "${{ steps.version.outputs.version }}"
    text = (ROOT / "packaging/gpu_gate.ps1").read_text()
    assert "device: cuda" in text and "CPU fallback is not a GPU pass" in text
    assert "--selftest" in text and "--device cuda" in text
