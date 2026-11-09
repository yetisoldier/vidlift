"""v0.4.17 Mode B replace-source (opt-in) — contracts at the highest
boundaries (Bob's implementation spec §8, file A).

Groups:

1. Pure naming (`subs_replace_target`): always `{stem}.mp4` beside the
   source; the old extension is never carried; the stem-strip regex is
   the byte-identical twin of `subs_output_path`.
2. Helper happy paths (`replace_subs_output`): rename-before-delete
   keeps every failure mode two-filed; sidecar renames to `{stem}.srt`;
   source-is-mp4 is one atomic move (no delete observable); the
   defensive cross-device arm stages a temp SIBLING, SHA-256-verifies,
   renames, and never leaves the temp behind.
3. Helper refusals / graceful degradation: stranger `{stem}.mp4`
   refuses the WHOLE replace (nothing deleted, nothing moved); stranger
   `{stem}.srt` only downgrades the SIDECAR (mp4 still ships); missing
   output/source refuse pre-mutation; failing deletes/renames keep
   both files and narrate honestly.
4. `run_subs_job` integration (fake engine + fake runner, the
   test_v0414 harness): OFF is byte-identical to v0.4.16.2; ON ships
   the rename; a replace failure never fails the job; cancellation
   after the remux never replaces.

No real ffmpeg, no real whisper, no network (the harnesses are
imported verbatim from tests/test_v0414_subs_job.py — one source of
truth for the seams, the spec's "copied conventions" reading).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from vidlift.run import EngineCancelled
from vidlift.subs_job import (
    SubsJobError,
    replace_subs_output,
    run_subs_job,
    subs_output_path,
    subs_replace_target,
)

# The fake engine/runner seams live with the v0.4.14 suite (the spec's
# "conventions and harnesses are copied" reading: imported, never
# forked). `engine_on` and `no_lock` are imported as REAL pytest
# fixtures (pytest resolves imported fixtures).
from test_v0414_subs_job import (  # noqa: F401
    FakeEngine,
    FakeRunner,
    _probe_payload,
    engine_on,
    no_lock,
)


# ------------------------------------------------- 1. pure naming (D6)
def test_1_suffix_sources_target_stem_mp4():
    """`.avi`/`.mkv`/`.ts`/`.MP4` → `{stem}.mp4` beside the source; the
    old extension is NEVER carried to the target (D6)."""
    assert subs_replace_target("/lib/H/Show.s01e01.avi") == (
        "/lib/H/Show.s01e01.mp4"
    )
    assert subs_replace_target("/x/movie.mkv") == "/x/movie.mp4"
    assert subs_replace_target("/x/cap.ts") == "/x/cap.mp4"
    assert subs_replace_target("/x/cap.MP4") == "/x/cap.mp4"


def test_2_multi_dot_stem_stripped_like_subs_output_path():
    """Eric's repro name: the multi-dot stem strips at the LAST dot —
    regex parity with subs_output_path (one derivation, F-5 lesson)."""
    stem = "show - S03E01 - 35 Hours"
    src = f"/media/{stem}.avi"
    assert subs_replace_target(src) == f"/media/{stem}.mp4"
    # the family relationship holds for the same source, and the
    # `.subs.mp4` infix stays distinct from the replace target
    assert subs_output_path(src) == f"/media/{stem}.subs.mp4"


# ----------------------------------- 2. helper happy paths (+ §6 test 4)
def _stage_pair(tmp_path, *, stem="movie", suffix=".avi"):
    src = tmp_path / f"{stem}{suffix}"
    src.write_bytes(b"source-bytes")
    out = tmp_path / f"{stem}.subs.mp4"
    out.write_bytes(b"muxed-output-bytes")
    return src, out


def test_3_happy_path_moves_mp4_and_sidecar(tmp_path):
    src, out = _stage_pair(tmp_path)
    srt = tmp_path / "movie.subs.srt"
    srt.write_bytes(b"sidecar-bytes")
    log: list[str] = []
    final_out, final_srt = replace_subs_output(
        str(src), str(out), srt_path=str(srt), log_line=log.append
    )
    expected_target = tmp_path / "movie.mp4"
    assert final_out == str(expected_target)
    assert final_srt == str(tmp_path / "movie.srt")
    assert not src.exists()  # source deleted SECOND
    assert expected_target.read_bytes() == b"muxed-output-bytes"
    assert (tmp_path / "movie.srt").read_bytes() == b"sidecar-bytes"
    assert not srt.exists()  # renamed, not copied
    assert any("replace-source: output now at" in line for line in log)


def test_4_no_sidecar_returns_none(tmp_path):
    src, out = _stage_pair(tmp_path)
    final_out, final_srt = replace_subs_output(str(src), str(out))
    assert final_out == str(tmp_path / "movie.mp4")
    assert final_srt is None
    # an srt_path that does NOT exist on disk is equally honest-None
    # (its own fresh pair: the helper is one-shot by design)
    src2, out2 = _stage_pair(tmp_path, stem="second")
    _final_out, final_srt = replace_subs_output(
        str(src2), str(out2), srt_path=str(tmp_path / "ghost.subs.srt")
    )
    assert final_srt is None


def test_5_source_is_mp4_one_atomic_move_no_delete_observable(
    tmp_path, monkeypatch
):
    """Same-path case (D2): target == source path — the single
    os.replace IS the delete-and-move; os.remove must never fire."""
    src, out = _stage_pair(tmp_path, stem="show", suffix=".mp4")
    assert subs_replace_target(str(src)) == str(src)  # the identity case
    removals: list[str] = []

    real_remove = os.remove

    def spy_remove(path, *a, **kw):
        removals.append(os.fspath(path))
        return real_remove(path, *a, **kw)

    monkeypatch.setattr(os, "remove", spy_remove)
    final_out, final_srt = replace_subs_output(str(src), str(out))
    assert final_out == str(src)
    assert Path(final_out).read_bytes() == b"muxed-output-bytes"
    assert not out.exists()  # the .subs.mp4 name is consumed
    assert removals == []  # no delete step for this case
    assert final_srt is None


def _diverge_devices(monkeypatch, *paths) -> None:
    """Make os.stat report a DIFFERENT st_dev for each named path (and
    only those): the cross-device arm becomes reachable without a real
    second filesystem. Every other path (and every is_file()/exists()
    read) stays on the real stat."""
    real_stat = os.stat
    xor_by_path = {
        os.path.realpath(str(path)): mask
        for mask, path in zip((0x10, 0x20), paths)
    }

    def fake_stat(path, *a, **kw):
        res = real_stat(path, *a, **kw)
        real = os.path.realpath(os.fspath(path))
        if real in xor_by_path:
            fields = list(res)
            fields[2] = res.st_dev ^ xor_by_path[real]  # st_dev is index 2
            return os.stat_result(tuple(fields))
        return res

    monkeypatch.setattr(os, "stat", fake_stat)


def test_6_cross_device_arm_stages_verifies_and_sweeps(
    tmp_path, monkeypatch
):
    """Defensive arm: divergent st_dev → temp SIBLING staging, SHA-256
    verification, atomic rename, output unlinked, temp swept — and step
    3 still removes the source (the replace shipped)."""
    src, out = _stage_pair(tmp_path, stem="video title")
    _diverge_devices(monkeypatch, src, out)
    copies: list[tuple[str, str]] = []

    def spy_byte_copy2(src_path, dst_path, *a, **kw):
        # a plain byte-copy stand-in for shutil.copy2: the divergent
        # fake os.stat (tuple-built stat_result) has no ns slots, which
        # breaks stdlib copystat's utime call — metadata fidelity is
        # stdlib-tested; THIS test's subject is staging+verify+rename.
        copies.append((os.fspath(src_path), os.fspath(dst_path)))
        Path(dst_path).write_bytes(Path(src_path).read_bytes())

    monkeypatch.setattr(shutil, "copy2", spy_byte_copy2)
    final_out, final_srt = replace_subs_output(str(src), str(out))
    target = tmp_path / "video title.mp4"
    assert final_out == str(target)
    assert target.read_bytes() == b"muxed-output-bytes"  # byte identity
    assert not out.exists()  # the former output copy is unlinked
    tmp_sibling = str(target) + ".replace-tmp"
    assert not os.path.exists(tmp_sibling)  # the temp never outlives
    assert copies == [(str(out), tmp_sibling)]  # staged at a sibling
    assert not src.exists()  # step 3: the shipped replace removes it


def test_6b_cross_device_copy_cleanup_keeps_both_on_mid_copy_failure(
    tmp_path, monkeypatch
):
    """Disk-full / mid-copy failure analogue: copy2 raising strands only
    the temp (finally sweep), the verified output and the source both
    survive, and the helper refuses loudly."""
    src, out = _stage_pair(tmp_path, stem="failcopy")
    _diverge_devices(monkeypatch, src, out)

    def boom_copy2(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "copy2", boom_copy2)
    with pytest.raises(SubsJobError, match="cross-device copy"):
        replace_subs_output(str(src), str(out))
    assert src.read_bytes() == b"source-bytes"
    assert out.read_bytes() == b"muxed-output-bytes"
    assert not os.path.exists(
        str(tmp_path / "failcopy.mp4.replace-tmp")
    )


# ------------------------- 3. helper refusals / graceful degradation
def test_7_stranger_target_refuses_whole_replace(tmp_path):
    """A pre-existing unrelated {stem}.mp4 refuses EVERYTHING — no move,
    no delete; all three files survive byte-identical (D2)."""
    src, out = _stage_pair(tmp_path, stem="35 Hours")
    stranger = tmp_path / "35 Hours.mp4"
    stranger.write_bytes(b"unrelated-pre-existing")
    with pytest.raises(
        SubsJobError,
        match="35 Hours.mp4 already exists as a different file; "
        "keeping both files",
    ):
        replace_subs_output(str(src), str(out))
    assert src.read_bytes() == b"source-bytes"
    assert out.read_bytes() == b"muxed-output-bytes"
    assert stranger.read_bytes() == b"unrelated-pre-existing"


def test_8_sidecar_collision_keeps_sidecar_mp4_still_ships(tmp_path):
    """A stranger {stem}.srt only downgrades the SIDECAR: the mp4
    replace still ships, the stranger .srt survives, the sidecar keeps
    its .subs.srt name, and an honest warning is narrated (D1)."""
    src, out = _stage_pair(tmp_path, stem="35 Hours")
    sidecar = tmp_path / "35 Hours.subs.srt"
    sidecar.write_bytes(b"sidecar-bytes")
    stranger_srt = tmp_path / "35 Hours.srt"
    stranger_srt.write_bytes(b"unrelated-pre-existing-srt")
    log: list[str] = []
    final_out, final_srt = replace_subs_output(
        str(src), str(out), srt_path=str(sidecar), log_line=log.append
    )
    target = tmp_path / "35 Hours.mp4"
    assert final_out == str(target)
    assert Path(final_out).read_bytes() == b"muxed-output-bytes"  # ships
    assert not src.exists()
    assert stranger_srt.read_bytes() == b"unrelated-pre-existing-srt"
    assert sidecar.read_bytes() == b"sidecar-bytes"
    assert final_srt == str(sidecar)  # HONEST location
    assert any("35 Hours.srt already exists" in line for line in log)


def test_9_missing_output_refuses_source_kept(tmp_path):
    src, _out = _stage_pair(tmp_path)
    missing = tmp_path / "vanish.subs.mp4"
    with pytest.raises(
        SubsJobError,
        match="replace-source refused: verified output is missing:",
    ):
        replace_subs_output(str(src), str(missing))
    assert src.read_bytes() == b"source-bytes"  # source kept


def test_10_missing_source_refuses_output_kept(tmp_path):
    _src, out = _stage_pair(tmp_path)
    missing = tmp_path / "ghost.avi"
    with pytest.raises(
        SubsJobError,
        match="replace-source refused: source is gone:",
    ):
        replace_subs_output(str(missing), str(out))
    assert out.read_bytes() == b"muxed-output-bytes"  # output kept


def test_11_failing_source_delete_keeps_both_and_warns(
    tmp_path, monkeypatch
):
    """os.remove(source) failing AFTER the rename shipped: success
    stands, both target and source exist, warning narrates the residue
    (the user removes the original manually — documented worst case)."""
    src, out = _stage_pair(tmp_path)
    real_remove = os.remove

    def failing_remove(path, *a, **kw):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(str(src)):
            raise OSError("permission denied")
        return real_remove(path, *a, **kw)

    monkeypatch.setattr(os, "remove", failing_remove)
    log: list[str] = []
    final_out, _final_srt = replace_subs_output(
        str(src), str(out), log_line=log.append
    )
    assert final_out == str(tmp_path / "movie.mp4")
    assert Path(final_out).read_bytes() == b"muxed-output-bytes"
    assert src.exists()  # kept — the failed delete's residue
    assert any("source copy could not be removed" in line for line in log)


def test_12_failing_rename_refuses_nothing_deleted_first(
    tmp_path, monkeypatch
):
    """os.replace(output, target) failing: SubsJobError, source and
    output both intact — the rename happens BEFORE any delete (§2)."""
    src, out = _stage_pair(tmp_path)
    real_replace = os.replace

    def failing_replace(src_path, dst_path, *a, **kw):
        moving = (
            os.path.abspath(os.fspath(src_path)) == os.path.abspath(str(out))
            and os.path.abspath(os.fspath(dst_path))
            == os.path.abspath(str(tmp_path / "movie.mp4"))
        )
        if moving:
            raise OSError("destination locked")
        return real_replace(src_path, dst_path, *a, **kw)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(SubsJobError, match="failed to move the output to"):
        replace_subs_output(str(src), str(out))
    assert src.read_bytes() == b"source-bytes"
    assert out.read_bytes() == b"muxed-output-bytes"


# ----------------------- 4. run_subs_job integration (fake harnesses)
def _run_job(src, tmp_path, runner, *, replace_source, should_cancel=None):
    return run_subs_job(
        str(src),
        ffmpeg="ffmpeg",
        probe_runner=runner,
        runner=runner,
        engine_factory=lambda p: FakeEngine(p),
        workdir=str(tmp_path / "wd"),
        replace_source=replace_source,
        should_cancel=should_cancel,
    )


@pytest.mark.usefixtures("engine_on", "no_lock")
def test_13_replace_on_end_to_end_renames_pair(tmp_path):
    """Eric's repro end-to-end: `Show - S03E01 - 35 Hours.avi` →
    `…35 Hours.mp4` + `…35 Hours.srt`, source gone, honest notes."""
    stem = "Show - S03E01 - 35 Hours"
    src = tmp_path / f"{stem}.avi"
    src.write_bytes(b"source-bytes")
    runner = FakeRunner(_probe_payload())
    result = _run_job(src, tmp_path, runner, replace_source=True)
    target = tmp_path / f"{stem}.mp4"
    assert result.output_path == str(target)
    assert result.srt_path == str(tmp_path / f"{stem}.srt")
    assert target.is_file()
    assert (tmp_path / f"{stem}.srt").is_file()
    assert not src.exists()
    assert any(
        note.startswith("replace-source: output now at")
        for note in result.notes
    )


@pytest.mark.usefixtures("engine_on", "no_lock")
def test_14_default_absent_is_byte_identical_to_v0416_behavior(tmp_path):
    """Option OFF (the §10-1 invariant): output stays `{stem}.subs.mp4`,
    source untouched, and NO replace note — the v0.4.16.2 behavior."""
    src = tmp_path / "movie.avi"
    src.write_bytes(b"source-bytes")
    runner = FakeRunner(_probe_payload())
    result = _run_job(src, tmp_path, runner, replace_source=False)
    assert result.output_path == str(tmp_path / "movie.subs.mp4")
    assert Path(result.output_path).read_bytes() == b"muxed-out"
    assert src.read_bytes() == b"source-bytes"
    assert result.srt_path == str(tmp_path / "movie.subs.srt")
    assert not any("replace-source" in note for note in result.notes)


@pytest.mark.usefixtures("engine_on", "no_lock")
def test_15_replace_failure_keeps_success_and_warns(
    tmp_path, monkeypatch
):
    """A replace failure NEVER fails the job: the original `.subs.mp4`
    path rides, both files stay on disk, the Mode A warning note lands."""
    src = tmp_path / "movie.avi"
    src.write_bytes(b"source-bytes")
    runner = FakeRunner(_probe_payload())

    def boom(*a, **kw):
        raise SubsJobError("boom")

    monkeypatch.setattr("vidlift.subs_job.replace_subs_output", boom)
    result = _run_job(src, tmp_path, runner, replace_source=True)
    assert result.output_path == str(tmp_path / "movie.subs.mp4")
    assert Path(result.output_path).read_bytes() == b"muxed-out"
    assert src.read_bytes() == b"source-bytes"  # both files on disk
    assert (
        "WARNING replace-source failed; keeping both files: boom"
        in result.notes
    )


class _ArmingRunner(FakeRunner):
    """Arms a cancel flag when the (fake) remux command runs — the
    observed-after-remux posture of the tail block."""

    def __init__(self, payload, cancel_flag) -> None:
        super().__init__(payload)
        self._flag = cancel_flag

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        argv = tuple(args)
        if "-c:s" in argv and "mov_text" in argv:
            self._flag.armed = True
        return super().__call__(binary, args)


@pytest.mark.usefixtures("engine_on", "no_lock")
def test_16_cancel_after_remux_never_replaces(tmp_path, monkeypatch):
    """Cancellation after the remux lands, before the replace:
    EngineCancelled('cancelled after remux, before replace'); both files
    stay; the replace helper is never attempted (zero rename calls onto
    the {stem}.mp4 target)."""
    src = tmp_path / "movie.avi"
    src.write_bytes(b"source-bytes")

    class CancelLater:
        armed = False

        def __call__(self):
            return self.armed

    flag = CancelLater()
    runner = _ArmingRunner(_probe_payload(), flag)
    attempted: list[int] = []

    real_replace = os.replace

    def spy_replace(src_path, dst_path, *a, **kw):
        # only renames onto a {stem}.mp4 (NOT the .subs.mp4 remux
        # finalize, NOT the .replace-tmp staging) count as a replace
        name = os.path.basename(os.fspath(dst_path))
        if name.endswith(".mp4") and not (
            name.endswith(".subs.mp4") or name.endswith(".muxing.mp4")
        ):
            attempted.append(1)
        return real_replace(src_path, dst_path, *a, **kw)

    monkeypatch.setattr(os, "replace", spy_replace)
    with pytest.raises(EngineCancelled) as excinfo:
        _run_job(
            src, tmp_path, runner, replace_source=True, should_cancel=flag
        )
    assert str(excinfo.value) == "cancelled after remux, before replace"
    assert src.is_file()
    assert (tmp_path / "movie.subs.mp4").is_file()
    assert (tmp_path / "movie.subs.srt").is_file()
    assert attempted == []


@pytest.mark.usefixtures("engine_on", "no_lock")
def test_17_no_cancel_observer_is_inert_replace_ships(tmp_path):
    """CLI posture (should_cancel=None): the observe is inert by
    contract and the replace ships normally."""
    src = tmp_path / "movie.avi"
    src.write_bytes(b"source-bytes")
    runner = FakeRunner(_probe_payload())
    result = _run_job(src, tmp_path, runner, replace_source=True)
    assert result.output_path == str(tmp_path / "movie.mp4")
    assert not src.exists()

# ----------------------------------------------------------------- fakes
class _CapturingRegistry:
    """The tool_transcribe enqueue seam (test 21)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def enqueue(self, **kw):
        self.calls.append(kw)
        return type("Record", (), {"job_id": "VJ1"})()


