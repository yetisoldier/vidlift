r"""v0.4.16.2 VAD packaging tests (arch/impl specs bob-vidlift-v0.4.16.2, ADR-1..7).

The defect being pinned: the v0.4.16.1 Windows installer could not
transcribe ANY file — both PyInstaller specs carried ``datas=[]``, so
faster-whisper's packaged ``assets/`` payload (its bundled silero VAD
ONNX set) never reached the frozen tree and every job died minutes in
with ``NO_SUCHFILE … _internal\faster_whisper\assets\<version-suffixed VAD leaf>``.
The Linux suite stayed green because its world genuinely HAS the asset
— the suite's documented rule (N3) is it must never import
``faster_whisper``, so these tests assert everything import-free
(find_spec + filesystem + text contracts + injected seams).

House rules honored: pure-file/monkeypatch assertions (no subprocess
PyInstaller runs), no conftest edits, no network, Windows-only
behaviors exercised cross-platform (paths always derive from tmp_path /
os conventions — never a hardcoded ``/``-rooted round trip; work is
asserted via filesystem state and the RUNNER call stream, never a
captured ``os.path.exists`` stream).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import wave
from pathlib import Path

import pytest

import vidlift.cli as cli
import vidlift.transcribe as transcribe_mod
from vidlift.monitor import API_VERSION, StatusView, status_payload
from vidlift.transcribe import (
    TranscriptSegment,
    TranscriptionError,
    VadAssetMissingError,
    WordStamp,
    _is_vad_asset_fault,
    _vad_missing_message,
    make_whisper_engine_with_fallback,
    selftest_report,
    transcribe_source_to_srt,
    vad_asset_report,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
GUI_SPEC = REPO_ROOT / "vidlift_gui.spec"
CLI_SPEC = REPO_ROOT / "vidlift_cli.spec"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-windows-installer.yml"

# N3 probe ONLY — find_spec never imports faster_whisper (verified: the
# name stays out of sys.modules afterwards), and a top-level package
# carries no parent that the lookup could import either side of it.
_FW_SPEC = importlib.util.find_spec("faster_whisper")
_fw_installed = pytest.mark.skipif(
    _FW_SPEC is None, reason="faster_whisper not installed on this box"
)


# ---------------------------------------------------------------------------
# §7.1 — M1 spec contracts (the release-path packaging fix, as text)
# ---------------------------------------------------------------------------


def test_gui_spec_collects_faster_whisper_data():
    """The v0.4.16.1 shipping defect, pinned where it lived: the GUI spec
    must collect faster-whisper's package data (WHOLE package — rename-
    and relocation-immune, ADR-1) without ever naming a version-suffixed
    leaf. This test would have caught the v0.4.16.1 shipping defect
    because ``datas=[]`` fails every assertion here."""
    text = GUI_SPEC.read_text(encoding="utf-8")
    assert 'collect_data_files("faster_whisper")' in text
    assert "datas=collect_data_files(" in text
    assert "datas=[]" not in text
    # ADR-6: the release path never pins the version-suffixed leaf.
    assert "silero" not in text.lower()


def test_cli_spec_collects_faster_whisper_data():
    """Same contract on the one-file CLI spec — the CLI is the lane the
    CI gate actually drives (``--selftest``), a CLI build without the
    payload would still ship a broken product."""
    text = CLI_SPEC.read_text(encoding="utf-8")
    assert 'collect_data_files("faster_whisper")' in text
    assert "datas=collect_data_files(" in text
    assert "datas=[]" not in text
    assert "silero" not in text.lower()


def test_both_specs_parity():
    """ADR-2: the collector delta is ONE identical line in EACH spec —
    the two ``datas=`` lines must match modulo whitespace so neither
    lane can drift from the other."""
    gui_lines = [
        ln.strip()
        for ln in GUI_SPEC.read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith("datas=")
    ]
    cli_lines = [
        ln.strip()
        for ln in CLI_SPEC.read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith("datas=")
    ]
    assert len(gui_lines) == 1 and len(cli_lines) == 1
    assert gui_lines[0] == cli_lines[0]
    assert "faster_whisper" in gui_lines[0]


# ---------------------------------------------------------------------------
# §7.2 — checker unit tests (the import-free truth surface, M2)
# ---------------------------------------------------------------------------

_REPORT_KEYS = {
    "package",
    "package_version",
    "vad_asset",
    "assets_dir",
    "onnx_files",
    "onnx_count",
    "resolver_source",
    "detail",
    "checked_at",
}


def test_report_present_lists_only_onnx_files(tmp_path):
    """present: seeds one real asset dir (plus a decoy .txt and a
    DIRECTORY whose name ends in .onnx) — the glob must see only regular
    ``*.onnx`` FILES, sorted, count honest. This test would have caught
    the v0.4.16.1 shipping defect because on the frozen install the
    report would have read ``missing`` where a healthy build reads
    ``present``; the checker is the tripwire the suite can actually
    exercise on Linux."""
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "b.onnx").write_bytes(b"\x00")
    (assets / "a.onnx").write_bytes(b"\x00")
    (assets / "decoy.txt").write_text("not a model", encoding="utf-8")
    (assets / "not_a_file.onnx").mkdir()  # directory with a .onnx name
    report = vad_asset_report(lambda: (str(assets), "find-spec"))
    # exact key schema (the monitor envelope serves this verbatim):
    assert set(report) == _REPORT_KEYS
    assert report["package"] == "faster-whisper"
    assert report["vad_asset"] == "present"
    assert report["onnx_files"] == ["a.onnx", "b.onnx"]  # sorted, files only
    assert report["onnx_count"] == 2
    assert report["assets_dir"] == str(assets)
    assert report["resolver_source"] == "injected"  # injected => labeled
    assert report["detail"] is None  # present carries no complaint
    assert isinstance(report["checked_at"], float)


def test_report_missing_when_assets_dir_has_no_onnx(tmp_path):
    """missing: a PRESENT dir with zero ``*.onnx`` — exactly the frozen
    v0.4.16.1 shape (the package dir exists; the payload never landed).
    The detail names the emptiness, not a device fault."""
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "readme.txt").write_text("payload never shipped", encoding="utf-8")
    report = vad_asset_report(lambda: (str(assets), "find-spec"))
    assert report["vad_asset"] == "missing"
    assert report["detail"] == "no *.onnx under assets dir"
    assert report["onnx_files"] == []
    assert report["onnx_count"] == 0


def test_report_missing_when_assets_dir_absent(tmp_path):
    """missing: the dir itself is absent — the honest KNOWN-empty shape
    (we know zero *.onnx exist; it is not 'unknown')."""
    report = vad_asset_report(
        lambda: (str(tmp_path / "never-shipped"), "find-spec")
    )
    assert report["vad_asset"] == "missing"
    assert report["detail"] == "assets dir absent"
    assert report["onnx_files"] == []
    assert report["onnx_count"] == 0
    assert report["assets_dir"] == str(tmp_path / "never-shipped")


def test_report_unknown_when_resolver_cannot_resolve():
    """unknown (N5): an unresolvable package must never read as
    ``missing`` — a probe failure is not a proven absence, and the
    envelope's ``ready_for_transcription`` rides on that honesty."""
    report = vad_asset_report(lambda: (None, "unavailable"))
    assert report["vad_asset"] == "unknown"
    assert report["assets_dir"] is None
    assert report["onnx_files"] is None
    assert report["onnx_count"] is None
    assert report["resolver_source"] == "injected"
    assert "unavailable" in (report["detail"] or "")


