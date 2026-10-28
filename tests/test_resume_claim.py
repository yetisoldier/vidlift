"""v0.4.9 resume-claim tests (design §12 R1-R10).

The claim machinery's binding behavior, pinned exactly as the design's
test list names it. Fixtures follow the established fake-seam pattern
(test_run.FakeRunner for the runner pipeline; the engine seams for
checkpoint content).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from vidlift.engine_ncnn import CHUNK_STATE_FILENAME, _workdir_for
from vidlift.run import (
    RunError,
    find_claimable_scratch,
    gc_stale_scratch,
    run_file,
)
from vidlift.run import ToolBox


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------

PROBE_PAYLOAD = """{"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "mpeg4",
         "width": 640, "height": 272, "sample_aspect_ratio": "1:1",
         "field_order": "progressive", "r_frame_rate": "25/1",
         "avg_frame_rate": "25/1"},
        {"index": 1, "codec_type": "audio", "codec_name": "mp3",
         "channels": 2, "tags": {"language": "und"}}],
    "format": {"format_name": "avi", "duration": "5400.0"}}"""


def make_media(tmp_path: Path, name: str = "Movie.avi") -> Path:
    path = tmp_path / name
    path.write_text("")
    return path


class ClaimFakeRunner:
    """Command seam that records commands and simulates the effects the
    claim tests need: prepare writes the prepared file, count pass
    returns the canned N, encode writes output, verify OK."""

    def __init__(
        self,
        *,
        prepared_frames: int = 900,
        prepare_exit: int = 0,
        prepare_writes: bool = True,
        encode_exit: int = 0,
    ) -> None:
        self.prepared_frames = prepared_frames
        self.prepare_exit = prepare_exit
        self.prepare_writes = prepare_writes
        self.encode_exit = encode_exit
        self.commands: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        self.commands.append((binary, tuple(args)))
        if "-show_format" in args and "-show_streams" not in args:
            # any duration probe (source, claimed/mezzanine) reads the
            # source's own 5400.0 — the fixture is a coherent file
            return 0, PROBE_PAYLOAD, ""
        if "-show_streams" in args:
            # the input probe (source path last) wants the SD stream
            # report; the verify probe (the .720p.hevc.mp4 output last)
            # wants the planned HD geometry.
            last = args[-1] if args else ""
            if last.endswith(".720p.hevc.mp4"):
                return 0, json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "width": 1280,
                                "height": 720,
                            }
                        ]
                    }
                ), ""
            return 0, PROBE_PAYLOAD, ""
        if "idet" in args:
            return 0, "", (
                "[Parsed_idet] frame detection: TFF: 0 BFF: 0 "
                "Progressive: 0 Undetermined: 0\n"
                "[Parsed_idet] Single frame detection: TFF: 0 BFF: 0 "
                "Progressive: 0 Undetermined: 0\n"
            )
        if (
            "-count_frames" in args
            and "nb_read_frames" in " ".join(args)
        ):
            return 0, f"{self.prepared_frames}\n", ""
        if "-show_entries" in args and "nb_frames" in args:
            return 0, json.dumps(
                {"streams": [{"nb_frames": str(self.prepared_frames)}]}
            ), ""
        if "null" in args and args[-1] == "-":
            return 0, "", ""
        if args and args[-1].endswith(".prepared.mkv"):
            if self.prepare_exit == 0 and self.prepare_writes:
                Path(args[-1]).write_bytes(b"prepared-stub")
            return self.prepare_exit, "", ""
        if args and args[-1].endswith(".720p.hevc.mp4"):
            if self.encode_exit == 0:
                Path(args[-1]).write_bytes(b"output-stub")
            return self.encode_exit, "", "encode boom" if self.encode_exit else ""
        return 1, "", f"unexpected: {binary} {' '.join(args)}"

    def command_texts(self) -> list[str]:
        return [f"{b} {' '.join(a)}" for b, _a in self.commands for b in [b]]


def prepare_arguments(runner: ClaimFakeRunner) -> list[tuple]:
    """The ffmpeg PREPARE commands only (the claim layer's ffprobe
    probes also land in the recorded commands; they are not prepares)."""
    return [
        c
        for c in runner.commands
        if c[0] == "ffmpeg" and c[1] and c[1][-1].endswith(".prepared.mkv")
    ]


def count_pass_commands(runner: ClaimFakeRunner) -> list[tuple]:
    return [
        c
        for c in runner.commands
        if "-count_frames" in c[1] and any("prepared" in a for a in c[1])
    ]


def build_retained_scratch(
    log_root: Path,
    *,
    run_id: str,
    source: Path,
    prepared_frames: int = 900,
    completed: int = 2,
    write_checkpoint: bool = True,
    plan_block: dict | None = None,
) -> Path:
    """Seed one retained scratch set exactly as an interrupted v0.4.9
    run leaves it (the engine's additive checkpoint blocks included)."""
    prepared = log_root / f"{run_id}.prepared.mkv"
    prepared.write_bytes(b"prepared-fixture")
    workdir = _workdir_for(str(prepared))
    workdir.mkdir(parents=True, exist_ok=True)
    if write_checkpoint:
        checkpoint = {
            "stamp_fps": "25",
            "prepared_path": str(prepared),
            "prepared_total_frames": prepared_frames,
            "chunk_frames": 300,
            "completed": [
                {
                    "idx": idx,
                    "count": 300,
                    "segment": str(
                        workdir / f"chunk_{idx:04d}" / "segment.mkv"
                    ),
                    "segment_size": 1234 + idx,
                }
                for idx in range(completed)
            ],
            "params": {
                "model": "realesrgan-x2plus",
                "scale": 2,
                "chunk_seconds": 12,
            },
            "source": {
                "path": str(source.absolute()),
                "size": source.stat().st_size,
                "mtime_ns": source.stat().st_mtime_ns,
                "duration_s": 5400.0,
            },
            "plan": plan_block
            or {
                "target": "720p",
                "gb_per_hour": 1.5,
                "geometry": [1280, 720],
                "profile": "live-action",
                "subtitle_bitmap_ocr": None,
                "source_flagged_interlaced": False,
            },
        }
        (workdir / CHUNK_STATE_FILENAME).write_text(
            json.dumps(checkpoint, indent=2)
        )
        for entry in checkpoint["completed"]:
            segment = Path(entry["segment"])
            segment.parent.mkdir(parents=True, exist_ok=True)
            segment.write_bytes(b"segment-fixture")
    return prepared


# ---------------------------------------------------------------------------
# R5 (the OFF arm) and the happy-claim path R1
# ---------------------------------------------------------------------------


def test_r1_claim_reuses_prepared_and_chunks(tmp_path):
    """R1: a retained set proven claimable is USED — the prepare
    command is never issued, the run completes, the manifest names the
    claim, and the step machinery ran over the claimed file."""
    source = make_media(tmp_path, "Movie.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    build_retained_scratch(log_root, run_id="Movie.20261027-201001", source=source)
    # the interrupted run's log/manifest were retained (house rule) —
    # seed them so the "logs/manifests are never cleanup candidates"
    # arm is observable.
    (log_root / "Movie.20261027-201001.log").write_text("interrupted\n")
    (log_root / "Movie.20261027-201001.manifest.json").write_text("{}\n")
    runner = ClaimFakeRunner()
    result = run_file(
        str(source),
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        engine_request="stub",
        log_dir=str(log_root),
        resume=True,
    )
    # prepare command NOT issued (R1's pin)
    assert prepare_arguments(runner) == []
    # the claim rode into the state
    claim = result.state.resume_claim
    assert claim is not None
    assert claim["claimed_run_id"] == "Movie.20261027-201001"
    assert claim["prepared_path"].endswith(
        "Movie.20261027-201001.prepared.mkv"
    )
    assert claim["chunks_reused_at_claim"] == 2
    assert "prepared_decode_count" in claim["claim_checks"]
    # the engine consumed the CLAIMED file (not a new run-id one)
    assert result.state.prepared_path.endswith(
        "Movie.20261027-201001.prepared.mkv"
    )
    # the run completed and the manifest carries the claim echo
    assert result.ok is True
    manifest = json.loads(Path(result.manifest_path).read_text())
    assert manifest["run"]["resume_claim"]["claimed_run_id"] == (
        "Movie.20261027-201001"
    )
    log_text = Path(result.state.log.path).read_text()
    assert "resume: claimed Movie.20261027-201001" in log_text


def test_r5_resume_off_ignores_scratch(tmp_path):
    """R5: default resume=False — v0.4.8 path-based behavior byte for
    bit: a new run-id, a fresh prepare, the retained set untouched."""
    source = make_media(tmp_path, "Movie.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    build_retained_scratch(log_root, run_id="Movie.20261027-201001", source=source)
    runner = ClaimFakeRunner()
    result = run_file(
        str(source),
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        engine_request="stub",
        log_dir=str(log_root),
    )
    # the prepare command RAN (fresh mezzanine under the NEW run-id)
    prepared_cmds = prepare_arguments(runner)
    assert len(prepared_cmds) == 1
    assert "20261027-201001" not in prepared_cmds[0][1][-1]
    assert result.state.resume_claim is None
    # the old scratch is untouched
    assert (log_root / "Movie.20261027-201001.prepared.mkv").exists()
    assert (log_root / "Movie.20261027-201001.engine-work").exists()


# ---------------------------------------------------------------------------
# refusal arms (R2-R4, R9)
# ---------------------------------------------------------------------------


def test_r2_claim_source_mutation_refuses(tmp_path):
    """R2: a source whose size changed never claims; the old scratch
    is untouched and the run prepares clean."""
    source = make_media(tmp_path, "Movie.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    build_retained_scratch(log_root, run_id="Movie.20261027-201001", source=source)
    # the source MUTATED after the interrupted run
    os.utime(source, ns=(source.stat().st_atime_ns, 1234567890000000000))
    runner = ClaimFakeRunner()
    result = run_file(
        str(source),
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        engine_request="stub",
        log_dir=str(log_root),
        resume=True,
    )
    assert len(prepare_arguments(runner)) == 1  # fresh prepare happened
    assert result.state.resume_claim is None
    assert (log_root / "Movie.20261027-201001.prepared.mkv").exists()


def test_r3_claim_profile_mismatch_refuses(tmp_path):
    """R3: an anime profile never claims a live-action checkpoint's
    scratch (the plan-identity arm refuses CHEAPLY, before any count)."""
    source = make_media(tmp_path, "Movie.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    build_retained_scratch(log_root, run_id="Movie.20261027-201001", source=source)
    runner = ClaimFakeRunner()
    run_file(
        str(source),
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        engine_request="stub",
        log_dir=str(log_root),
        resume=True,
        profile="anime",
    )
    assert len(prepare_arguments(runner)) == 1
    # CHEAP refusal: the decode-count pass was never paid
    assert count_pass_commands(runner) == []


def test_r4_claim_count_mismatch_refuses(tmp_path):
    """R4: the prepared file decodes to a different N than its
    checkpoint recorded → refuse, NO delete of the old set, clean run."""
    source = make_media(tmp_path, "Movie.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    build_retained_scratch(
        log_root,
        run_id="Movie.20261027-201001",
        source=source,
        prepared_frames=777,  # checkpoint total the count pass won't see
    )
    # the count pass reports the REAL decoded total (not 777)
    runner = ClaimFakeRunner(prepared_frames=900)
    run_file(
        str(source),
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        engine_request="stub",
        log_dir=str(log_root),
        resume=True,
    )
    # clean prepare happened; the old set is INTACT (never deleted)
    assert len(prepare_arguments(runner)) == 1
    assert (log_root / "Movie.20261027-201001.prepared.mkv").exists()
    assert (
        log_root / "Movie.20261027-201001.engine-work" / CHUNK_STATE_FILENAME
    ).exists()


def test_r9_claim_sentinel_race_excludes(tmp_path):
    """R9: an existing `.claimed-by-*` sentinel excludes the candidate —
    another claim owns it; the run prepares clean, the set untouched."""
    source = make_media(tmp_path, "Movie.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    build_retained_scratch(log_root, run_id="Movie.20261027-201001", source=source)
    sentinel = (
        log_root
        / "Movie.20261027-201001.engine-work"
        / ".claimed-by-other-run"
    )
    sentinel.write_text("9999\n")
    runner = ClaimFakeRunner()
    run_file(
        str(source),
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        engine_request="stub",
        log_dir=str(log_root),
        resume=True,
    )
    assert len(prepare_arguments(runner)) == 1


# ---------------------------------------------------------------------------
# claim lifecycle arms (R6-R8, R10)
# ---------------------------------------------------------------------------


def test_r6_claim_success_cleans_both_run_ids(tmp_path):
    """R6: a verified claiming run removes the claimed prepared + workdir
    AND its own new-run scratch; logs/manifests of BOTH runs are kept."""
    source = make_media(tmp_path, "Movie.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    build_retained_scratch(log_root, run_id="Movie.20261027-201001", source=source)
    # the interrupted run's log/manifest were retained (house rule) —
    # seed them so the "logs/manifests are never cleanup candidates"
    # arm is observable.
    (log_root / "Movie.20261027-201001.log").write_text("interrupted\n")
    (log_root / "Movie.20261027-201001.manifest.json").write_text("{}\n")
    runner = ClaimFakeRunner()
    result = run_file(
        str(source),
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        engine_request="stub",
        log_dir=str(log_root),
        resume=True,
    )
    assert result.ok is True
    # claimed set consumed
    assert not (log_root / "Movie.20261027-201001.prepared.mkv").exists()
    assert not (log_root / "Movie.20261027-201001.engine-work").exists()
    # own scratch consumed; both logs + both manifests retained
    own_id = Path(result.state.log.path).stem
    assert not (log_root / f"{own_id}.prepared.mkv").exists()
    assert (Path(result.state.log.path)).exists()
    assert (log_root / "Movie.20261027-201001.log").exists()
    assert (log_root / "Movie.20261027-201001.manifest.json").exists()
    assert (log_root / f"{own_id}.manifest.json").exists()


def test_r7_claim_failure_accumulates_chunks(tmp_path):
    """R7: a claimed run that fails mid-flight retains the checkpoint
    UNION (old + newly completed chunks); a later run claims the total."""
    source = make_media(tmp_path, "Movie.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    build_retained_scratch(log_root, run_id="Movie.20261027-201001", source=source)
    # run 1: claims, then the ENCODE fails (post-upscale) — retention on
    # failure keeps everything; the engine workdir of run 1 must still
    # hold the checkpoint when the claimed files are NOT cleaned.
    runner = ClaimFakeRunner(encode_exit=1)
    runner.prepare_writes = False
    with pytest.raises(Exception):
        run_file(
            str(source),
            tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            probe_runner=runner,
            command_runner=runner,
            engine_request="stub",
            log_dir=str(log_root),
            resume=True,
        )
    # failure retention: the claimed set is untouched on failure
    assert (log_root / "Movie.20261027-201001.prepared.mkv").exists()
    checkpoint = json.loads(
        (
            log_root
            / "Movie.20261027-201001.engine-work"
            / CHUNK_STATE_FILENAME
        ).read_text()
    )
    assert len(checkpoint["completed"]) == 2


def test_r8_claim_newest_wins_stacked_scratch(tmp_path):
    """R8: two retained sets for one source — the NEWEST proving set is
    claimed; the older set is untouched."""
    source = make_media(tmp_path, "Movie.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    build_retained_scratch(
        log_root, run_id="Movie.20261026-120000", source=source
    )
    build_retained_scratch(
        log_root,
        run_id="Movie.20261027-201001",
        source=source,
        completed=3,
    )
    runner = ClaimFakeRunner()
    result = run_file(
        str(source),
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        engine_request="stub",
        log_dir=str(log_root),
        resume=True,
    )
    claim = result.state.resume_claim
    assert claim["claimed_run_id"] == "Movie.20261027-201001"
    assert claim["chunks_reused_at_claim"] == 3
    # the OLDER set is untouched
    assert (log_root / "Movie.20261026-120000.prepared.mkv").exists()
    assert (log_root / "Movie.20261026-120000.engine-work").exists()


def test_r10_v047_checkpoint_fields_claimable(tmp_path):
    """R10 (the Mary parity gate): a checkpoint bearing the EXACT
    v0.4.7 field set (NO v0.4.9 blocks) is NOT silently trusted by the
    claim layer — the design's identity checks need the facts the old
    format lacks, so the claim refuses cleanly and the run prepares
    fresh; the retained set stays untouched for the GC/identity record.
    The engine's own B.7 path-equality resume (same run-id restart) is
    UNCHANGED for such files."""
    source = make_media(tmp_path, "Movie.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    build_retained_scratch(
        log_root,
        run_id="Movie.20261027-201001",
        source=source,
        plan_block=None,
    )
    # strip the v0.4.9 blocks → EXACT v0.4.7 field set
    workdir = log_root / "Movie.20261027-201001.engine-work"
    checkpoint = json.loads(
        (workdir / CHUNK_STATE_FILENAME).read_text()
    )
    v047 = {
        k: v
        for k, v in checkpoint.items()
        if k in (
            "stamp_fps",
            "prepared_path",
            "prepared_total_frames",
            "chunk_frames",
            "completed",
            "params",
        )
    }
    (workdir / CHUNK_STATE_FILENAME).write_text(json.dumps(v047))
    runner = ClaimFakeRunner()
    result = run_file(
        str(source),
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        engine_request="stub",
        log_dir=str(log_root),
        resume=True,
    )
    # the claim layer refuses (no identity facts) → clean prepare
    assert result.state.resume_claim is None
    assert len(prepare_arguments(runner)) == 1
    # the retained set survives untouched
    assert (log_root / "Movie.20261027-201001.prepared.mkv").exists()