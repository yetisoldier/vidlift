"""Real offline, non-editable distribution smoke, never a GUI launch."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


def test_noneditable_wheel_contains_runtime_packages(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "input"
    source.mkdir()
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(root / name, source / name)
    shutil.copytree(root / "vidlift", source / "vidlift", ignore=shutil.ignore_patterns("__pycache__"))
    wheels = tmp_path / "wheels"
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-build-isolation",
                    "--no-deps", "--no-index", "-w", str(wheels), str(source)],
                   check=True, capture_output=True, text=True)
    wheel, = wheels.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "vidlift/ui/review.py" in names
        assert "vidlift/mcp_bridge/client_local.py" in names
        assert "vidlift/gpu_arbitration.py" in names
    target = tmp_path / "installed"
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
                    "--target", str(target), str(wheel)], check=True, capture_output=True)
    # -I ignores source/user PYTHONPATH. Dependencies use their already installed
    # locations, not an editable VidLift checkout. No dependency acquisition.
    dependency_paths = [p for p in sys.path if "site-packages" in p]
    script = f"""
import sys, importlib, importlib.metadata, pathlib, json
sys.path[:0] = {[str(target)] + dependency_paths!r}
modules = ['vidlift', 'vidlift.ui', 'vidlift.ui.widgets', 'vidlift.ui.gpu_meter',
           'vidlift.ui.drop_area', 'vidlift.ui.preflight', 'vidlift.ui.review',
           'vidlift.ui.view_state', 'vidlift.mcp_bridge',
           'vidlift.mcp_bridge.client_local', 'vidlift.mcp_bridge.registry',
           'vidlift.gpu_arbitration']
for name in modules:
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).is_relative_to({str(target)!r}), module.__file__
# v0.4.22.0 scope restore (spec §5-H.3): the cut modules must not ride the wheel.
cut_modules = ['vidlift.cuda_runtime', 'vidlift.transcription_watchdog',
               'vidlift.media_transaction', 'vidlift.safe_files',
               'vidlift.ui.single_instance', 'vidlift.mcp_bridge.registry_transactions']
for name in cut_modules:
    assert importlib.util.find_spec(name) is None, name + " must not be shipped"
entries = list(importlib.metadata.distribution('vidlift').entry_points)
assert {{e.name for e in entries}} == {{'vidlift', 'vidlift-cli', 'vidlift-gui'}}
for entry in entries:
    assert callable(entry.load())
sys.argv = ['vidlift', '--help']
import vidlift.cli
vidlift.cli.main()
"""
    result = subprocess.run([sys.executable, "-I", "-c", script], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