def test_report_unknown_when_listing_faults(tmp_path):
    """unknown (N5): the resolver points at a FILE — the listing itself
    faults (NotADirectoryError on native paths); the report must name
    the fault and never claim missing on uncertainty."""
    blocker = tmp_path / "a-regular-file"
    blocker.write_text("x", encoding="utf-8")
    report = vad_asset_report(lambda: (str(blocker), "find-spec"))
    assert report["vad_asset"] == "unknown"
    assert report["onnx_files"] is None
    assert report["onnx_count"] is None
    assert (report["detail"] or "").startswith("probe failed:")


@_fw_installed
def test_default_resolver_shape_dev_box():
    """On a properly provisioned dev/CI box the DEFAULT resolver (no
    injection) resolves find-spec + a real ``assets`` dir — the
    hermeticity footprint (one find_spec + one listing) the validation
    spec documents. NO import of faster_whisper happens here."""
    assets_dir, source = transcribe_mod._default_vad_assets_dir()
    assert source == "find-spec"
    assert assets_dir and Path(assets_dir).name == "assets"
    assert Path(assets_dir).is_dir()
    # THE N3 PROOF: find_spec resolves; the package never imports.
    assert "faster_whisper" not in sys.modules


def test_selftest_report_skip_shape_under_env_guard():
    """Under the suite-wide engine-off guard (conftest pins
    VIDLIFT_TRANSCRIPTION_ENGINE=0) the selftest returns the explicit
    skip shape — ok:False (a CI gate treats skip as FAILURE, by
    construction), checks empty, and — the N3 seam proof — the REAL
    function ran and imported NOTHING."""
    report = selftest_report()
    assert report["ok"] is False
    assert "VIDLIFT_TRANSCRIPTION_ENGINE" in (report.get("skipped") or "")
    assert report["checks"] == {}
    assert report["errors"] == []
    assert "faster_whisper" not in sys.modules


