"""v0.4.18 WS2 — Mode A suppress_srt across the FOUR subtitle branches.

Bob spec §12 file C. Fake Mode A runs reusing test_run.py's harness
fakes (no real ffmpeg/whisper/network/GPU anywhere):

  28 transcribed branch ON: mux ran, sidecar removed, manifest carries
     ``sidecar_suppressed`` and NO ``sidecar_path``; OFF (default):
     sidecar present + path set (unchanged v0.4.17)
  29 embedded-text branch ON: the `-c:s srt` extract command NEVER
     runs; status "carried"; ``embedded`` dict present; flag set.
     (The track already rode the encode as mov_text.)
  30 adopted-sidecar branch ON: the run's COPY ({output-stem}.srt
     beside the OUTPUT) is removed; the USER's original sidecar beside
     the SOURCE is byte-identical (never a candidate).
  31 OCR branch ON (fake OCR seam): the OCR sidecar is removed
     post-mux; the .ocr/ diagnosis kit is retained; OFF unchanged.
  32 mux failure + ON → the UNCHANGED fail-open: sidecar exists,
     ``sidecar_kept`` True, the fallback warning present.
  33 removal OSError + ON → success stands, sidecar present,
     ``sidecar_path`` set, S-9 in the run log.
  34 CLI print honesty: carried+suppressed prints S-13, ocr+suppressed
     prints S-14; default runs print the unchanged v0.4.17 lines.

The runner subclass adds the two arms test_run's FakeRunner lacks for
a full fake subtitle run: the transcription wav extract (pcm_s16le)
and the mov_text mux (whose final arg is the .muxing.mp4 TEMP, which
test_run's FakeRunner does not dispatch).
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest

import vidlift.run as run_mod
from test_run import FakeRunner, make_media_file, probe_payload
from vidlift.subtitles import OcrResult
from vidlift.run import ToolBox

# test_v0414_subs_job's engine double (two deterministic en segments)
from test_v0414_subs_job import FakeEngine


# ---------------------------------------------------------------- harness
@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.delenv("VIDLIFT_TRANSCRIPTION_ENGINE", raising=False)


class ModeARunner(FakeRunner):
    """FakeRunner + the Mode A transcription/mux arms.

    - pcm_s16le: the wav extract into the transcribe workdir (writes
      b"wav", exactly the test_v0414_subs_job FakeRunner arm).
    - `-c:s mov_text` with any final arg: the mov_text mux — writes the
      TEMP sibling at args[-1] (mux_srt_into_mp4 then os.replace()s).
    - mux_fail: only the mux (never the encode) exits nonzero — the
      fail-open test's lever.
    """

    def __init__(self, payload: str, *, mux_fail: bool = False) -> None:
        self.commands: list = []
        super().__init__(payload)
        self.mux_fail = mux_fail

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        argv = list(args)
        joined = " ".join(argv)
        if "pcm_s16le" in joined:
            # record WITHOUT falling through to super (which would
            # append it a second time)
            self.commands.append((binary, tuple(argv)))
            Path(argv[-1]).write_bytes(b"wav")
            return 0, "", ""
        if "-c:s" in argv and "mov_text" in argv:
            self.commands.append((binary, tuple(argv)))
            if self.mux_fail:
                return 1, "", "mux boom"
            Path(argv[-1]).write_bytes(b"muxed-stub")
            return 0, "", ""
        return super().__call__(binary, args)


class _NoLock:
    # .path is required by run.py's transcribed branch's log line
    path = "/tmp/vidlift-test-fake-gpu-phase.lock"

    def __init__(self):
        self.acquired = False
        self.released = False

    def acquire(self, on_wait=None):
        self.acquired = True
        return True

    def release(self):
        self.released = True


def _monkey_transcribe_seams(monkeypatch, tmp_path):
    """The transcribed branch's local imports resolve from
    vidlift.transcribe at call time — patch the two resolve seams +
    the GPU phase lock there (the test_v0414_subs_job patterns)."""
    import vidlift.engine_ncnn as engine_mod
    import vidlift.transcribe as transcribe_mod

    lock = _NoLock()
    monkeypatch.setattr(engine_mod, "_gpu_phase_lock", lambda: lock)
    monkeypatch.setattr(
        transcribe_mod,
        "resolve_model_dir",
        lambda pin=None, **_k: str(tmp_path / "model"),
    )
    monkeypatch.setattr(
        transcribe_mod,
        "resolve_transcription_device",
        lambda pin: "cpu",
    )
    return lock


def _mode_a_run_with_tools(tmp_path, runner, *, suppress_srt, monkeypatch,
                           output_dir=None, **kwargs):
    src = make_media_file(tmp_path)
    _monkey_transcribe_seams(monkeypatch, tmp_path)
    result = run_mod.run_file(
        src,
        engine_request="stub",
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        output_dir=output_dir,
        log_dir=str(tmp_path / "runs"),
        transcription_enabled=True,
        transcribe_engine_factory=lambda md: FakeEngine(md),
        suppress_srt=suppress_srt,
        **kwargs,
    )
    return result


def _mux_cmd_in(runner) -> bool:
    return any(
        "-c:s" in argv and "mov_text" in argv for _b, argv in runner.commands
    )


# ---------------------------------------------------------------- tests
def test_28_transcribed_branch_suppression_truthful_bookkeeping(
    tmp_path, engine_on, monkeypatch
):
    runner = ModeARunner(probe_payload())
    result = _mode_a_run_with_tools(tmp_path, runner, suppress_srt=True,
                                    monkeypatch=monkeypatch)
    out = Path(result.state.output_path)
    sidecar = out.with_suffix(".srt")
    subs = result.state.subtitles
    assert result.ok
    assert _mux_cmd_in(runner), "the mux MUST have run before removal"
    assert not sidecar.exists(), "the sidecar must be GONE after embed"
    assert subs["sidecar_suppressed"] is True
    assert "sidecar_path" not in subs
    # S-15 rides the run log
    log_text = "\n".join(result.state.log.tail(100)) if result.state.log else ""
    assert (
        "subtitles: .srt sidecar suppressed after a successful embed "
        "(suppress_srt)"
    ) in log_text


def test_28b_transcribed_default_off_keeps_v0417_block(
    tmp_path, engine_on, monkeypatch
):
    runner = ModeARunner(probe_payload())
    result = _mode_a_run_with_tools(tmp_path, runner, suppress_srt=False,
                                    monkeypatch=monkeypatch)
    out = Path(result.state.output_path)
    sidecar = out.with_suffix(".srt")
    subs = result.state.subtitles
    assert subs["sidecar_path"] == str(sidecar)  # unchanged default
    assert sidecar.is_file()  # the KEEP deliverable stands
    assert "sidecar_suppressed" not in subs


def test_29_embedded_text_branch_skips_extract_entirely(
    tmp_path, engine_on, monkeypatch
):
    payload = probe_payload(
        subtitles=[{"codec_name": "subrip", "tags": {"language": "eng"}}]
    )
    runner = ModeARunner(payload)
    result = _mode_a_run_with_tools(tmp_path, runner, suppress_srt=True,
                                    monkeypatch=monkeypatch)
    subs = result.state.subtitles
    assert result.ok
    assert subs["status"] == "carried"
    assert subs["sidecar_suppressed"] is True
    assert subs["embedded"] == {"codec": "mov_text", "language_tag": "eng"}
    # NO `-c:s srt` extract command ran at all (the suppressed arm)
    extracts = [
        " ".join(argv)
        for _b, argv in runner.commands
        if "-c:s" in argv and "srt" in argv
    ]
    assert extracts == [], extracts
    log_text = "\n".join(result.state.log.tail(100)) if result.state.log else ""
    assert (
        "subtitles: sidecar extract skipped — the track is embedded in "
        "the encode (suppress_srt)"
    ) in log_text


def test_30_adopted_sidecar_run_copy_removed_user_original_intact(
    tmp_path, engine_on, monkeypatch
):
    """Separate OUTPUT dir: the run's copy lives at
    <out>/movie.srt; the user's original beside the SOURCE is the
    adoption's input and must never be a removal candidate."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    payload = probe_payload()  # no subtitle streams → sidecar adoption
    runner = ModeARunner(payload)
    # the user's original sidecar beside the SOURCE
    original = tmp_path / "movie.srt"
    original.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
    result = run_mod.run_file(
        make_media_file(tmp_path),
        engine_request="stub",
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        output_dir=str(out_dir),
        log_dir=str(tmp_path / "runs"),
        transcription_enabled=True,
        transcribe_engine_factory=lambda md: FakeEngine(md),
        suppress_srt=True,
    )
    subs = result.state.subtitles
    assert subs["sidecar_suppressed"] is True
    assert "sidecar_path" not in subs
    # the run's COPY ({output-stem}.srt → /out/movie.720p.srt) is gone;
    # the user's ORIGINAL beside the source is byte-identical
    # (sidecar_srt_path of the OUTPUT — never the adoption input)
    run_copy = Path(result.state.output_path).with_suffix(".srt")
    assert str(run_copy).startswith(str(out_dir))
    assert not run_copy.exists()
    assert original.read_bytes() == (
        "1\n00:00:01,000 --> 00:00:02,000\nHi\n".encode("utf-8")
    )


