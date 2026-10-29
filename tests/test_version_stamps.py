"""Release-gate: version-stamp parity across pyproject.toml and packaging/version_info.txt.

Guards against multi-stamp drift: the version bump commit for v0.4.11 missed
vidlift/__init__.py (the PyInstaller CLI built from that tree happily reported
``vidlift 0.4.<prev>`` while pyproject and the Windows version resource said 0.4.11).
These are pure-file assertions -- no subprocesses, no imports of packaging
machinery -- so they run identically on Linux and Windows CI.
"""

import re
from pathlib import Path

import vidlift

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
VERSION_INFO = REPO_ROOT / "packaging" / "version_info.txt"


def _pyproject_version() -> str:
    """Pull the [project] version straight from pyproject.toml text.

    Regex over the raw file keeps the assertion dependency-free and exact;
    a full TOML parse is fine cross-platform (tomllib is stdlib on 3.12,
    this repo's floor), but a flat regex is enough for a single stamp.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"\s*$', text)
    assert match, "pyproject.toml has no [project] version line"
    return match.group(1)


def _version_info_string(name: str) -> str:
    """Read a StringStruct value (e.g. FileVersion) from version_info.txt."""
    text = VERSION_INFO.read_text(encoding="utf-8")
    match = re.search(
        rf"StringStruct\(\s*'{name}'\s*,\s*u?'([^']*)'\s*\)", text
    )
    assert match, f"version_info.txt has no {name} StringStruct"
    return match.group(1)


def test_pyproject_version_matches_package_version():
    assert _pyproject_version() == vidlift.__version__, (
        "pyproject.toml version drifted from vidlift/__init__.py __version__"
    )


def test_version_info_file_version_matches_package_version():
    assert _version_info_string("FileVersion") == vidlift.__version__, (
        "packaging/version_info.txt FileVersion drifted from __version__"
    )


def test_version_info_product_version_matches_package_version():
    assert _version_info_string("ProductVersion") == vidlift.__version__, (
        "packaging/version_info.txt ProductVersion drifted from __version__"
    )