# ---------------------------------------------------------------------------
# §7.3 — the pinned asset record (ADR-6 tripwire; import-free, N3-safe)
# ---------------------------------------------------------------------------

EXPECTED_VAD_ASSETS = {
    # faster-whisper 1.2.1 (recorded 2026-11-03; the leaf is version-suffixed
    # upstream — when the faster-whisper floor moves and this trips, refresh
    # from the NEW wheel; never edit this record to silence it):
    #   python - <<'EOF'  (in an env with the new faster-whisper)
    #   import importlib.util, hashlib, pathlib
    #   d = pathlib.Path(list(importlib.util.find_spec("faster_whisper").submodule_search_locations)[0]) / "assets"
    #   for p in sorted(d.glob("*.onnx")): print(p.name, p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest())
    #   EOF
    "silero_vad_v6.onnx": (
        1245151,
        "4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2",
    ),
}


def test_record_has_no_stale_placeholder():
    """A guard against the empty-dict 'fix': the record holds at least
    one real asset entry (someone gutting the tripwire to silence it
    fails HERE, not at release)."""
    assert len(EXPECTED_VAD_ASSETS) >= 1
    for name, (size, digest) in EXPECTED_VAD_ASSETS.items():
        assert name.endswith(".onnx") and size > 0
        assert len(digest) == 64


@_fw_installed
def test_vad_asset_record_matches_installed_package():
    """ADR-6's tripwire, verified import-free (find_spec + filesystem +
    hashlib — faster_whisper stays unimported): the installed package's
    ``*.onnx`` set must EQUAL the recorded set, each file matching on
    size AND sha256. This would have caught upstream leaf drift the day
    it shipped, loudly, with refresh instructions beside the constant."""
    assets_dir = Path(list(_FW_SPEC.submodule_search_locations)[0]) / "assets"
    onnx_files = sorted(assets_dir.glob("*.onnx"))
    assert {p.name for p in onnx_files} == set(EXPECTED_VAD_ASSETS), (
        "upstream asset drift — refresh EXPECTED_VAD_ASSETS from the NEW "
        "wheel (see the constant's comment); never edit to silence"
    )
    for asset in onnx_files:
        expected_size, expected_digest = EXPECTED_VAD_ASSETS[asset.name]
        assert asset.stat().st_size == expected_size, asset.name
        assert (
            hashlib.sha256(asset.read_bytes()).hexdigest() == expected_digest
        ), asset.name
    assert "faster_whisper" not in sys.modules  # N3 held end to end


# ---------------------------------------------------------------------------
# §7.4 — core preflight + backstop predicate + fallback carve-out
# ---------------------------------------------------------------------------


def _silence_wav(tmp_path):
    """16 kHz mono wav, 100 ms of silence — the core-scaffold fixture
    shape (test_v04152_cuda_fallback.py); deterministic, stdlib-only."""
    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    return wav


def _wav_creating_runner(wav_path, calls):
    """A runner stub that mimics the ffmpeg extractor's effect (creates
    the 16k mono wav at the target path) and RECORDS each call into
    ``calls`` (the work-stream assertion sink)."""

    def _run(binary, args):
        calls.append((binary, args))
        target = str(wav_path)
        dir_of = str(Path(target).parent)
        if dir_of:
            Path(dir_of).mkdir(parents=True, exist_ok=True)
        with wave.open(target, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 1600)
        return (0, "", "")

    return _run


