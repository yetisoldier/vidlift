"""v0.4.24 MKV-container contract tests (Bob's spec §8b, T-NEW-1..7).

Seven red-first contractual pins for the MP4→MKV output-container
change (the container ADR in
.specs/bob-vidlift-v0.4.24-mkv-container-spec.md):

1. T-NEW-1 run-side guard: a pre-0.4.24 legacy ``{stem}.subs.mp4``
   blocks ``run_subs_job`` (names the legacy file; never a transcription).
2. T-NEW-2 plan-time guard: ``subs_plan_verdict`` is probe-free
   blocked for the legacy name; ``--force`` does not bypass; the
   current ``{stem}.subs.mkv`` name behavior is unchanged.
3. T-NEW-3 collector: BOTH subs-output families are excluded from
   re-ingestion; plain sources stay collectible.
4. T-NEW-4 schema vocabulary: MCP schemas/descriptions carry
   ``subrip``/``.subs.mkv`` and no ``mov_text``/``.mp4`` tokens.
5. T-NEW-5 encode command shape: ``-c:s subrip``, `.mkv` output, and
   NO ``-movflags`` token (D7).
6. T-NEW-6 sidecar derivation: a `{stem}.subs.mkv` output derives
   `{stem}.subs.srt` (the 4-char-strip invariant).
7. T-NEW-7 verify scan: subrip is the single scan token, fail-open
   preserved, and a stray mov_text reports the generalized
   unexpected-stream detail naming the codec.

Fake seams are imported from the established harnesses (the repo's
one-source-of-truth convention) — no real ffmpeg/whisper/network
except where noted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vidlift.mcp_bridge.schemas import TOOL_DESCRIPTIONS, TOOL_SCHEMAS
from vidlift.plan import build_file_plan
from vidlift.run import ToolBox, build_encode_command, verify_output
from vidlift.subs_job import (
    SUBS_VERDICT_BLOCKED_OUTPUT,
    SubsJobError,
    run_subs_job,
    subs_plan_verdict,
)

# Established fakes / fixtures / builders (imported, never forked —
# pytest resolves the imported engine_on/no_lock fixtures).
from test_subtitles_v041 import _report
from test_v0414_subs_job import (  # noqa: F401 (engine_on/no_lock fixtures)
    FakeEngine,
    FakeRunner,
    _probe_payload,
    engine_on,
    no_lock,
)


# ------------------------------------------------------ 1+2. legacy guards
def test_v0424_subs_legacy_output_blocked(tmp_path):
    """T-NEW-1 (run-side): a legacy `{stem}.subs.mp4` — with NO current
    `.subs.mkv` present — makes run_subs_job raise the block error
    naming the LEGACY file, and no transcription is ever invoked."""
    src = tmp_path / "movie.avi"
    src.write_bytes(b"source-bytes")
    legacy = tmp_path / "movie.subs.mp4"
    legacy.write_bytes(b"pre-0.4.24-output")
    factory_calls: list[str] = []

    def engine_factory(model_dir):
        factory_calls.append(model_dir)
        return FakeEngine(model_dir)

    with pytest.raises(SubsJobError) as excinfo:
        run_subs_job(
            str(src),
            ffmpeg="ffmpeg",
            probe_runner=FakeRunner(_probe_payload()),
            runner=FakeRunner(_probe_payload()),
            engine_factory=engine_factory,
            workdir=str(tmp_path / "wd"),
            gpu_lock_factory=lambda: type("L", (), {
                "acquire": staticmethod(lambda on_wait=None: True),
                "release": staticmethod(lambda: None),
            })(),
        )
    # the error names the file that matched — the legacy one
    assert "output already exists (never overwritten)" in str(excinfo.value)
    assert "movie.subs.mp4" in str(excinfo.value)
    # nothing was overwritten, and no transcription ran (the fake
    # engine factory is only reachable past the guard)
    assert legacy.read_bytes() == b"pre-0.4.24-output"
    assert src.read_bytes() == b"source-bytes"
    assert factory_calls == []
    # and no stray new-container output was created either
    assert not (tmp_path / "movie.subs.mkv").exists()


def test_v0424_plan_verdict_legacy_blocked(tmp_path):
    """T-NEW-2 (plan-time): subs_plan_verdict returns
    SUBS_VERDICT_BLOCKED_OUTPUT probe-free for the legacy
    ``{stem}.subs.mp4`` name; ``--force`` does NOT bypass it; and the
    current ``{stem}.subs.mkv`` name behavior is unchanged
    (v0.4.19 regression pin)."""
    counter = {"calls": 0}

    def probe_runner(binary, args):
        counter["calls"] += 1
        raise AssertionError("blocked verdicts must be probe-free")

    legacy_src = tmp_path / "legacy.avi"
    legacy_src.write_bytes(b"x")
    (tmp_path / "legacy.subs.mp4").write_bytes(b"out")
    assert subs_plan_verdict(
        str(legacy_src), probe_runner=probe_runner
    ) == SUBS_VERDICT_BLOCKED_OUTPUT
    assert counter["calls"] == 0  # checked FIRST and probe-free

    # --force does not bypass the never-overwrite guard
    assert subs_plan_verdict(
        str(legacy_src), probe_runner=probe_runner, force=True
    ) == SUBS_VERDICT_BLOCKED_OUTPUT

    # current-name regression pin: the v0.4.19 probe-free blocked
    # verdict for {stem}.subs.mkv is byte-identical
    current_src = tmp_path / "current.avi"
    current_src.write_bytes(b"x")
    (tmp_path / "current.subs.mkv").write_bytes(b"out")
    assert subs_plan_verdict(
        str(current_src), probe_runner=probe_runner
    ) == SUBS_VERDICT_BLOCKED_OUTPUT
    assert counter["calls"] == 0


# ------------------------------------------------------ 3. collector guard
def test_v0424_collector_excludes_both_subs_families(tmp_path):
    """T-NEW-3: collect_video_files yields NEITHER `a.subs.mp4` (the
    pre-0.4.24 family) NOR `a.subs.mkv` (the current family), and
    still yields plain `a.mkv` and `a.mp4` sources."""
    from vidlift.gui import collect_video_files

    root = tmp_path / "tree"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "a.subs.mp4").write_bytes(b"x")
    (root / "nested" / "a.subs.mkv").write_bytes(b"x")
    (root / "nested" / "a.mkv").write_bytes(b"x")
    (root / "nested" / "a.mp4").write_bytes(b"x")
    collected = collect_video_files(root)
    assert collected == [
        str(root / "nested" / "a.mkv"),
        str(root / "nested" / "a.mp4"),
    ]


# ------------------------------------------------------ 4. MCP vocabulary
def test_v0424_schemas_description_vocabulary():
    """T-NEW-4: every MCP tool schema and description carries the new
    `subrip` / `.subs.mkv` vocabulary and NO `mov_text` / `.mp4`
    token anywhere (a cheap pin against surface drift)."""
    transcribe_desc = TOOL_DESCRIPTIONS["transcribe"]
    assert "subrip" in transcribe_desc
    assert "{stem}.subs.mkv" in transcribe_desc
    for name, blob in list(TOOL_SCHEMAS.items()) + [
        (k, v) for k, v in TOOL_DESCRIPTIONS.items()
    ]:
        text = blob if isinstance(blob, str) else json.dumps(blob)
        assert "mov_text" not in text, f"{name}: stale mov_text token"
        assert ".mp4" not in text, f"{name}: stale .mp4 token"


# ------------------------------------------------------ 5. encode shape
def test_v0424_encode_command_mkv_shape():
    """T-NEW-5: build_encode_command with an embedded-text plan
    contains `-c:s subrip`, ends with a `.mkv` output, and contains
    NO `-movflags` token (D7)."""
    report = _report(subs=[{"codec_name": "subrip", "tags": {"language": "eng"}}])
    plan = build_file_plan(report)
    cmd = build_encode_command(
        ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"), plan, "/fake/mezz.mkv"
    )
    # the codec token: '-c:s' immediately followed by 'subrip'
    assert "-c:s" in cmd
    assert cmd[cmd.index("-c:s") + 1] == "subrip"
    # the container: the last arg (the output) ends .mkv
    assert cmd[-1].endswith(".mkv")
    # D7: the MP4-era faststart flag is gone from the live path
    assert "-movflags" not in cmd
    assert "+faststart" not in " ".join(cmd)
    assert plan.output_path.endswith(".mkv")


# ------------------------------------------------------ 6. sidecar strip
@pytest.mark.usefixtures("engine_on", "no_lock")
def test_v0424_subs_sidecar_derivation(tmp_path):
    """T-NEW-6: the 4-char-strip invariant — run_subs_job deriving its
    sidecar from a `{stem}.subs.mkv` output yields `{stem}.subs.srt`
    (the KEEP contract; byte-safe for the legacy `.mp4` suffix too)."""
    src = tmp_path / "movie.avi"
    src.write_bytes(b"source-bytes")
    runner = FakeRunner(_probe_payload())
    result = run_subs_job(
        str(src),
        ffmpeg="ffmpeg",
        probe_runner=runner,
        runner=runner,
        engine_factory=lambda p: FakeEngine(p),
        workdir=str(tmp_path / "wd"),
    )
    assert result.output_path == str(tmp_path / "movie.subs.mkv")
    assert result.srt_path == str(tmp_path / "movie.subs.srt")
    assert Path(result.srt_path).is_file()


# ------------------------------------------------------ 7. verify scan
def _planned_output(tmp_path, *, with_subs: bool):
    """A real plan + an on-disk output stub, as the verifier probes."""
    report = _report(
        subs=(
            [{"codec_name": "subrip", "tags": {"language": "eng"}}]
            if with_subs
            else []
        ),
        path=str(tmp_path / "movie.avi"),
    )
    plan = build_file_plan(report, output_dir=str(tmp_path / "out"))
    out = Path(plan.output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"output-stub")
    return plan


def _probe_runner(streams):
    def runner(binary, args):
        return 0, json.dumps({"streams": streams}), ""

    return runner


def _video_stream():
    return {"codec_type": "video", "width": 1280, "height": 720}


def test_v0424_verify_subrip_scan(tmp_path):
    """T-NEW-7 (D4): the verify assertion scans `subrip` —
    (a) embedded-text plan + subrip stream → subtitle_ok True;
    (b) embedded-text plan + NO subtitle stream → False with the
        "no subrip stream" detail; and
    (c) no plan + a stray mov_text stream → False with the
        generalized "unexpected subtitle stream(s)" detail naming the
        codec. Fail-open asymmetry preserved throughout (ok stays
        True; reported, never gated)."""
    tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")

    # (a) expected and present
    plan = _planned_output(tmp_path, with_subs=True)
    result = verify_output(
        tools, plan,
        runner=_probe_runner([_video_stream(),
                              {"codec_type": "subtitle", "codec_name": "subrip"}]),
        full_decode=False,
    )
    assert result.ok is True
    assert result.subtitle_expected is True
    assert result.subtitle_ok is True
    assert result.subtitle_detail is None

    # (b) expected and missing
    plan = _planned_output(tmp_path, with_subs=True)
    result = verify_output(
        tools, plan,
        runner=_probe_runner([_video_stream()]),
        full_decode=False,
    )
    assert result.ok is True  # non-gating by design
    assert result.subtitle_ok is False
    assert "no subrip stream" in (result.subtitle_detail or "")

    # (c) unexpected: no plan, a stray mov_text stream in the output
    plan = _planned_output(tmp_path, with_subs=False)
    result = verify_output(
        tools, plan,
        runner=_probe_runner([_video_stream(),
                              {"codec_type": "subtitle", "codec_name": "mov_text"}]),
        full_decode=False,
    )
    assert result.ok is True
    assert result.subtitle_expected is False
    assert result.subtitle_ok is False
    assert "unexpected subtitle stream(s) in the output (mov_text)" in (
        result.subtitle_detail or ""
    )
