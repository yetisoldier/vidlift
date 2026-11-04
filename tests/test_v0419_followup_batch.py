"""v0.4.19 — cancel pre-transcribe + plan-time skip + device indicator.

Bob's v0.4.19 follow-up batch (spec §12 File A). Groups (appended per
milestone):

  W1a — core cancel observes (transcribe.py, engine_on + seeded model
        state): the five observes O1/O3/O4/O5 + the armed download
        beats O2, the entry-observe precedence, the conditional-arity
        rule (R-5), and the golden-path drift guard.
  W1b — both modes close ``cancelled`` (executor integration, the
        test_v0416_cancel_propagation harness shape) + the
        EngineCancelled→JobOutcome(cancelled=True) mapping pins.
  W3  — device indicator (M2).
  W2  — plan-time skip (M3).

Conventions: house fakes — FakeEngine / FakeRunner / _probe_payload /
_seed_model_state imported from test_v0414_subs_job; the ``engine_on``
fixture pattern of test_v0418_subs_suppress_cleanup.py; GUI-level
tests reuse ``test_v0414_mode_b_gui``'s _ScriptedClient / _window. No
network / GPU / real-model.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from vidlift.run import EngineCancelled
from vidlift.transcribe import TranscriptionError, resolve_model_dir

from test_v0414_subs_job import (  # house fakes (spec §12 conventions)
    FakeEngine,
    FakeRunner,
    _probe_payload,
    _seed_model_state,
)


# ---------------------------------------------------------------- harness
@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.delenv("VIDLIFT_TRANSCRIPTION_ENGINE", raising=False)


class _CountingEngine(FakeEngine):
    """FakeEngine with call counters + an optional mid-detect flag flip
    (the counter seam the WS1 tests observe; the golden behavior is the
    base class's)."""

    def __init__(self, model_path, *, detect_flips: "dict | None" = None):
        super().__init__(model_path)
        self.detect_calls = 0
        self.transcribe_calls = 0
        self._detect_flips = detect_flips

    def detect_language(self, wav_path: str, *, offset_seconds: float = 0.0):
        self.detect_calls += 1
        if self._detect_flips is not None:
            self._detect_flips["flag"] = True
        return super().detect_language(wav_path, offset_seconds=offset_seconds)

    def transcribe_segments(self, wav_path, *, language, on_segment=None):
        self.transcribe_calls += 1
        return super().transcribe_segments(
            wav_path, language=language, on_segment=on_segment
        )


def _core(tmp_path, *, flag_box: "dict | None" = None, steps: list | None = None,
          **over):
    """The transcribe_source_to_srt call with the house fakes + seeded
    model state. ``over`` kwargs override verbatim (engine_factory=,
    on_step=, …). The VAD preflight report is injected so File A stays
    hermetic on boxes without the bundled assets."""
    state_root = str(tmp_path / "state")
    _seed_model_state(state_root)
    if steps is None:
        steps = []

    def factory(model_dir):
        return _CountingEngine(model_path=model_dir)

    kwargs = dict(
        source_path=str(tmp_path / "movie.avi"),
        output_path=str(tmp_path / "movie.subs.mp4"),
        workdir=str(tmp_path / "work"),
        ffmpeg="ffmpeg",
        runner=FakeRunner(_probe_payload()),
        engine_factory=factory,
        state_root=state_root,
        vad_report={"vad_asset": "present"},
        on_step=steps.append,
        should_cancel=(lambda: bool(flag_box and flag_box.get("flag")))
        if flag_box is not None
        else None,
    )
    kwargs.update(over)
    if not Path(kwargs["source_path"]).exists():
        Path(kwargs["source_path"]).write_bytes(b"source")
    return kwargs


# ------------------------------------------------------------------ W1a
def test_t1_flag_true_at_entry_raises_s1_before_resolution(
    tmp_path, engine_on
):
    """O1: a cancel already flipped at the core's resolve step raises
    EngineCancelled with S-1 right after the transcribe-resolve step —
    and the engine factory was NEVER called (no model load started)."""
    steps: list[str] = []
    factory_calls: dict = {}

    def factory(model_dir):
        factory_calls["engine"] = factory_calls.get("engine", 0) + 1
        return _CountingEngine(model_path=model_dir)

    from vidlift.transcribe import transcribe_source_to_srt

    with pytest.raises(EngineCancelled) as excinfo:
        transcribe_source_to_srt(
            **_core(
                tmp_path,
                flag_box={"flag": True},
                steps=steps,
                engine_factory=factory,
            )
        )
    assert str(excinfo.value) == "cancelled during model resolution"  # S-1
    assert steps == ["transcribe-resolve"]
    assert factory_calls.get("engine", 0) == 0


def test_t2_factory_flips_flag_raises_s3_after_engine_load(
    tmp_path, engine_on
):
    """O3: the factory returns the engine, the observe fires BEFORE any
    language work — S-3, factory called exactly once, detect never."""
    steps: list[str] = []
    factory_calls: dict = {"count": 0}
    built: dict = {}
    flag_box: dict = {}

    def factory(model_dir):
        factory_calls["count"] += 1
        eng = _CountingEngine(model_path=model_dir)
        built["engine"] = eng
        flag_box["flag"] = True
        return eng

    from vidlift.transcribe import transcribe_source_to_srt

    with pytest.raises(EngineCancelled) as excinfo:
        transcribe_source_to_srt(
            **_core(
                tmp_path,
                steps=steps,
                engine_factory=factory,
                flag_box=flag_box,
            )
        )
    assert str(excinfo.value) == "cancelled during model load"  # S-3
    assert steps == ["transcribe-resolve"]
    assert factory_calls["count"] == 1
    assert built["engine"].detect_calls == 0
    assert built["engine"].transcribe_calls == 0


def test_t3_onstep_language_flips_raises_s4_before_detection(
    tmp_path, engine_on
):
    """O4: the flag flipped at the transcribe-language step label raises
    'cancelled before language detection' — detect_language never runs."""
    steps: list[str] = []
    built: dict = {}
    flag_box: dict = {}

    def factory(model_dir):
        eng = _CountingEngine(model_path=model_dir)
        built["engine"] = eng
        return eng

    def on_step(step: str) -> None:
        steps.append(step)
        if step == "transcribe-language":
            flag_box["flag"] = True

    from vidlift.transcribe import transcribe_source_to_srt

    with pytest.raises(EngineCancelled) as excinfo:
        transcribe_source_to_srt(
            **_core(
                tmp_path,
                engine_factory=factory,
                on_step=on_step,
                flag_box=flag_box,
            )
        )
    assert str(excinfo.value) == "cancelled before language detection"  # S-4
    assert steps == ["transcribe-resolve", "transcribe-language"]
    assert built["engine"].detect_calls == 0


def test_t4_detect_flips_flag_raises_s4_after_no_rebuild(
    tmp_path, engine_on, monkeypatch
):
    """O5: detect_language returns (flag flipped mid-detect), the observe
    immediately AFTER the call raises 'cancelled during language
    detection' — never transcribe-run, and NO CPU-rebuild side effect:
    the DEFAULT fallback-wrapper path is driven via the wrapper fake and
    the wrapper is constructed exactly once with exactly one detect (the
    D2 re-raise discipline — no rebuild, no fabricated rescue)."""
    steps: list[str] = []
    flag_box: dict = {}
    built: dict = {}
    import vidlift.transcribe as transcribe_mod

    def fake_wrapper(model_dir, **engine_kwargs):
        built["kwargs"] = engine_kwargs
        built["construction"] = built.get("construction", 0) + 1
        eng = _CountingEngine(model_path=model_dir, detect_flips=flag_box)
        built["engine"] = eng
        return eng

    monkeypatch.setattr(
        transcribe_mod, "make_whisper_engine_with_fallback", fake_wrapper
    )
    from vidlift.transcribe import transcribe_source_to_srt

    with pytest.raises(EngineCancelled) as excinfo:
        transcribe_source_to_srt(
            **_core(
                tmp_path,
                steps=steps,
                flag_box=flag_box,
                engine_factory=None,
            )
        )
    assert str(excinfo.value) == "cancelled during language detection"  # S-4
    assert steps[-1] == "transcribe-language"
    assert "transcribe-run" not in steps
    assert built["construction"] == 1  # no CPU-rebuild re-construction
    assert built["engine"].detect_calls == 1  # no second (rescue) detect


def test_t5_flag_flips_at_transcribe_run_s1_segment_observe_untouched(
    tmp_path, engine_on
):
    """The EXISTING _on_segment observe is the first segment-level gate:
    a flag flipped only AT transcribe-run still raises the untouched
    'cancelled during transcription' (the v0.4.14 observe, verbatim)."""
    steps: list[str] = []
    flag_box: dict = {}

    def on_step(step: str) -> None:
        steps.append(step)
        if step == "transcribe-run":
            flag_box["flag"] = True

    from vidlift.transcribe import transcribe_source_to_srt

    with pytest.raises(EngineCancelled) as excinfo:
        transcribe_source_to_srt(
            **_core(tmp_path, on_step=on_step, flag_box=flag_box)
        )
    assert str(excinfo.value) == "cancelled during transcription"
    assert steps[-1] == "transcribe-run"


class _Arity3Fetcher:
    """Injected fetch with the modern 3-arg contract (url, target,
    on_progress). It stages bytes through the .part.tmp → os.replace
    discipline (mimicking the default fetcher), fires a beat, flips the
    flag, fires a second beat — and on any BaseException removes ONLY
    the .tmp staging and re-raises (the default fetcher's control-flow
    pass-through)."""

    def __init__(self, flip_box: dict) -> None:
        self.flip_box = flip_box
        self.calls: list[tuple] = []

    def __call__(self, url: str, target_path, on_progress=None) -> None:
        self.calls.append((url, str(target_path), on_progress is not None))
        staging = Path(str(target_path) + ".tmp")
        staging.write_bytes(b"x" * 128)
        try:
            if on_progress is not None:
                on_progress(64, None)  # beat 1: flag still clear
                # the flip lands BETWEEN beats (should_cancel True after
                # the first beat — exactly the pin's premise)
                self.flip_box["flip"] = True
                on_progress(128, None)  # beat 2: the armed observe raises
            staging.replace(target_path)
        except BaseException:
            staging.unlink(missing_ok=True)  # ONLY the .tmp (M-1 rule)
            raise


def test_t6a_download_beat_flips_cancel_raises_s2(tmp_path, engine_on):
    """O2 (the armed download beats): a should_cancel flipping after the
    first download beat raises S-2 through the injected arity-3 fetch;
    the `.complete` marker is absent and no `.part` staging survives."""
    state_root = tmp_path / "state"
    flip_box = {"flip": False}
    fetch = _Arity3Fetcher(flip_box)
    beats: list[tuple[int, int | None]] = []
    with pytest.raises(EngineCancelled) as excinfo:
        resolve_model_dir(
            None,
            state_root=str(state_root),
            fetch=fetch,
            on_download_progress=lambda done, total: beats.append((done, total)),
            should_cancel=lambda: bool(flip_box["flip"]),
        )
    assert str(excinfo.value) == "cancelled during model download"  # S-2
    assert beats, "the first beat fired (the channel existed)"
    assert len(beats) == 1  # beat 2 never reached the consumer
    tag_dir = state_root / "whisper" / "models" / "medium"
    assert not (tag_dir / ".complete").exists()
    assert not list(tag_dir.glob("*.part")) and not list(
        tag_dir.glob("*.part.tmp")
    )


def test_t6b_legacy_arity2_fetch_no_progress_cancel_true_raises_s1_at_entry(
    tmp_path, engine_on
):
    """R-5 (the conditional-arity pin): an injected legacy 2-arg fetch
    with NO progress callback keeps its byte-identical shape — a cancel
    raised at ENTRY (S-1) closes before the fetch is ever called (never
    a TypeError from a 3-arg call)."""

    def legacy_fetch(url: str, target_path) -> None:  # 2-arg shape only
        raise AssertionError("the legacy fetch must never be called")

    state_root = tmp_path / "state"
    with pytest.raises(EngineCancelled) as excinfo:
        resolve_model_dir(
            None,
            state_root=str(state_root),
            fetch=legacy_fetch,
            should_cancel=lambda: True,
        )
    assert str(excinfo.value) == "cancelled during model resolution"  # S-1
    tag_dir = state_root / "whisper" / "models" / "medium"
    assert not (tag_dir / ".complete").exists()


def test_t6c_legacy_arity2_fetch_no_cancel_keeps_call_shape(
    tmp_path, engine_on
):
    """The absent-should_cancel contract + the legacy shape: no
    should_cancel and an arity-2 fetch behave identically to v0.4.16 —
    the fetch received exactly 2 args on every call (never a 3rd), and
    resolution runs its real contract far enough to raise the honest
    sha-pin failure on junk content (pytest.raises TranscriptionError —
    NEVER TypeError; spec §12 T6c's arity-shape scoping)."""
    from vidlift.transcribe import MODEL_SUPPORT_FILES, MIN_DOWNLOAD_SIZE

    state_root = tmp_path / "state"
    call_shapes: list[int] = []

    def legacy_fetch(url: str, target_path) -> None:
        call_shapes.append(2)
        name = Path(str(target_path)).name
        for filename, expected_size in MODEL_SUPPORT_FILES["medium"]:
            if filename == name:
                Path(str(target_path)).write_bytes(b"z" * expected_size)
                return
        # model.bin: expected_size None — the MIN floor is what gates
        Path(str(target_path)).write_bytes(b"z" * MIN_DOWNLOAD_SIZE)

    with pytest.raises(TranscriptionError):  # the sha pin (junk content)
        resolve_model_dir(
            None,
            state_root=str(state_root),
            fetch=legacy_fetch,
        )
    assert call_shapes and all(shape == 2 for shape in call_shapes)
    tag_dir = state_root / "whisper" / "models" / "medium"
    # the sha refusal deleted the junk model.bin (its documented cleanup)
    assert not (tag_dir / "model.bin").exists()


def test_t7_absent_should_cancel_golden_path_unchanged(tmp_path, engine_on):
    """T7: should_cancel=None → the v0.4.14 FakeEngine golden path is
    unchanged (2 cues, the KEEP sidecar beside the output)."""
    from vidlift.transcribe import transcribe_source_to_srt

    result = transcribe_source_to_srt(**_core(tmp_path))
    assert result.cue_count == 2
    srt = Path(str(tmp_path / "movie.subs.mp4")).with_name("movie.subs.srt")
    assert srt.is_file(), f"no KEEP sidecar at {srt}"
    assert "Hello there." in srt.read_text(encoding="utf-8-sig")


# ------------------------------------------------------------------ W1b
class _BlockingSubsFactory:
    """A transcribe_engine_factory that BLOCKS inside the engine-load
    window until released — the wedged-at-model-load shape the WS1
    observes must close cancelled (Mode B: O3; Mode A: the same core
    observe through run.py's engine_factory closure)."""

    def __init__(self) -> None:
        self.calls = 0
        self._entered = threading.Event()
        self._release = threading.Event()

    def __call__(self, model_dir: str):
        self.calls += 1
        self._entered.set()
        self._release.wait(timeout=20)
        # a real-shaped engine (never actually used: the O3 observe
        # sees the flipped cancel immediately after construction)
        return _CountingEngine(model_path=model_dir)

    def wait_entered(self, timeout: float = 20.0) -> None:
        assert self._entered.wait(timeout=timeout), "factory never called"

    def release(self) -> None:
        self._release.set()


class _ModeARunner:
    """The Mode A command seam (the BridgeRunner shape, simplified):
    real probe JSON, prepare/encode/verify arms, and the pcm_s16le wav
    arm the transcription phase needs. Never blocks — the factory
    blocks."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.commands: list[str] = []

    def __call__(self, binary: str, args: list) -> tuple[int, str, str]:
        argv = [str(a) for a in args]
        self.commands.append(" ".join(argv))
        if "-show_format" in argv:
            return 0, self.payload, ""
        if "-show_streams" in argv and "-show_format" not in argv:
            return 0, json.dumps(
                {"streams": [{"codec_type": "video", "width": 1280,
                              "height": 720}]}
            ), ""
        if "idet" in argv:
            return 0, "", (
                "[Parsed_idet] Single frame detection: TFF: 0 BFF: 0 "
                "Progressive: 0 Undetermined: 0\n"
            )
        if argv and argv[-1].endswith(".prepared.mkv"):
            Path(argv[-1]).write_bytes(b"prepared-stub")
            return 0, "", ""
        if argv and argv[-1].endswith(".slice.prepared.mkv"):
            Path(argv[-1]).write_bytes(b"slice-stub")
            return 0, "", ""
        if argv and argv[-1].endswith(".720p.mp4"):
            Path(argv[-1]).write_bytes(b"output-stub")
            return 0, "", ""
        if argv and argv[-1] == "-" and "null" in argv:
            return 0, "", ""
        if "pcm_s16le" in " ".join(argv):
            Path(argv[-1]).write_bytes(b"wav")
            return 0, "", ""
        return 1, "", f"unexpected: {binary} {' '.join(argv)}"


def _call(server, name: str, arguments: dict, request_id: int = 1) -> dict:
    response = server.handle_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
    )
    message = json.loads(response)
    return json.loads(message["result"]["content"][0]["text"])


def _wait_terminal(server, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _call(server, "status", {"job_id": job_id}, 99)
        if last["state"] in ("cancelled", "succeeded", "failed"):
            return last
        time.sleep(0.02)
    raise AssertionError(f"job never reached terminal; last: {last}")


def test_t8_mode_b_blocks_at_model_load_close_cancelled(tmp_path, engine_on):
    """W1b T8 (the wedged-job defect, end to end): Mode B claims, blocks
    INSIDE the engine factory (the model-load window), the cancel flips,
    the factory returns, and the job closes ``cancelled`` — never
    ``failed`` — with the WS1 S-3 message as the recorded error."""
    from vidlift.mcp_bridge import BridgeServices, McpServer

    factory = _BlockingSubsFactory()
    runner = FakeRunner(_probe_payload())
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    _seed_model_state(str(state))
    services = BridgeServices(
        registry=None,
        state_dir=state,
        probe_runner=runner,
        command_runner=runner,
    )
    services.transcribe_engine_factory = factory
    server = McpServer(state_dir=state, services=services, start_workers=True)
    try:
        media = tmp_path / "wedged.avi"
        media.write_bytes(b"source")
        payload = _call(server, "transcribe", {"paths": [str(media)]})
        job_id = payload["jobs"][0]["job_id"]
        deadline = time.time() + 10
        while factory.calls == 0 and time.time() < deadline:
            time.sleep(0.02)
        factory.wait_entered()
        # the click: the registry's cancel API flips the claim-time flag
        verdict = _call(server, "cancel", {"job_id": job_id})
        assert verdict["state"] == "cancelling"
        factory.release()
        final = _wait_terminal(server, job_id)
        assert final["state"] == "cancelled", final["error"]
        assert "cancelled during model load" in (final["error"] or "")
        # never failed (the register-mapping discipline)
        history = _call(server, "list", {"include_history": True}, 100)[
            "history"
        ]
        row = next(r for r in history if r["job_id"] == job_id)
        assert row["state"] == "cancelled"
        assert "cancelled during model load" in (row["error"] or "")
    finally:
        server.executor.stop()


def test_t9_mode_a_transcription_block_close_cancelled(
    tmp_path, engine_on, monkeypatch
):
    """W1b T9: Mode A — the run reaches transcription (the shared core),
    the engine construction BLOCKS (the wedged model-load window; the
    core's default fallback-wrapper factory is faked in-process — the
    real one would need a GPU/model), the cancel flips, release → the
    core's O3 observe closes the run ``cancelled`` (never failed-open:
    the EngineCancelled escapes the TranscriptionError conversion)."""
    import vidlift.transcribe as transcribe_mod

    factory = _BlockingSubsFactory()
    monkeypatch.setattr(
        transcribe_mod, "make_whisper_engine_with_fallback",
        lambda model_dir, **_kw: factory(model_dir),
    )
    runner = _ModeARunner(_probe_payload())
    from vidlift.mcp_bridge import BridgeServices, McpServer

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    _seed_model_state(str(state))
    services = BridgeServices(
        registry=None,
        state_dir=state,
        probe_runner=runner,
        command_runner=runner,
    )
    services.transcribe_engine_factory = factory
    server = McpServer(state_dir=state, services=services, start_workers=True)
    try:
        media = tmp_path / "modea-cancel.avi"
        media.write_bytes(b"source")
        payload = _call(
            server,
            "run",
            {
                "paths": [str(media)],
                "options": {"log_dir": str(tmp_path / "runs")},
            },
        )
        job_id = payload["jobs"][0]["job_id"]
        deadline = time.time() + 30
        while factory.calls == 0 and time.time() < deadline:
            time.sleep(0.02)
        factory.wait_entered()
        verdict = _call(server, "cancel", {"job_id": job_id})
        assert verdict["state"] == "cancelling"
        factory.release()
        final = _wait_terminal(server, job_id, timeout=40)
        assert final["state"] == "cancelled", final["error"]
        assert "cancelled during model load" in (final["error"] or "")
        history = _call(server, "list", {"include_history": True}, 101)[
            "history"
        ]
        row = next(r for r in history if r["job_id"] == job_id)
        assert row["state"] == "cancelled"
    finally:
        server.executor.stop()


def test_t10_mapping_pins_engine_cancelled_becomes_cancelled_outcome(
    tmp_path, engine_on, monkeypatch
):
    """W1b T10: the executor mapping pins — an EngineCancelled raised at
    the core boundary (stubbed run_subs_job / run_file) maps to
    JobOutcome(cancelled=True) in BOTH executors (the jobs.py engine-
    cancelled mappings cannot regress by reorder)."""
    from vidlift.mcp_bridge import BridgeServices
    from vidlift.mcp_bridge.jobs import (
        CancelFlag,
        _execute_run_job,
        _execute_subs_job,
    )
    from vidlift.mcp_bridge.registry import Registry

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    registry = Registry(state)
    services = BridgeServices(registry=registry, state_dir=state)
    media = tmp_path / "movie.avi"
    media.write_bytes(b"x")

    # (a) Mode B: run_subs_job raises -> cancelled outcome
    def _boom_subs(*_a, **_k):
        raise EngineCancelled("cancelled during transcription")

    monkeypatch.setattr("vidlift.subs_job.run_subs_job", _boom_subs)
    record = registry.enqueue(kind="subs", paths=[str(media)], options={})
    outcome = _execute_subs_job(record, services, CancelFlag())
    assert outcome.cancelled is True and outcome.ok is False
    assert "cancelled during transcription" in (outcome.error or "")

    # (b) Mode A: run_file raises -> cancelled outcome
    def _boom_run(*_a, **_k):
        raise EngineCancelled("cancelled during transcription")

    monkeypatch.setattr("vidlift.run.run_file", _boom_run)
    record2 = registry.enqueue(kind="run", paths=[str(media)], options={})
    outcome2 = _execute_run_job(record2, services, CancelFlag())
    assert outcome2.cancelled is True and outcome2.ok is False
    assert "cancelled during transcription" in (outcome2.error or "")