class _StubEngine:
    """Shape-compatible engine stand-in producing one real cue (the
    v0.4.14/15.2 test idiom) — the core's full happy path completes."""

    def __init__(self, model_path, *, device="auto", cpu_threads=4, vad_on=True):
        self.model_path = model_path
        self.device = device

    def detect_language(self, audio_path, *, offset_seconds=0.0):
        from vidlift.transcribe import LanguageGuess

        return LanguageGuess(code="en", confidence=0.9)

    def transcribe_segments(self, audio_path, *, language, on_segment=None):
        segs = [
            TranscriptSegment(
                start=0.5,
                end=1.5,
                text=" Hello there.",
                words=[
                    WordStamp(0.5, 1.0, "Hello", 0.95),
                    WordStamp(1.1, 1.5, "there.", 0.95),
                ],
            ),
        ]
        for s in segs:
            if on_segment is not None:
                on_segment(s)
        return segs


def _complete_core(monkeypatch, tmp_path, **overrides):
    """Drive transcribe_source_to_srt to COMPLETION through the stub
    seams (resolve_model_dir patched to "m"; injected engine_factory).
    Used by the preflight-skip/lenient tests: assertions stay on the
    completed run, environment-proof on every box."""
    monkeypatch.setattr(
        transcribe_mod, "resolve_model_dir", lambda pin, **kw: "m"
    )
    calls: list = []
    runner = _wav_creating_runner(tmp_path / "work" / "source-audio-16k.wav", calls)
    kwargs = dict(
        output_path=str(tmp_path / "out.mp4"),
        workdir=str(tmp_path / "work"),
        ffmpeg="ffmpeg",
        runner=runner,
        engine_factory=lambda model_dir: _StubEngine(model_dir),
    )
    kwargs.update(overrides)
    result = transcribe_source_to_srt(str(_silence_wav(tmp_path)), **kwargs)
    return result, calls


@pytest.mark.usefixtures("transcription_engine_on")
def test_core_preflight_missing_raises_before_any_work(monkeypatch, tmp_path):
    """ADR-3's second-zero refusal: with the engine ON (guard cleared)
    and an injected missing-shape report, the core refuses BEFORE any
    ffmpeg/model work — the runner records ZERO calls and the workdir is
    never created. This test would have caught the v0.4.16.1 shipping
    defect by proving a defective install fails FAST with the remedy
    instead of dying minutes in after a wasted download."""
    calls: list = []
    runner = _wav_creating_runner(tmp_path / "work" / "source-audio-16k.wav", calls)
    with pytest.raises(VadAssetMissingError) as excinfo:
        transcribe_source_to_srt(
            str(_silence_wav(tmp_path)),
            output_path=str(tmp_path / "out.mp4"),
            workdir=str(tmp_path / "work"),
            ffmpeg="ffmpeg",
            runner=runner,
            vad_report={"vad_asset": "missing", "assets_dir": str(tmp_path)},
        )
    message = str(excinfo.value)
    assert str(tmp_path) in message  # the resolved dir is named
    assert "reinstall" in message  # the remedy is named
    assert calls == []  # ZERO ffmpeg calls: preflight precedes work
    assert not (tmp_path / "work").exists()  # zero side effects on refusal


def test_core_preflight_skips_under_env_guard(monkeypatch, tmp_path):
    """The suite-default env guard (VIDLIFT_TRANSCRIPTION_ENGINE=0) skips
    the preflight ENTIRELY: with a missing-shaped report injected,
    control reaches the pre-existing engine-off refusal at
    resolve_model_dir — pinned verbatim. (Production never sets the
    variable; this is the suite's determinism seam, not a production
    behavior change.)"""
    with pytest.raises(TranscriptionError, match="engine disabled by env"):
        transcribe_source_to_srt(
            str(_silence_wav(tmp_path)),
            output_path=str(tmp_path / "out.mp4"),
            workdir=str(tmp_path / "work"),
            ffmpeg="ffmpeg",
            runner=_wav_creating_runner(
                tmp_path / "work" / "source-audio-16k.wav", []
            ),
            vad_report={"vad_asset": "missing", "assets_dir": str(tmp_path)},
        )


@pytest.mark.usefixtures("transcription_engine_on")
def test_core_preflight_skips_when_vad_off(monkeypatch, tmp_path):
    """N6/ADR-3: a no-VAD run (vad_on=False) genuinely needs no VAD asset
    — a vad_filter=False engine never touches the ONNX. With a
    missing-shaped report injected the preflight must NOT fire and the
    run completes cheaply (cues land). The existing vad_on seam is the
    ONLY switch — this is true minimal-dependency truth."""
    result, calls = _complete_core(
        monkeypatch,
        tmp_path,
        vad_on=False,
        vad_report={"vad_asset": "missing", "assets_dir": str(tmp_path)},
    )
    assert result.cue_count >= 1  # the run completes
    assert calls  # extraction really happened (the runner ran)