def test_31_ocr_branch_suppression_and_kit_retention(
    tmp_path, engine_on, monkeypatch
):
    """Fake OCR seam (Linux lane monkeypatched to a fake run_subtitle_
    ocr writing the sidecar + the .ocr/ kit): suppression removes the
    sidecar post-mux; the kit is retained (v0.4.1 contract); OFF
    unchanged."""
    payload = probe_payload(
        subtitles=[{"codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "und"}}]
    )

    def fake_ocr(sub_plan, ocr_source, output_path, **_kw):
        srt = Path(output_path).with_suffix(".srt")
        srt.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nOCR hi\n", encoding="utf-8"
        )
        kit = Path(output_path + ".ocr")
        kit.mkdir(parents=True, exist_ok=True)
        (kit / "kit.txt").write_text("diagnosis", encoding="utf-8")
        return OcrResult(
            srt_path=str(srt), cue_count=1, engine="tesseract",
            notes=["OCR quality: ok"],
        )

    monkeypatch.setattr(run_mod, "run_subtitle_ocr", fake_ocr)
    monkeypatch.setattr(run_mod, "resolve_tesseract", lambda: "tesseract")

    # two runs in SEPARATE source dirs — the plan-time pre-flight
    # refuses a second run whose output already exists (output-exists)
    on_root = tmp_path / "on"
    off_root = tmp_path / "off"
    on_root.mkdir()
    off_root.mkdir()

    runner = ModeARunner(payload)
    src_on = make_media_file(on_root)
    _monkey_transcribe_seams(monkeypatch, on_root)
    result = run_mod.run_file(
        src_on,
        engine_request="stub",
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        log_dir=str(tmp_path / "runs"),
        suppress_srt=True,
    )
    subs = result.state.subtitles
    out = Path(result.state.output_path)
    sidecar = out.with_suffix(".srt")
    assert result.ok
    assert subs["status"] == "ocr"
    assert subs["sidecar_suppressed"] is True
    assert "sidecar_path" not in subs
    assert _mux_cmd_in(runner)  # the mux ran before the removal
    assert not sidecar.exists()
    kit = out.parent / (out.name + ".ocr")  # the fake kit's own path
    assert kit.is_dir()
    assert any(p.name == "kit.txt" for p in kit.iterdir())

    # OFF: the unchanged v0.4.17 block (sidecar_path present + file)
    runner2 = ModeARunner(payload)
    src_off = make_media_file(off_root)
    _monkey_transcribe_seams(monkeypatch, off_root)
    result2 = run_mod.run_file(
        src_off,
        engine_request="stub",
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner2,
        command_runner=runner2,
        log_dir=str(tmp_path / "runs"),
        suppress_srt=False,
    )
    subs2 = result2.state.subtitles
    assert subs2["sidecar_path"] == str(Path(result2.state.output_path).with_suffix(".srt"))
    assert Path(result2.state.output_path).with_suffix(".srt").is_file()


def test_32_suppress_on_mux_failure_fails_open_sidecar_kept(
    tmp_path, engine_on, monkeypatch
):
    runner = ModeARunner(probe_payload(), mux_fail=True)
    result = _mode_a_run_with_tools(tmp_path, runner, suppress_srt=True,
                                    monkeypatch=monkeypatch)
    out = Path(result.state.output_path)
    sidecar = out.with_suffix(".srt")
    subs = result.state.subtitles
    assert result.ok  # fail-open is a SUCCESSFUL run, honestly named
    assert subs["status"] == "failed-open"
    assert subs["sidecar_kept"] is True
    assert sidecar.is_file(), "the fallback promise: the sidecar stays"
    assert any(
        warning == "embed failed; sidecar kept next to the output"
        for warning in subs["warnings"]
    )


def test_33_suppress_removal_oserror_success_with_truthful_path(
    tmp_path, engine_on, monkeypatch
):
    def _oserror_remove(path, *, log_line=None):
        # the helper's honest failure shape without filesystem tricks
        if log_line is not None:
            log_line(
                f"WARNING suppress-srt: could not remove {path}: "
                "file locked by another process"
            )
        return False

    monkeypatch.setattr(run_mod, "remove_srt_sidecar", _oserror_remove)
    runner = ModeARunner(probe_payload())
    result = _mode_a_run_with_tools(tmp_path, runner, suppress_srt=True,
                                    monkeypatch=monkeypatch)
    out = Path(result.state.output_path)
    sidecar = out.with_suffix(".srt")
    subs = result.state.subtitles
    assert result.ok
    assert sidecar.is_file()
    assert subs["sidecar_path"] == str(sidecar)
    assert "sidecar_suppressed" not in subs
    log_text = "\n".join(result.state.log.tail(100)) if result.state.log else ""
    assert "WARNING suppress-srt: could not remove" in log_text


# ---------------------------------------------------------------- CLI (34)
def test_34_cli_print_honesty_suppressed_and_default(
    tmp_path, engine_on, monkeypatch, capsys
):
    """S-13/S-14 + the unchanged v0.4.17 default lines, through the real
    _cmd_run print paths (a real fake run's state, posture-modified)."""
    import vidlift.cli as cli_mod

    runner = ModeARunner(probe_payload())
    result = _mode_a_run_with_tools(tmp_path, runner, suppress_srt=False,
                                    monkeypatch=monkeypatch)
    state = result.state
    # the clean carried block the setup run produced (the v0.4.17 OFF
    # posture) — every CLI posture rebuilds FROM this copy so the
    # sequential mutations never leak across postures
    clean_subs = dict(state.subtitles)

    def _with_posture(result_, posture: str):
        import copy as _copy

        subs = dict(clean_subs)
        subs["embedded"] = {
            "codec": "mov_text", "language_tag": "eng",
        }
        if posture == "carried-suppressed":
            subs.pop("sidecar_path", None)
            subs["sidecar_suppressed"] = True
        elif posture == "ocr-suppressed":
            subs["status"] = "ocr"
            subs["ocr"] = {"engine": "tesseract", "cue_count": 3}
            subs.pop("sidecar_path", None)
            subs["sidecar_suppressed"] = True
        elif posture == "carried-default":
            subs.pop("sidecar_suppressed", None)
            subs.pop("sidecar_path", None)
            subs["sidecar_path"] = (
                str(Path(result_.state.output_path).with_suffix(".srt"))
            )
        elif posture == "ocr-default":
            subs["status"] = "ocr"
            subs["ocr"] = {"engine": "tesseract", "cue_count": 3}
            subs.pop("sidecar_suppressed", None)
            subs.pop("sidecar_suppressed", None)
            subs["sidecar_path"] = (
                str(Path(result_.state.output_path).with_suffix(".srt"))
            )
        result_.state.subtitles = subs
        return result_

    def _run_cli(posture):
        monkeypatch.setattr(
            cli_mod,
            "run_file",
            lambda path, **kw: _with_posture(result, posture),
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_mod.main(["run", state.file_path])
        out = buf.getvalue()
        assert code == 0, out
        return out

    out_carried = _run_cli("carried-suppressed")
    assert (
        "subtitles: carried (mov_text embedded; .srt sidecar suppressed)"
        in out_carried
    )
    out_ocr = _run_cli("ocr-suppressed")
    assert "  sidecar: suppressed after a successful embed" in out_ocr
    out_c_default = _run_cli("carried-default")
    assert (
        "subtitles: carried (mov_text embedded + .srt sidecar)"
        in out_c_default
    )
    out_o_default = _run_cli("ocr-default")
    assert "  sidecar: " in out_o_default
    ocr_sidecar_line = next(
        line for line in out_o_default.splitlines() if line.startswith("  sidecar:")
    )
    assert "suppressed" not in ocr_sidecar_line