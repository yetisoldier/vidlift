"""v0.4.22.0 scope-restore structural guards (spec §7.3.1).

Trim guards so the .20/.21 scope creep cannot silently regrow on the
0.4.22.0 tree:

1. the six cut production modules are ABSENT from disk and
   unimportable (find_spec None with the repo root off sys.path);
2. the §6.2 symbol blacklist greps clean over ``vidlift/``
   (implemented in-test, literal token list);
3. version four-file agreement at 0.4.22.0 (ADR-S4: ``__init__.py``,
   ``pyproject.toml``, ``packaging/version_info.txt``,
   ``packaging/vidlift.iss``);
4. the trimmed bridge client exposes NO submission-receipt surface and
   its ``enqueue_subs`` accepts the reviewed ``device`` kwarg (§5-F.2).

These are pure file-structure/introspection assertions — no Qt event
loop, no subprocesses, no network.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
import sys
from pathlib import Path

import vidlift

REPO_ROOT = Path(__file__).resolve().parent.parent

# §2.4's six cut production modules (packaging/deletion ledger mirrored
# in the Forge report; tests are covered by §7.2's absence list).
CUT_MODULE_NAMES = (
    "vidlift.cuda_runtime",
    "vidlift.transcription_watchdog",
    "vidlift.media_transaction",
    "vidlift.safe_files",
    "vidlift.ui.single_instance",
    "vidlift.mcp_bridge.registry_transactions",
)

# §6.2 blacklist — the exact token list from the binding spec, matched
# case-sensitively as identifiers over every ``vidlift/*.py`` file.
BLACKLIST_TOKENS = (
    "cuda_runtime",
    "transcription_watchdog",
    "supervise_worker",
    "supervise_with_cpu_retry",
    "media_transaction",
    "safe_files",
    "publish_new",
    "verify_mux",
    "registry_transactions",
    "prepare_submission",
    "enqueue_idempotent",
    "lookup_submission",
    "list_submissions",
    "abandon_submission",
    "submit_gui_job",
    "submission_recovery",
    "GuiSubmissionResult",
    "canonical_payload",
    "single_instance",
    "SingleInstanceGuard",
    "raise_for_single_instance",
    "release_gates",
    "build_identity",
    "pyinstaller_cuda",
    "acquire_cuda_runtime",
    "gpu_gate",
    "size-baseline",
    "freeze_support",
    "batch_id",
    "on_fallback",
    "_submission_attempts",
    "_recovery_receipts",
    "_receipt_recovery_error",
    "worker_exited",
    "WorkerTimeout",
    "WorkerExited",
    "WorkerCancelled",
    "transcription_batch_id",
)

# §6.2 explicitly keeps the gui.py PRIVATE helper ``_submit_gui_job``
# (only the CLIENT method ``submit_gui_job`` is cut): identifier-shaped
# matches that are part of the underscored helper name are not
# blacklist violations, so the scan anchors each token with
# identifier boundaries and ``submit_gui_job`` additionally requires a
# non-underscore predecessor.
BLACKLIST_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    # file -> allowed exception tokens
    "vidlift/gui.py": ("submit_gui_job",),
    # D-01 (Forge report): v0.4.15.2's pre-existing CUDA->CPU fallback
    # callback, present at .19 (7625cef) bytes and parity-anchored by
    # the never-edited tests/test_v04152_cuda_fallback.py. The intended
    # blacklist target — the watchdog fallback plumbing in
    # subs_job/client_local/cli/gui — stays gated here: any NEW
    # ``on_fallback`` outside these two .19 files fails this guard.
    "vidlift/transcribe.py": ("on_fallback",),
    "vidlift/run.py": ("on_fallback",),
}


def _identifier_pattern(token: str) -> re.Pattern[str]:
    """Match ``token`` as an identifier (not inside a longer name)."""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])")


def test_cut_modules_absent_from_disk():
    for name in CUT_MODULE_NAMES:
        relpath = name.replace(".", "/")
        module_file = REPO_ROOT / f"{relpath}.py"
        package_init = REPO_ROOT / relpath / "__init__.py"
        assert not module_file.exists(), f"cut module still on disk: {module_file}"
        assert not package_init.exists(), f"cut package still on disk: {package_init}"


def test_cut_modules_unimportable_with_repo_root_off_sys_path():
    saved = list(sys.path)
    try:
        sys.path[:] = [
            entry for entry in sys.path
            if os.path.abspath(entry or os.getcwd()) != str(REPO_ROOT)
        ]
        for name in CUT_MODULE_NAMES:
            spec = importlib.util.find_spec(name)
            assert spec is None, f"cut module still importable: {name}"
    finally:
        sys.path[:] = saved


def test_blacklist_greps_clean_over_vidlift():
    violations: list[str] = []
    source_files = sorted((REPO_ROOT / "vidlift").rglob("*.py"))
    assert source_files, "vidlift/ package not found"
    assert not (REPO_ROOT / "vidlift" / "media_transaction.py").exists()
    for path in source_files:
        relpath = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8", errors="replace")
        allowed = BLACKLIST_EXCEPTIONS.get(relpath, ())
        for token in BLACKLIST_TOKENS:
            if token in allowed:
                continue
            pattern = _identifier_pattern(token)
            if token == "submit_gui_job":
                # §6.2 keeps the underscored gui helper; a bare
                # ``submit_gui_job`` (or ``x.submit_gui_job``) is the cut
                # client method and must not exist.
                pattern = re.compile(
                    r"(?<![A-Za-z0-9_])(?<!_)submit_gui_job(?![A-Za-z0-9_])"
                )
            match = pattern.search(text)
            if match is not None:
                line_no = text[:match.start()].count("\n") + 1
                violations.append(f"{relpath}:{line_no}: {token}")
    assert violations == [], (
        "§6.2 blacklist violations in vidlift/: " + "; ".join(violations)
    )


def test_version_four_file_agreement():
    assert vidlift.__version__ == "0.4.24.0"

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"\s*$', pyproject)
    assert match, "pyproject.toml has no [project] version line"
    assert match.group(1) == "0.4.24.0"

    stamp = "0.4.24.0"
    info = (REPO_ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8")
    assert re.search(r"filevers=\(0, 4, 24, 0\)", info)
    assert re.search(r"prodvers=\(0, 4, 24, 0\)", info)
    assert re.search(r"StringStruct\('FileVersion',\s*u?'" + re.escape(stamp) + r"'\)", info)
    assert re.search(r"StringStruct\('ProductVersion',\s*u?'" + re.escape(stamp) + r"'\)", info)

    iss = (REPO_ROOT / "packaging" / "vidlift.iss").read_text(encoding="utf-8")
    assert re.search(r'(?m)^#define MyAppVersion "' + re.escape(stamp) + r'"$', iss)


def test_client_local_has_no_submission_receipt_surface():
    from vidlift.mcp_bridge.client_local import LocalBridgeClient

    assert not hasattr(LocalBridgeClient, "submit_gui_job")
    assert not hasattr(LocalBridgeClient, "submission_recovery")
    signature = inspect.signature(LocalBridgeClient.enqueue_subs)
    assert "device" in signature.parameters
    assert signature.parameters["device"].default == "auto"
    assert "batch_id" not in signature.parameters