@pytest.mark.usefixtures("transcription_engine_on")
def test_core_preflight_unknown_is_lenient(monkeypatch, tmp_path):
    """N5: 'unknown' (a probe fault or unresolvable package) PROCEEDS —
    the engine's own load decides; a probe failure is not a proven
    absence and must never refuse a healthy run."""
    result, _calls = _complete_core(
        monkeypatch,
        tmp_path,
        vad_report={"vad_asset": "unknown", "assets_dir": None},
    )
    assert result.cue_count >= 1


@pytest.mark.usefixtures("transcription_engine_on")
def test_core_preflight_default_resolver_present_dev_box(monkeypatch, tmp_path):
    """The guard-off REAL filesystem behavior: no injected report, no
    monitor, the default resolver answers and the run completes on THIS
    box (asset present). On any box lacking the package the resolver
    reads unknown → lenient → completes too, so the single completion
    assertion is environment-proof (never a new failure surface)."""
    result, _calls = _complete_core(monkeypatch, tmp_path)
    assert result.cue_count >= 1
    assert "faster_whisper" not in sys.modules


def test_vad_missing_message_template(tmp_path):
    """The ONE message template (the preflight and the decode backstop
    share it): it names the resolved dir, the expectation, and the
    remedy — and requires no version-suffixed leaf to state the need."""
    message = _vad_missing_message({"assets_dir": str(tmp_path)})
    assert str(tmp_path) in message
    assert "reinstall/repair" in message
    assert "*.onnx" in message
    unresolved = _vad_missing_message({"assets_dir": None})
    assert "(unresolved)" in unresolved


# ---- the pure decode-fault predicate ----------------------------------------


def _fault_message_named(assets_dir: Path) -> str:
    """A faster_whisper 1.2.1-shaped onnxruntime fault naming the KNOWN
    assets dir (path derived from tmp_path — native separators, never a
    hardcoded slash layout)."""
    leaf = assets_dir / "shipped-vad.onnx"
    return (
        "ONNXRuntimeError : 3 : NO_SUCHFILE : Load model from "
        f"{leaf} failed: Load model ... failed. File doesn't exist"
    )


def test_fault_predicate_matches_known_assets_dir(tmp_path):
    """The version-stable TOCTOU backstop: an exception whose message
    names the KNOWN assets dir IS an asset fault — matched on the path
    string, never on onnxruntime's exception TYPE (whose hierarchy varies
    by version; on 1.31.x NoSuchFile derives straight from Exception)."""
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    exc = RuntimeError(_fault_message_named(assets_dir))
    assert _is_vad_asset_fault(exc, str(assets_dir)) is True


def test_fault_predicate_guards_and_negatives(tmp_path):
    """The predicate's discipline: an empty/unknown assets dir NEVER
    matches (a vad_filter=False engine must not transform faults), an
    unrelated message never matches, and matching is a case-sensitive
    exact substring (documented: no normalization, no fuzzy wins)."""
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    named = _fault_message_named(assets_dir)
    assert _is_vad_asset_fault(RuntimeError(named), "") is False
    assert _is_vad_asset_fault(RuntimeError("unrelated device fault"), str(assets_dir)) is False
    assert _is_vad_asset_fault(
        RuntimeError(named.upper()), str(assets_dir)
    ) is False  # case-sensitive: an uppercased path is a different fact


def test_vad_missing_error_is_transcription_error():
    """The disposition contract: Mode A's fail-open and Mode B's
    job-failure ride the EXISTING ``except TranscriptionError`` arms —
    the carve-out (ADR-7) re-raises on the same base class. An
    isinstance pin, not a courtesy."""
    assert issubclass(VadAssetMissingError, TranscriptionError)


# ---- _FallbackEngine decode arms (ADR-7 carve-out) ---------------------------


