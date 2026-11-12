"""v0.4.16 (Karen remediation) — Mode A (run-job) model-download honesty.

Karen's v0.4.16 validation (probe 1) found two Medium defects in
run.py's Mode A publisher (`_on_model_download`), both sitting in cells
of the promised-but-never-delivered test matrices (her D-7):

- **D-1 / H-2**: the closure touched ONLY the live state
  (``state.touch()``) — ``run_file`` owns no JobRecord handle. A
  first-use model download inside a run-job left
  ``record.last_activity_at`` frozen while the state advanced, so every
  reader's ``activity_age_seconds`` (the GUI stale-activity row marker,
  the stage label's "last activity N min" suffix, and
  ``/status``) kept climbing during a LIVE download — crossing the
  15-min ``STALE_ACTIVITY_SECONDS`` threshold on a 17-minute fetch. The
  fix threads the executor's record through the state_box seam
  (jobs.py) and the publisher touches record+state on every landed
  beat, exactly mirroring Mode B's ``_model_download_publisher``.

- **D-2 / H-3 (Mode A analog)**: on a terminal download failure
  (network error, sha256 mismatch, truncated fetch), the stale
  ``model_download`` byte fact survived the fail-open conversion — the
  pop ran only on resolve's success path and ``record_step`` merges
  without popping — so status surfaces reported "downloading" for a
  dead download for the rest of the run. The fail-open arm now pops
  unconditionally (mirroring Mode B's ``on_step`` pop).

This file lands the missing **H-2 Mode A heartbeat-parity** and the
**H-3 Mode A analog** pins on the REAL run.py branch: the
probe/prepare/encode/verify command seam is a deterministic
record-and-fake runner (Karen's probe-1/cancel-suite shape — no real
subprocess beyond ffmpeg's absence), the upscale engine is a stub
adapter, the transcription engine is a stub built through run_file's
own ``transcribe_engine_factory`` seam, and the model resolve is a fake
that fires throttled download beats (and, for D-2, raises
TranscriptionError mid-download). The Registry record + state_box
wiring is exactly what ``_execute_run_job`` builds.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import vidlift.transcribe as transcribe_mod
from vidlift.mcp_bridge import BridgeServices
from vidlift.mcp_bridge.jobs import CancelFlag, JobOutcome, _execute_run_job
from vidlift.mcp_bridge.registry import Registry
from vidlift.mcp_bridge.tools import _status_payload
from vidlift.run import EngineAdapter, EngineResult, ToolBox, run_file

pytestmark = pytest.mark.usefixtures("transcription_engine_on")


# ---- the deterministic command seam (Karen probe 1 / cancel-suite shape) -----


def probe_payload() -> str:
    return json.dumps(
        {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "mpeg4",
                    "width": 640,
                    "height": 360,
                    "sample_aspect_ratio": "1:1",
                    "field_order": "progressive",
                    "r_frame_rate": "25/1",
                    "avg_frame_rate": "25/1",
                },
                {"index": 1, "codec_type": "audio", "codec_name": "mp3", "channels": 2},
            ],
            "format": {"format_name": "avi", "duration": "5400.0"},
        }
    )


class Runner:
    """Command seam: everything runs, nothing spawns. The ``pcm_s16le``
    branch writes the transcription scratch wav so the REAL core sees a
    real input file."""

    def __init__(self) -> None:
        self.payload = probe_payload()
        self.commands: list[str] = []

    def __call__(self, binary, args):
        name = " ".join(str(part) for part in args)
        self.commands.append(name)
        if "-show_format" in args:
            return 0, self.payload, ""
        if "idet" in args:
            return 0, "", (
                "[Parsed_idet] Single frame detection: TFF: 0 BFF: 0 "
                "Progressive: 0 Undetermined: 0\n"
            )
        if "-show_streams" in args and "-show_format" not in args:
            return 0, json.dumps(
                {"streams": [{"codec_type": "video", "width": 1280, "height": 720}]}
            ), ""
        if "pcm_s16le" in name:
            Path(args[-1]).write_bytes(b"wav")
            return 0, "", ""
        if args and str(args[-1]).endswith(".prepared.mkv"):
            Path(args[-1]).write_bytes(b"prepared-stub")
            return 0, "", ""
        if args and str(args[-1]).endswith(".muxing.mkv"):
            # the subtitles mux's own temp-then-replace staging
            # (mux_srt_into_video): the "carried" disposition lands here
            Path(args[-1]).write_bytes(b"muxed-stub")
            return 0, "", ""
        if args and str(args[-1]).endswith(".720p.mkv"):
            Path(args[-1]).write_bytes(b"output-stub")
            return 0, "", ""
        if args and args[-1] == "-" and "null" in args:
            return 0, "", ""
        return 1, "", f"unexpected: {binary} {name}"


class StubEngine(EngineAdapter):
    """The upscale engine seam (probe 1's shape): deterministic
    mezzanine, zero GPU."""

    def run(self, prepared_path, plan, tools, *, cancel=None, on_progress=None, on_step=None):
        mz = Path(prepared_path).with_suffix(".upscaled.mkv")
        mz.write_bytes(b"mezzanine-stub")
        return EngineResult(
            mezzanine_path=str(mz), frames_done=2, simulated=True
        )


class TextualEngine:
    """The transcription engine seam (mode_a_seam's shape): deterministic
    en segments with word stamps."""

    def __init__(self, model_path: str, *, device: str = "auto", vad_on: bool = True):
        self.model_path = model_path

    def detect_language(self, wav_path: str, *, offset_seconds: float = 0.0):
        from vidlift.transcribe import LanguageGuess

        return LanguageGuess(code="en", confidence=0.9)

    def transcribe_segments(
        self, wav_path: str, *, language: str, on_segment=None
    ):
        from vidlift.transcribe import TranscriptSegment, WordStamp

        segs = [
            TranscriptSegment(
                start=0.5,
                end=1.5,
                text=" Hello there.",
                words=[WordStamp(0.5, 1.0, "Hello", 0.95), WordStamp(1.1, 1.5, "there.", 0.95)],
            ),
            TranscriptSegment(
                start=2.0,
                end=2.8,
                text=" General Kenobi.",
                words=[WordStamp(2.0, 2.5, "General", 0.95), WordStamp(2.6, 2.8, "Kenobi.", 0.95)],
            ),
        ]
        for seg in segs:
            if on_segment is not None:
                on_segment(seg)
        return segs


# ---- the production wiring harness ------------------------------------------


def _wire(tmp_path: Path):
    """The REAL Registry record + state_box wiring ``_execute_run_job``
    builds (jobs.py), including the v0.4.16 D-1 seam key the executor
    puts in the box. `_wire_executor` pins that executor wiring
    directly; these mirrors keep the publisher tests honest."""
    src = tmp_path / "movie.avi"
    src.write_text("")
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    registry = Registry(tmp_path / "state")
    record = registry.enqueue(
        kind="run", paths=[str(src)], options={"log_dir": str(runs)}
    )
    registry.transition(record, "running")
    services = BridgeServices(registry=registry, state_dir=tmp_path / "state")
    state_box: dict = {}
    record.set_live("state_box", state_box)
    state_box["record"] = record  # the executor's D-1 seam wiring
    return src, record, services, state_box


def _run_mode_a(tmp_path, src, state_box, runner):
    return run_file(
        str(src),
        engine=StubEngine(),
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        output_dir=str(tmp_path / "out"),
        log_dir=str(tmp_path / "runs"),
        sample_classify=False,
        transcription_enabled=None,  # the locked default: ON (env fixture)
        transcribe_engine_factory=lambda model_dir: TextualEngine(model_dir),
        state_box=state_box,
    )


def _install_fake_resolve(monkeypatch, body) -> None:
    """run.py imports resolve_model_dir at phase time, so patching the
    transcribe module attribute redirects Mode A's resolve. The fake
    distinguishes the two production call shapes: run.py's
    download-shaped call (beats + lock-wait label) and the core's
    internal warm-path re-resolve (``transcribe_source_to_srt`` re-runs
    ``resolve_model_dir`` after run.py already resolved — in production
    that second call rides the just-written ``.complete`` fast path) —
    the core-shaped call resolves quietly with zero beats."""

    def fake_resolve(pin, **kwargs):
        download_shaped = (
            kwargs.get("on_download_progress") is not None
            or kwargs.get("on_lock_wait") is not None
        )
        return body(
            pin,
            kwargs.get("on_download_progress"),
            kwargs.get("on_lock_wait"),
            download_shaped,
        )

    monkeypatch.setattr(transcribe_mod, "resolve_model_dir", fake_resolve)


# ---- H-2: the Mode A heartbeat-parity test (D-1) -----------------------------


def test_run_executor_wires_the_record_into_the_state_box(
    tmp_path, monkeypatch
):
    """The D-1 seam (jobs.py): ``_execute_run_job`` hands run_file's
    state_box the JobRecord so Mode A's in-run publisher can touch it —
    the hand-wired boxes in the publisher tests mirror THIS production
    wiring, and a regression here would silently revive D-1."""
    registry = Registry(tmp_path / "state")
    record = registry.enqueue(kind="run", paths=["/m/movie.avi"], options={})
    registry.transition(record, "running")

    captured: dict = {}

    def fake_run_file(path, **kwargs):
        captured.update(kwargs)
        kwargs["state_box"]["state"] = SimpleNamespace(
            log=None, progress=None, last_activity_at=time.time()
        )
        return SimpleNamespace(
            ok=True,
            plan=SimpleNamespace(output_path="/m/movie.720p.mkv"),
            manifest_path=None,
            state=SimpleNamespace(
                log=None,
                error=None,
                engine_simulated=False,
                engine_note=None,
            ),
        )

    monkeypatch.setattr("vidlift.run.run_file", fake_run_file)
    services = SimpleNamespace(
        registry=registry,
        state_dir=tmp_path / "state",
        command_runner=None,
        probe_runner=None,
        engine=None,
    )
    outcome = _execute_run_job(record, services, CancelFlag())
    assert isinstance(outcome, JobOutcome) and outcome.ok
    box = record.get_live("state_box")
    assert box is not None and "record" in box, (
        "the executor must carry the record in the state_box — Mode A's "
        "publisher touches it (D-1 fix seam)"
    )
    assert box["record"] is record


def test_h2_mode_a_download_beats_touch_record_and_state(
    tmp_path, monkeypatch
):
    """H-2 (the promised Mode A parity test, D-1): during a SIMULATED
    SLOW first-use download inside a real run-job Mode A transcription,
    every landed beat touches BOTH the live state and the executor's
    JobRecord — the reader's ``_status_payload`` ``activity_age_seconds``
    stays small across the whole window (the record no longer freezes
    while the state advances). A pre-fix record freezes here and the
    age climbs linearly with the download (Karen's probe 1)."""
    src, record, services, state_box = _wire(tmp_path)
    runner = Runner()
    samples: list[dict] = []

    def fake_resolve_body(
        pin, on_download_progress, on_lock_wait, download_shaped
    ):
        if not download_shaped:
            # the core's internal re-resolve (production: the warm-cache
            # fast path) — no beats, same dir
            return str(tmp_path / "resolved-model")
        state = state_box["state"]
        for done, total in ((2_000_000, 5_000_000), (4_000_000, 5_000_000), (5_000_000, 5_000_000)):
            time.sleep(0.3)  # past the publisher's 0.25 s throttle
            if on_download_progress is not None:
                on_download_progress(done, total)
            progress = state.progress or {}
            samples.append(
                {
                    "record_la": record.last_activity_at,
                    "state_la": state.last_activity_at,
                    "age": _status_payload(services, record)[
                        "activity_age_seconds"
                    ],
                    "bytes": (progress.get("model_download") or {}).get(
                        "bytes_done"
                    ),
                }
            )
            # the download-lock wait rides the SAME record_step seam as
            # the GPU-phase lock's wait (run.py passes on_lock_wait)
            assert on_lock_wait is not None, (
                "Mode A hands the publisher BOTH seams: progress and the "
                "download-lock wait label"
            )
        return str(tmp_path / "resolved-model")

    _install_fake_resolve(monkeypatch, fake_resolve_body)
    result = _run_mode_a(tmp_path, src, state_box, runner)

    state = result.state
    assert state.ok, f"Mode A run failed: {state.error}"
    assert state.subtitles["status"] == "carried"
    assert state.subtitles["transcription"]["model"] == str(
        tmp_path / "resolved-model"
    )
    # the run reached the transcribed branch THROUGH the fake resolve's
    # beats (the Mode A branch actually executed)
    assert len(samples) == 3
    # H-2 parity: every beat landed (≥0.3 s spacing clears the 0.25 s
    # throttle), the record's last_activity_at strictly ADVANCED across
    # the window (Mode B's H-1b shape), the state touched too, and the
    # reader's activity_age_seconds never had room to climb.
    record_touches = [s["record_la"] for s in samples]
    state_touches = [s["state_la"] for s in samples]
    assert len(set(record_touches)) == 3, (
        "each landed download beat must touch the executor's record "
        "(D-1: a frozen record is the defect)"
    )
    assert record_touches == sorted(record_touches)
    assert state_touches[-1] > state_touches[0], (
        "the live state keeps advancing as before (H-2 parity with "
        "Mode B's state.touch())"
    )
    assert max(s["age"] for s in samples) < 0.5, (
        "a reader's activity_age_seconds stays small during a live "
        "download — it used to climb linearly (Karen probe 1)"
    )
    # the byte fact rode the SAME progress dict mid-flight (ADR-3)
    assert [s["bytes"] for s in samples] == [2_000_000, 4_000_000, 5_000_000]
    # and after resolve: the fact is popped, the step moved on (H-3's
    # success arm), and the terminal payload carries no stale bytes
    assert "model_download" not in (state.progress or {})
    assert "downloading" not in (state.progress or {}).get("step", "")
    payload = _status_payload(services, record)
    assert (payload.get("progress") or {}).get("model_download") is None


# ---- H-3: the Mode A analog (D-2) --------------------------------------------


def test_h3_mode_a_fail_open_pops_the_stale_download_fact(
    tmp_path, monkeypatch
):
    """H-3's Mode A analog (D-2): the download beats publish the byte
    fact, the resolve THEN terminally fails (a sha256 mismatch — the
    realistic first-use failure), and the fail-open conversion must pop
    ``progress["model_download"]`` unconditionally — mirroring Mode B's
    on_step pop. A pre-fix run leaves status surfaces reporting
    "downloading — 5000000 / 5000000 (100%)" for a DEAD download for
    the rest of the run (Karen's probe 1 post-run inspection)."""
    src, record, services, state_box = _wire(tmp_path)
    runner = Runner()

    def fake_resolve_body(
        pin, on_download_progress, on_lock_wait, download_shaped
    ):
        if not download_shaped:
            return str(tmp_path / "resolved-model")
        if on_download_progress is not None:
            on_download_progress(5_000_000, 5_000_000)
        raise transcribe_mod.TranscriptionError(
            "sha256 mismatch for fetched model.bin (forced)"
        )

    _install_fake_resolve(monkeypatch, fake_resolve_body)
    result = _run_mode_a(tmp_path, src, state_box, runner)

    state = result.state
    # Mode A is fail-open: the upscale still ships, the phase records
    # the dead transcription honestly...
    assert state.ok, "a download failure must NEVER fail the run (fail-open)"
    assert state.subtitles["status"] == "failed-open"
    assert "sha256 mismatch" in state.subtitles["reason"]
    # ...and the stale byte fact is GONE at the first fail-open beat and
    # through the terminal close:
    assert "model_download" not in (state.progress or {})
    assert "downloading" not in (state.progress or {}).get("step", "")
    payload = _status_payload(services, record)
    assert (payload.get("progress") or {}).get("model_download") is None
    # the transcription-model block derives from the honest disk/cache
    # truth now (no in-flight bytes to lie about)
    assert payload["transcription_model"]["bytes_done"] is None or (
        payload["transcription_model"]["bytes_done"]
        == payload["transcription_model"]["bytes_total"]
    )