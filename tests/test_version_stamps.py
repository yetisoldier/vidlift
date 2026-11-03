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


def _version_info_tuple(name: str) -> tuple[int, int, int, int]:
    """Read a FixedFileInfo binary tuple (filevers/prodvers) from
    version_info.txt.

    v0.4.12 guard: the v0.4.11 bump edited BOTH string structs and BOTH
    byte tuples; this parser pins the tuples so a drift between the string
    forms and the binary manifest cannot escape again (the built exe
    reports the TUPLES to Explorer's details view; the strings feed the
    legacy view — both must agree)."""
    text = VERSION_INFO.read_text(encoding="utf-8")
    match = re.search(rf"{name}\s*=\s*\(([^)]*)\)", text)
    assert match, f"version_info.txt has no {name} tuple"
    parts = [p.strip() for p in match.group(1).split(",") if p.strip()]
    assert len(parts) == 4, f"{name} tuple is not a 4-part version: {parts}"
    return tuple(int(p) for p in parts)


def _expected_version_tuple() -> tuple[int, int, int, int]:
    """The 4-part byte tuple __version__ must stamp into version_info.txt.

    3-part versions (x.y.z) pad with a trailing 0 (the v0.4.12 rule).
    4-part hotfix versions (x.y.z.w — first: 0.4.15.1) map as-is."""
    parts = [int(part) for part in vidlift.__version__.split(".")]
    if len(parts) == 3:
        parts.append(0)
    assert len(parts) == 4, (
        f"__version__ {vidlift.__version__!r} does not carry 3 or 4 parts"
    )
    return tuple(parts)


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


def test_version_info_filevers_tuple_matches_package_version():
    """ALL FOUR version_info values (design §5: two strings + two tuples)
    must carry the package version — the tuple is what Explorer and the
    CLI smoke gate actually read off the built binary."""
    filevers = _version_info_tuple("filevers")
    expected = _expected_version_tuple()
    assert len(expected) == 4
    assert filevers == expected, (
        f"filevers {filevers} drifted from __version__ {vidlift.__version__}"
    )


def test_version_info_prodvers_tuple_matches_package_version():
    prodvers = _version_info_tuple("prodvers")
    expected = _expected_version_tuple()
    assert len(expected) == 4
    assert prodvers == expected, (
        f"prodvers {prodvers} drifted from __version__ {vidlift.__version__}"
    )


# ---------------------------------------------------------------------------
# v0.4.15.4 (F-9): the MCP serverInfo stamp derives from the package
# ---------------------------------------------------------------------------


def test_server_info_version_matches_package_version():
    """The MCP handshake's ``serverInfo.version`` used to be a
    hand-stamped literal ("0.2.0" against package 0.4.15.x) — exactly
    the multi-stamp drift class this gate was built for. SERVER_INFO
    now derives from __version__; this pin holds the derivation
    class-wide (a future hand-stamp anywhere in this family fails
    here). The module import lives inside the test so collection-time
    module cost stays out of every other stamp check (the file's
    existing style)."""
    from vidlift.mcp_bridge.server import SERVER_INFO

    assert SERVER_INFO["version"] == vidlift.__version__, (
        "the MCP serverInfo version drifted from vidlift.__version__"
    )
    assert SERVER_INFO["name"] == "vidlift"