class _FirstCallFaulty:
    """Healthy CUDA construction; the FIRST transcribe_segments (or
    detect_language) call raises the configured exception — the tier-2
    gate shapes from test_v04152_cuda_fallback.py, parameterized on the
    fault so the carve-out and its negative control share one fake."""

    def __init__(self, model_path, *, device="auto", cpu_threads=4,
                 vad_on=True, fault=None, fail_in="transcribe"):
        self.device = device
        self.model = "MODEL" if device == "cuda" else None
        self.calls: list[str] = []
        self._fault = fault
        self._fail_in = fail_in

    def detect_language(self, audio_path, *, offset_seconds=0.0):
        from vidlift.transcribe import LanguageGuess

        self.calls.append("detect")
        if self._fail_in == "detect":
            raise self._fault
        return LanguageGuess(code="en", confidence=0.9)

    def transcribe_segments(self, audio_path, *, language, on_segment=None):
        self.calls.append("transcribe")
        if self._fail_in == "transcribe":
            raise self._fault
        return [
            TranscriptSegment(
                start=0.5,
                end=1.5,
                text=" Hello there.",
                words=[WordStamp(0.5, 1.0, "Hello", 0.95),
                       WordStamp(1.1, 1.5, "there.", 0.95)],
            ),
        ]


@pytest.mark.usefixtures("transcription_engine_on")
def test_fallback_engine_reraises_transcription_error_from_first_decode(
    monkeypatch, capsys
):
    """ADR-7: a VadAssetMissingError from the FIRST DECODE is an honest
    refusal, never a device guess — NO cpu ctor call, the exception is
    the ORIGINAL object, no fallback note, no stderr WARNING. This test
    would have caught the v0.4.16.1 defect's dishonesty gap (a missing
    asset on a CUDA box triggering a wasted CPU rebuild plus a false
    'cuda unavailable' manifest note)."""
    built: list[str] = []
    disposed: list[str] = []
    notes: list[str] = []
    fault = VadAssetMissingError("vad asset missing from this install")

    def ctor(model_path, *, device="auto", cpu_threads=4, vad_on=True):
        built.append(device)
        return _FirstCallFaulty(
            model_path, device=device, fault=fault, fail_in="transcribe"
        )

    engine = make_whisper_engine_with_fallback(
        "m",
        resolved_device="cuda",
        ctor=ctor,
        on_fallback=notes.append,
        on_dispose=disposed.append,
    )
    assert engine.device == "cuda"
    with pytest.raises(VadAssetMissingError) as excinfo:
        engine.transcribe_segments("wav", language="en")
    assert excinfo.value is fault  # the ORIGINAL refusal, not a rewrap
    assert built == ["cuda"]  # NO cpu rebuild for an honest refusal
    assert disposed == []
    assert notes == []  # no fabricated cuda-unavailable note
    assert capsys.readouterr().err == ""  # no fabricated WARNING


@pytest.mark.usefixtures("transcription_engine_on")
def test_fallback_engine_reraises_transcription_error_from_first_detect(
    monkeypatch, capsys
):
    """Same carve-out at the DETECT arm (its own one-shot gate):
    an honest TranscriptionError raises through with no rebuild, no
    note, no forensics — the wrapper never guesses a device from a
    refusal, on either first call."""
    built: list[str] = []
    notes: list[str] = []
    disposed: list[str] = []
    fault = VadAssetMissingError("vad asset missing from this install")

    def ctor(model_path, *, device="auto", cpu_threads=4, vad_on=True):
        built.append(device)
        return _FirstCallFaulty(
            model_path, device=device, fault=fault, fail_in="detect"
        )

    engine = make_whisper_engine_with_fallback(
        "m",
        resolved_device="cuda",
        ctor=ctor,
        on_fallback=notes.append,
        on_dispose=disposed.append,
    )
    with pytest.raises(VadAssetMissingError) as excinfo:
        engine.detect_language("wav")
    assert excinfo.value is fault
    assert built == ["cuda"]
    assert notes == []
    assert disposed == []
    assert capsys.readouterr().err == ""