class _NotifyExecutor:
    def notify_new_work(self):
        pass


# ------------------------------------------------------------- 5. CLI
def test_18_subs_argparse_replace_source_default_off_flag_on():
    """The tests/test_cli_v049 N1/N2 pattern, Mode B edition: absent
    means OFF; the flag parses True."""
    from vidlift import cli

    args = cli.build_parser().parse_args(["subs", "movie.avi"])
    assert args.replace_source is False
    args = cli.build_parser().parse_args(
        ["subs", "movie.avi", "--replace-source"]
    )
    assert args.replace_source is True


def test_19_cmd_subs_threads_flag_and_refused_replace_exits_zero(
    tmp_path, monkeypatch, capsys
):
    """_cmd_subs passes replace_source into run_subs_job's kwarg, and a
    mocked success carrying a refused-replace note exits 0 (refusal ≠
    failure — the honest-note channel prints and the job succeeded)."""
    from vidlift import cli

    src = tmp_path / "35 Hours.avi"
    src.write_bytes(b"x")
    captured: dict = {}

    def fake_run_subs_job(path, **kw):
        captured.update(kw)
        from vidlift.subs_job import SubsJobResult

        return SubsJobResult(
            source_path=path,
            output_path=str(tmp_path / "35 Hours.mp4"),
            srt_path=str(tmp_path / "35 Hours.srt"),
            cue_count=2,
            language="en",
            language_confidence=0.9,
            model="medium",
            notes=[
                "WARNING replace-source failed; keeping both files: boom"
            ],
        )

    monkeypatch.setattr("vidlift.subs_job.run_subs_job", fake_run_subs_job)
    monkeypatch.setattr(
        cli, "find_tools", lambda **k: type("T", (), {
            "ffmpeg": "ffmpeg", "ffprobe": "ffprobe"
        })()
    )
    rc = cli.main(["subs", str(src), "--replace-source"])
    assert rc == 0  # a refused replace is still a succeeded job
    assert captured["replace_source"] is True
    out = capsys.readouterr().out
    assert "35 Hours.mp4" in out  # the renamed location prints
    assert (
        "note: WARNING replace-source failed; keeping both files: boom"
        in out
    )

    # the OFF posture threads False (no fabrication)
    captured.clear()
    rc = cli.main(["subs", str(src)])
    assert rc == 0
    assert captured["replace_source"] is False