@pytest.mark.usefixtures("transcription_engine_on")
def test_fallback_engine_still_rebuilds_once_on_runtime_error(
    monkeypatch, capsys
):
    """The negative control pinning the carve-out did NOT widen: a plain
    RuntimeError at the first decode keeps the v0.4.15.2 rescue — one
    cpu rebuild, the honest note, the cuda wording. (The decode-arm
    fakes in test_v04152_cuda_fallback.py raise RuntimeError/OOM shapes
    — verified unaffected by this change.)"""
    built: list[str] = []
    notes: list[str] = []

    def ctor(model_path, *, device="auto", cpu_threads=4, vad_on=True):
        built.append(device)
        if device == "cuda":
            return _FirstCallFaulty(
                model_path,
                device=device,
                fault=RuntimeError("CUDA error: out of memory on the first pull"),
            )
        return _StubEngine(model_path, device=device)

    engine = make_whisper_engine_with_fallback(
        "m",
        resolved_device="cuda",
        ctor=ctor,
        on_fallback=notes.append,
    )
    segs = engine.transcribe_segments("wav", language="en")
    assert [s.text for s in segs] == [" Hello there."]
    assert built == ["cuda", "cpu"]  # exactly ONE rebuild (behavior unchanged)
    assert notes == ["cuda unavailable — fell back to cpu (RuntimeError)"]
    assert "falling back to CPU (int8)" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# §7.5 — surfaces (CLI --selftest dispatch; the monitor envelope key)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("report", "expected_exit"),
    [
        ({"ok": True, "checks": {"x": 1}, "errors": []}, 0),
        ({"ok": False, "checks": {}, "errors": ["TypeError: boom"]}, 1),
    ],
)
def test_cli_selftest_flag_dispatch(monkeypatch, capsys, report, expected_exit):
    """ADR-4: ``--selftest`` is a FLAG dispatched FIRST in main() —
    before any subcommand chain — printing ``vidlift-selftest {json}``
    on stdout and exiting 0 pass / 1 fail. The suite drives main() with
    the report seam monkeypatched (the real report never imports under
    the guard). This test would have caught the v0.4.16.1 shipping
    defect by giving CI a binary that answers 'can you transcribe'
    with an exit code the gate can fail on."""
    monkeypatch.setattr(
        "vidlift.transcribe.selftest_report", lambda: dict(report)
    )
    exit_code = cli.main(["--selftest"])
    captured = capsys.readouterr()
    assert exit_code == expected_exit
    assert captured.out.startswith("vidlift-selftest ")
    payload = json.loads(captured.out[len("vidlift-selftest ") :])
    assert payload["ok"] is report["ok"]
    assert captured.err == ""  # no dispatch side effects anywhere else
    # the flag is a flag, not a subcommand: command stays unset.
    assert cli.build_parser().parse_args(["--selftest"]).command is None


def _present_report(assets_dir: str) -> dict:
    """A fixed present-shape report for envelope tests (generic leaf
    name — the version-suffixed leaf lives only in the pinned record)."""
    return {
        "package": "faster-whisper",
        "package_version": "1.2.1",
        "vad_asset": "present",
        "assets_dir": assets_dir,
        "onnx_files": ["shipped-vad.onnx"],
        "onnx_count": 1,
        "resolver_source": "find-spec",
        "detail": None,
        "checked_at": 1793739337.5,
    }


@pytest.fixture
def monitor_client(tmp_path, monkeypatch):
    """The real bridge client over a tmp registry (test_monitor.py's
    scaffolding shape — the GUI FakeClient seam is not the API truth)."""
    from vidlift.mcp_bridge.client_local import LocalBridgeClient

    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    yield client
    client.stop()


def test_monitor_envelope_carries_transcription_engine(
    monitor_client, monkeypatch, tmp_path
):
    """ADR-5/V-6: the /status envelope gains ``transcription_engine``
    (additive — ``api_version`` stays 1), with the machine-scoped
    ``ready_for_transcription: true`` on a present report. Hermes's
    capability query rides this key with an idle queue. This test would
    have caught the v0.4.16.1 shipping defect by making the packaging
    fault VISIBLE before anyone enqueues a job."""
    monkeypatch.setattr(
        "vidlift.transcribe.vad_asset_report",
        lambda: _present_report(str(tmp_path / "assets")),
    )
    payload = status_payload(StatusView(monitor_client))
    block = payload["transcription_engine"]
    assert block["vad_asset"] == "present"
    assert block["ready_for_transcription"] is True
    assert block["package"] == "faster-whisper"
    assert payload["api_version"] == API_VERSION == 1  # additive-only pin


def test_monitor_envelope_missing_ready_false(monitor_client, monkeypatch, tmp_path):
    """A missing-shape report reads ``ready_for_transcription: false`` —
    the honest 'this install cannot transcribe until repaired' answer."""
    missing = _present_report(str(tmp_path / "gone"))
    missing.update(
        vad_asset="missing",
        onnx_files=[],
        onnx_count=0,
        detail="assets dir absent",
    )
    monkeypatch.setattr("vidlift.transcribe.vad_asset_report", lambda: missing)
    payload = status_payload(StatusView(monitor_client))
    block = payload["transcription_engine"]
    assert block["vad_asset"] == "missing"
    assert block["ready_for_transcription"] is False
    assert block["assets_dir"] == str(tmp_path / "gone")


def test_monitor_envelope_probe_fault_serves_unknown(
    monitor_client, monkeypatch
):
    """The envelope's no-500 discipline, doubled for the packaging key:
    a report PROBE that itself faults degrades to the honest unknown
    shape (ready null) and the envelope still SERVES — the monitor must
    never become unreachable because the thing it watches is broken."""
    def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr("vidlift.transcribe.vad_asset_report", _boom)
    payload = status_payload(StatusView(monitor_client))  # no exception
    block = payload["transcription_engine"]
    assert block["vad_asset"] == "unknown"
    assert block["ready_for_transcription"] is None
    assert "RuntimeError" in (block.get("detail") or "")


def test_status_envelope_foreign_client_degrades():
    """The soft discipline (test_monitor.py's _OldShape era): a minimal
    client carrying ONLY the required read surface still gets a served
    envelope with honest fields — soft getattr chains throughout, the
    packaging block derived from the real checker (present on a
    provisioned box, unknown-lenient elsewhere — both serve)."""
    class _ForeignReads:
        def status_snapshot(self, job_id):
            return None

        def enqueue_snapshots(self, job_ids):
            return []

        def history(self, limit=20):
            return []

        def queue_views(self):
            return ([], [])

    payload = status_payload(StatusView(_ForeignReads()))
    block = payload["transcription_engine"]
    assert block["vad_asset"] in ("present", "unknown")
    if block["vad_asset"] == "present":
        assert block["ready_for_transcription"] is True
    else:
        assert block["ready_for_transcription"] is None
    assert payload["api_version"] == 1


# ---------------------------------------------------------------------------
# §7.6 — workflow gates (M4): the release pipeline cannot ship this again
# ---------------------------------------------------------------------------


def _build_block() -> str:
    """The 'Build GUI and CLI executables' step body (b20-style split)."""
    text = WORKFLOW.read_text(encoding="utf-8")
    return text.split("- name: Build GUI and CLI executables")[1].split(
        "- name:"
    )[0]


def _validate_block() -> str:
    """The 'Validate installed artifact' step body."""
    text = WORKFLOW.read_text(encoding="utf-8")
    return text.split("- name: Validate installed artifact")[1].split(
        "- name:"
    )[0]


def test_workflow_dist_tree_vad_gate():
    """Insertion A: after the build, CI must prove the frozen GUI tree
    carries the asset dir (name+size parity against the build venv via
    Compare-Object — dynamic, no hardcoded leaf) AND run the frozen
    CLI's --selftest (the binary's own loader is the authority). This
    test would have caught the v0.4.16.1 shipping defect because the
    workflow had NO assertion about the frozen Python payload at all."""
    block = _build_block()
    assert r"dist\VidLift\_internal\faster_whisper\assets" in block
    assert r"faster_whisper\assets" in block
    assert "Compare-Object" in block  # the parity rule
    assert "asset size mismatch" in block
    assert "ships no *.onnx" in block  # non-empty ONNX-set assert
    assert "--selftest" in block
    assert r"dist\selftest-cli.json" in block
    assert "vad assets shipped:" in block  # the grep-able success line


def test_workflow_installed_vad_gate():
    """Insertion B: the INSTALLED payload (what users actually run) is
    gated identically — assets-dir parity plus the installed CLI's
    --selftest — and the step is self-contained (each pwsh step is its
    own process, so the venv asset list is re-derived there)."""
    block = _validate_block()
    assert r"$install\_internal\faster_whisper\assets" in block
    assert "Compare-Object" in block
    assert "installed asset size mismatch" in block
    assert "--selftest" in block
    assert r"$install\selftest-cli.json" in block
    assert "installed vad assets:" in block
    # self-contained re-derivation (steps share no pwsh variables):
    assert "$fwAssets = (& python" in block
    assert "$expected = Get-ChildItem $fwAssets" in block


def test_workflow_names_no_version_suffixed_leaf():
    """ADR-6: the release path (both gates, whole workflow) never names
    the version-suffixed leaf — the leaf lives ONLY in this suite's
    pinned record, which trips loudly on upstream renames."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "silero" not in text.lower()