# ------------------------------------------------ 6. MCP/registry plumbing
def test_20_transcribe_schema_validates_replace_source():
    """The additive boolean validates, type-checks, and absent-defaults."""
    import pytest as _pytest

    from vidlift.mcp_bridge.schemas import InvalidInput, validate_tool_args

    args = validate_tool_args(
        "transcribe", {"paths": ["x"], "replace_source": True}
    )
    assert args["replace_source"] is True
    # absent validates (the absent-defaults rule)
    validate_tool_args("transcribe", {"paths": ["x"]})
    # a non-boolean is refused at the schema boundary
    with _pytest.raises(InvalidInput) as excinfo:
        validate_tool_args("transcribe", {"paths": ["x"], "replace_source": "yes"})
    assert excinfo.value.field == "replace_source"


def test_21_tool_transcribe_enqueues_replace_source_option(tmp_path):
    """tool_transcribe maps the flag into the job's options dict — set
    when present, False when absent (no fabrication)."""
    from vidlift.mcp_bridge.tools import tool_transcribe

    media = tmp_path / "movie.avi"
    media.write_bytes(b"x")
    registry = _CapturingRegistry()

    class _Services:
        pass

    services = _Services()
    services.registry = registry
    services.executor = _NotifyExecutor()

    tool_transcribe(
        services,
        {"paths": [str(media)], "replace_source": True},
    )
    assert registry.calls[0]["options"]["replace_source"] is True

    tool_transcribe(services, {"paths": [str(media)]})
    assert registry.calls[1]["options"]["replace_source"] is False


class _LiveRecord:
    """The JobRecord seam _execute_subs_job needs (live-state box)."""

    def __init__(self, options: dict, path: str) -> None:
        self.options = options
        self.paths = [path]
        self.job_id = "VJ1"
        self._live: dict = {}

    def set_live(self, key, value) -> None:
        self._live[key] = value

    def get_live(self, key):
        return self._live.get(key)

    def touch(self) -> None:
        pass


class _RegistryPin:
    """The registry seam _execute_subs_job re-pins records through."""

    def __init__(self, record) -> None:
        self._jobs = {record.job_id: record}


def _drive_executor(tmp_path, monkeypatch, options: dict, output_stem: str):
    from vidlift.mcp_bridge.jobs import CancelFlag
    from vidlift.mcp_bridge.jobs import _execute_subs_job
    from vidlift.subs_job import SubsJobResult

    path = str(tmp_path / "movie.avi")
    Path(path).write_bytes(b"x")
    record = _LiveRecord(options, path)
    captured: dict = {}

    def fake_run_subs_job(job_path, **kw):
        captured.update(kw)
        return SubsJobResult(
            source_path=job_path,
            output_path=str(tmp_path / f"{output_stem}.mp4"),
            srt_path=str(tmp_path / f"{output_stem}.srt"),
            cue_count=2,
            language="en",
            language_confidence=0.9,
            model="medium",
            device="",
            notes=["replace-source: output now at x"],
        )

    monkeypatch.setattr("vidlift.subs_job.run_subs_job", fake_run_subs_job)
    monkeypatch.setattr(
        "vidlift.run.find_tools",
        lambda **k: type("T", (), {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"})(),
    )

    class _Services:
        pass

    services = _Services()
    services.registry = _RegistryPin(record)
    services.state_dir = str(tmp_path / "state")
    services.command_runner = None
    services.probe_runner = None

    outcome = _execute_subs_job(record, services, CancelFlag())
    return outcome, captured


def test_22_executor_forwards_replace_source_absent_defaults_false(
    tmp_path, monkeypatch
):
    """_execute_subs_job forwards bool(options.get('replace_source',
    False)): present-True flows True; a legacy record without the key
    flows False (the absent-defaults rule)."""
    outcome, captured = _drive_executor(
        tmp_path, monkeypatch, {"replace_source": True}, "35 Hours"
    )
    assert outcome.ok
    assert captured["replace_source"] is True

    outcome, captured = _drive_executor(
        tmp_path, monkeypatch, {}, "movie"  # legacy record: key absent
    )
    assert outcome.ok
    assert captured["replace_source"] is False


def test_24_terminal_echo_carries_renamed_path_same_payload_shape(
    tmp_path, monkeypatch
):
    """The executor's JobOutcome result dict keys are identical to
    v0.4.16.2 and the renamed output_path rides honestly."""
    outcome, _captured = _drive_executor(
        tmp_path, monkeypatch, {"replace_source": True}, "35 Hours"
    )
    assert outcome.ok
    assert set(outcome.result.keys()) == {
        "output_path", "srt_path", "language", "cue_count",
        "model", "device", "notes",
    }
    assert outcome.result["output_path"] == str(tmp_path / "35 Hours.mp4")
    assert outcome.output_path == str(tmp_path / "35 Hours.mp4")
    assert "replace-source: output now at x" in outcome.result["notes"]


def test_23_client_local_enqueue_subs_option_roundtrip(tmp_path, monkeypatch):
    """The real LocalBridgeClient writes the option into the registry
    record: replace_source=True lands True; the no-kwarg call keeps
    writing False (back-compat for existing scripted callers)."""
    from vidlift.mcp_bridge.client_local import LocalBridgeClient

    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    media = tmp_path / "movie.avi"
    media.write_bytes(b"x")
    client = LocalBridgeClient(
        state_dir=tmp_path / "state", start_executor=False
    )
    try:
        ids = client.enqueue_subs([str(media)], replace_source=True)
        record = client.registry.require(ids[0])
        assert record.options["replace_source"] is True

        ids = client.enqueue_subs([str(media)])
        record = client.registry.require(ids[0])
        assert record.options["replace_source"] is False
    finally:
        client.stop()
