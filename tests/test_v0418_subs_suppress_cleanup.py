"""v0.4.18 WS2/WS3 — opt-in suppress_srt (Mode B) + .subs-work cleanup.

Bob spec §12 file B. Contracts:

  Group 4 — run_subs_job suppression (fake engine + fake runner, the
            test_v0414_subs_job harness):
    14 default OFF → byte-identical v0.4.17 (sidecar kept, no S-2 note)
    15 ON → mux ok → sidecar GONE, srt_path "", S-2 note, mux argv had
         the srt path (removal happened after the mux)
    16 ON + mux failure → SubsJobError, sidecar KEPT (no suppression)
    17 ON + removal OSError → success stands, sidecar present, honest
         srt_path, S-9 in notes
    18 ON + replace_source → single {stem}.mp4, NO .srt anywhere,
         source deleted, srt_path "", replace narrated (helper's
         sidecar step a demonstrated no-op)
  Group 5 — cleanup_subs_scratch + integration:
    19 happy default-workdir run → {out}.subs-work GONE, S-16 note
    20 custom workdir → survives intact (path guard), no note
    21 mux failure → scratch retained; cancel mid-transcribe →
         EngineCancelled, scratch retained
    22 replace ON → scratch (named off the PRE-rename output) removed
    23 rmtree OSError → success stands, S-16 warning note, dir remains

Conventions: fakes only — no ffmpeg/ffprobe/network/GPU. Import the
house fakes from test_v0414_subs_job (FakeEngine / FakeRunner /
probe payload); symlink-free; nothing touches run.py.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vidlift.run import EngineCancelled
from vidlift.subs_job import (
    SubsJobError,
    run_subs_job,
)

from test_v0414_subs_job import (  # house fakes (spec §12 conventions)
    FakeEngine,
    FakeRunner,
    _probe_payload,
)


# ---------------------------------------------------------------- harness
@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.delenv("VIDLIFT_TRANSCRIPTION_ENGINE", raising=False)


def _lock():
    return type("L", (), {
        "acquire": staticmethod(lambda on_wait=None: True),
        "release": staticmethod(lambda: None),
    })()


def _media(tmp_path: Path) -> str:
    src = tmp_path / "movie.avi"
    src.write_bytes(b"source-bytes")
    return str(src)


def _job(**overrides):
    """The caller-facing seam defaults for one fake subs job; kwargs
    override (suppress_srt/replace_source etc. ride as normal kwargs)."""
    defaults = dict(
        ffmpeg="ffmpeg",
        engine_factory=lambda p: FakeEngine(p),
    )
    defaults.update(overrides)
    return defaults


def _mux_args(runner) -> list:
    return [argv for _b, argv in runner.commands
            if "-c:s" in argv and "mov_text" in argv]


# ---------------------------------------------------------------- group 4
def test_14_default_off_is_byte_identical_v0417(tmp_path, engine_on):
    """The option-OFF invariant: no kwarg → the sidecar is written and
    KEPT, srt_path points at it, the mux argv carries the srt path, and
    NO suppression note exists — the pre-0.4.18 deliverable set."""
    runner = FakeRunner(_probe_payload())
    result = run_subs_job(
        _media(tmp_path),
        probe_runner=runner,
        runner=runner,
        **_job(),
    )
    sidecar = tmp_path / "movie.subs.srt"
    assert sidecar.is_file()
    assert result.srt_path == str(sidecar)
    assert any(
        "movie.subs.srt" == os.path.basename(arg)
        for argv in _mux_args(runner)
        for arg in argv
    )
    assert not any(
        "suppressed" in note for note in result.notes
    ), result.notes


def test_15_suppress_on_removes_sidecar_after_successful_mux(
    tmp_path, engine_on
):
    runner = FakeRunner(_probe_payload())
    result = run_subs_job(
        _media(tmp_path),
        probe_runner=runner,
        runner=runner,
        suppress_srt=True,
        **_job(),
    )
    output = tmp_path / "movie.subs.mp4"
    sidecar = tmp_path / "movie.subs.srt"
    assert output.is_file()  # the deliverable stands
    assert not sidecar.exists()  # the sidecar is GONE
    assert result.srt_path == ""  # the deliberately-empty sentinel
    assert (
        "srt sidecar suppressed after a successful mux" in result.notes
    )  # S-2
    # the removal happened AFTER the mux: the recorded mux argv still
    # carries the srt input (v0.4.15.3's phase-seam is intact)
    assert any(
        any(arg.endswith("movie.subs.srt") for arg in argv)
        for argv in _mux_args(runner)
    )


def test_16_suppress_on_mux_failure_keeps_sidecar_and_fails(
    tmp_path, engine_on
):
    runner = FakeRunner(_probe_payload(), remux_fails=True)
    with pytest.raises(SubsJobError):
        run_subs_job(
            _media(tmp_path),
            probe_runner=runner,
            runner=runner,
            suppress_srt=True,
            **_job(),
        )
    # mplex raised BEFORE suppression ran: the .subs.srt survives as the
    # fallback deliverable (flag-independent)
    assert (tmp_path / "movie.subs.srt").is_file()


def test_17_suppress_removal_oserror_keeps_sidecar_and_succeeds(
    tmp_path, engine_on, monkeypatch
):
    srt_target = tmp_path / "movie.subs.srt"
    real_remove = os.remove

    def _blocked_remove(path, *_a, **_k):
        if os.path.abspath(str(path)) == os.path.abspath(str(srt_target)):
            raise OSError("locked by an antivirus scan")
        return real_remove(path, *_a, **_k)

    monkeypatch.setattr("vidlift.subs_job.os.remove", _blocked_remove)
    runner = FakeRunner(_probe_payload())
    result = run_subs_job(
        _media(tmp_path),
        probe_runner=runner,
        runner=runner,
        suppress_srt=True,
        **_job(),
    )
    # best-effort: the success stands, the sidecar stays, srt_path is
    # the honest truth, and S-9 rides the notes
    assert (tmp_path / "movie.subs.mp4").is_file()
    assert srt_target.is_file()
    assert result.srt_path == str(srt_target)
    assert any(
        note.startswith(
            "WARNING suppress-srt: could not remove "
        )
        and "movie.subs.srt" in note
        for note in result.notes
    )


def test_18_suppress_plus_replace_single_mp4_no_srt(
    tmp_path, engine_on
):
    runner = FakeRunner(_probe_payload())
    result = run_subs_job(
        _media(tmp_path),
        probe_runner=runner,
        runner=runner,
        suppress_srt=True,
        replace_source=True,
        **_job(),
    )
    assert result.output_path == str(tmp_path / "movie.mp4")
    assert (tmp_path / "movie.mp4").is_file()
    assert not (tmp_path / "movie.avi").exists()  # source deleted
    # NO .srt anywhere (suppression ran first; the replace helper's
    # sidecar step found nothing and no-oped)
    srts = [p.name for p in tmp_path.iterdir() if p.name.endswith(".srt")]
    assert srts == [], srts
    assert result.srt_path == ""  # the honest sentinel
    assert (
        "srt sidecar suppressed after a successful mux" in result.notes
    )  # S-2
    assert any(note.startswith("replace-source: output now at ")
               for note in result.notes)


# ---------------------------------------------------------------- group 5
def test_19_default_workdir_scratch_removed_after_success(
    tmp_path, engine_on
):
    """K-5: NO workdir kwarg → the default scratch
    {output}.subs-work is created by the core seam and GONE after the
    job, with the S-16 success note in the notes."""
    runner = FakeRunner(_probe_payload())
    scratch = tmp_path / "movie.subs.mp4.subs-work"
    result = run_subs_job(
        _media(tmp_path),
        probe_runner=runner,
        runner=runner,
        **_job(),
    )
    assert not scratch.exists(), "the .subs-work scratch dir must be gone"
    assert any(
        note.startswith("cleanup subs scratch: ")
        and note.endswith("movie.subs.mp4.subs-work")
        for note in result.notes
    )


def test_20_custom_workdir_is_never_touched(tmp_path, engine_on):
    """The path guard (D10): a caller-supplied (or harness) workdir is
    the caller's own directory — cleanup never touches it."""
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "keepme.txt").write_text("important")
    runner = FakeRunner(_probe_payload())
    result = run_subs_job(
        _media(tmp_path),
        probe_runner=runner,
        runner=runner,
        workdir=str(mine),
        **_job(),
    )
    assert (mine / "keepme.txt").read_text() == "important"
    assert mine.is_dir()
    assert not any(
        "cleanup subs scratch" in note for note in result.notes
    )


def test_21_failure_and_cancel_retain_scratch(tmp_path, engine_on):
    """Failure/cancel retention (the D11 guardrail): a mux failure and
    a mid-transcribe cancel both keep the scratch for diagnosis."""
    # (a) mux failure
    fail_runner = FakeRunner(_probe_payload(), remux_fails=True)
    scratch = tmp_path / "movie.subs.mp4.subs-work"
    with pytest.raises(SubsJobError):
        run_subs_job(
            _media(tmp_path),
            probe_runner=fail_runner,
            runner=fail_runner,
            **_job(),
        )
    assert scratch.is_dir()
    # (b) cancel mid-transcribe: should_cancel True → the core's
    # per-segment check raises EngineCancelled before the mux.
    runner = FakeRunner(_probe_payload())
    scratch2 = tmp_path / "movie.subs.mp4.subs-work"
    with pytest.raises(EngineCancelled):
        run_subs_job(
            str(tmp_path / "movie.avi"),
            probe_runner=runner,
            runner=runner,
            should_cancel=lambda: True,
            **_job(),
        )
    assert scratch2.is_dir()


def test_22_replace_on_scratch_named_off_pre_rename_output(
    tmp_path, engine_on
):
    """D10 by construction: the replace renamed the OUTPUT
    ({stem}.subs.mp4 → {stem}.mp4) but the scratch was captured
    PRE-rename — it is the {output}.subs-work dir (named off the
    OUTPUT), removed after the replace lands."""
    runner = FakeRunner(_probe_payload())
    result = run_subs_job(
        _media(tmp_path),
        probe_runner=runner,
        runner=runner,
        replace_source=True,
        **_job(),
    )
    assert result.output_path == str(tmp_path / "movie.mp4")
    assert not (tmp_path / "movie.subs.mp4").exists()
    assert not (tmp_path / "movie.subs.mp4.subs-work").exists()
    # the note names the PRE-rename scratch path (D10 by construction):
    assert any(
        note == f"cleanup subs scratch: {tmp_path / 'movie.subs.mp4.subs-work'}"
        for note in result.notes
    )


def test_23_cleanup_rmtree_failure_keeps_success(tmp_path, engine_on,
                                                 monkeypatch):
    """The best-effort guardrail: an rmtree OSError logs the S-16
    warning note and the success stands (the directory leaks — the
    strictly-non-worse pre-0.4.18 state)."""
    scratch = tmp_path / "movie.subs.mp4.subs-work"
    import shutil as shutil_mod

    real_rmtree = shutil_mod.rmtree

    def _boom(target, *a, **_k):
        if os.path.normcase(os.path.abspath(str(target))) == (
            os.path.normcase(os.path.abspath(str(scratch)))
        ):
            raise OSError("a handle inside the dir is locked")
        return real_rmtree(target, *a, **_k)

    runner = FakeRunner(_probe_payload())
    monkeypatch.setattr(shutil_mod, "rmtree", _boom)
    result = run_subs_job(
        _media(tmp_path),
        probe_runner=runner,
        runner=runner,
        **_job(),
    )
    assert result.output_path == str(tmp_path / "movie.subs.mp4")
    assert scratch.is_dir()  # the leak is visible and named
    assert any(
        note.startswith(
            "WARNING subs scratch cleanup could not remove "
        )
        and "movie.subs.mp4.subs-work" in note
        for note in result.notes
    )

# ---------------------------------------------------------------- group 6
# (M3 plumbing: schemas/tools/executor/client_local/argparse forward the
# flag; every read is absent-defaults.)
def test_24_schema_accepts_and_rejects_suppress_srt():
    from vidlift.mcp_bridge.schemas import InvalidInput, validate_tool_args

    # transcribe: boolean accepted; absent validates (absent-defaults)
    assert validate_tool_args(
        "transcribe", {"paths": ["x"], "suppress_srt": True}
    ) == {"paths": ["x"], "suppress_srt": True}
    assert validate_tool_args("transcribe", {"paths": ["x"]}) == {
        "paths": ["x"]
    }
    with pytest.raises(InvalidInput) as excinfo:
        validate_tool_args("transcribe", {"paths": ["x"], "suppress_srt": "yes"})
    assert excinfo.value.field == "suppress_srt"
    # run options: accept/reject identically
    assert validate_tool_args(
        "run", {"paths": ["x"], "options": {"suppress_srt": True}}
    )
    with pytest.raises(InvalidInput) as excinfo2:
        validate_tool_args("run", {"paths": ["x"], "options": {"suppress_srt": "yes"}})
    assert excinfo2.value.field == "suppress_srt"


def test_25_tool_transcribe_and_executors_forward_the_flag(
    tmp_path, monkeypatch
):
    """tool_transcribe carries the flag both ways into the registry
    record; _execute_subs_job forwards bool(options.get(...)) to
    run_subs_job (the spy patch, incl. the legacy no-key record →
    False); _execute_run_job's source carries the forward (the house
    source-fence pattern)."""
    from vidlift.mcp_bridge.registry import Registry
    import shutil as shutil_mod

    root = tmp_path / "state"
    shutil_mod.rmtree(root, ignore_errors=True)
    registry = Registry(state_dir=str(root))
    src = tmp_path / "movie.avi"
    src.write_bytes(b"x")

    class _Services:
        def __init__(self, reg):
            self.registry = reg
            self.engine = None
            self.executor = self

        def notify_new_work(self):
            pass

    from vidlift.mcp_bridge.tools import tool_transcribe

    services = _Services(registry)
    results_on = tool_transcribe(
        services, {"paths": [str(src)], "suppress_srt": True}
    )
    record_on = registry.require(results_on["jobs"][0]["job_id"])
    assert record_on.options["suppress_srt"] is True
    results_off = tool_transcribe(services, {"paths": [str(src)]})
    record_off = registry.require(results_off["jobs"][0]["job_id"])
    assert record_off.options["suppress_srt"] is False

    # _execute_subs_job forwards through the spy (legacy no-key → False)
    observed = {}

    def _spy_run_subs_job(path, **kw):
        observed["suppress_srt"] = kw.get("suppress_srt")
        return type(
            "R", (),
            {
                "source_path": path,
                "output_path": str(src) + ".subs.mp4",
                "srt_path": "",
                "cue_count": 0,
                "language": "en",
                "language_confidence": 1.0,
                "model": "m",
                "device": "",
                "notes": [],
                "started_at": 0.0,
                "finished_at": 0.0,
            },
        )()

    monkeypatch.setattr("vidlift.subs_job.run_subs_job", _spy_run_subs_job)

    def _fake_find_tools(ffmpeg=None, ffprobe=None):
        return type("T", (), {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"})()

    monkeypatch.setattr("vidlift.run.find_tools", _fake_find_tools)

    class _ExecServices(_Services):
        def __init__(self, reg):
            super().__init__(reg)
            self.state_dir = tmp_path / "exec-state"
            self.probe_runner = None
            self.command_runner = None
            self.transcribe_engine_factory = None

    services_exec = _ExecServices(registry)
    # a legacy record (pre-0.4.18 shape: no suppress_srt key at all,
    # enqueued directly — tool_transcribe always writes the key now)
    from vidlift.mcp_bridge.jobs import CancelFlag

    legacy_record = registry.enqueue(
        kind="subs",
        paths=[str(src)],
        options={"language": "", "model": None, "device": "auto",
                 "force": False, "replace_source": False},
    )
    assert "suppress_srt" not in legacy_record.options
    _execute = __import__(
        "vidlift.mcp_bridge.jobs", fromlist=["_execute_subs_job"]
    )._execute_subs_job
    outcome = _execute(legacy_record, services_exec, CancelFlag())
    assert outcome.ok
    assert observed["suppress_srt"] is False
    # an explicit True record → True
    observed.clear()
    _execute(record_on, services_exec, CancelFlag())
    assert observed["suppress_srt"] is True

    # _execute_run_job carries the forward into run_file (source fence,
    # the test_v0414_subs_job house pattern)
    import inspect
    import vidlift.mcp_bridge.jobs as jobs_mod

    run_src = inspect.getsource(jobs_mod._execute_run_job)
    assert "suppress_srt=bool(options.get(\"suppress_srt\", False))," in run_src


def test_26_client_local_enqueue_subs_writes_the_option(tmp_path):
    from vidlift.mcp_bridge.client_local import LocalBridgeClient

    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    try:
        first = tmp_path / "movie.avi"
        first.write_bytes(b"x")
        ids = client.enqueue_subs([str(first)], suppress_srt=True)
        record = client.registry.require(ids[0])
        assert record.options["suppress_srt"] is True
        second = tmp_path / "movie2.avi"
        second.write_bytes(b"x")
        ids2 = client.enqueue_subs([str(second)])
        record2 = client.registry.require(ids2[0])
        assert record2.options["suppress_srt"] is False  # no-kwarg default
    finally:
        client.stop()


def test_27_argparse_defaults_flip_and_both_cmds_thread(tmp_path,
                                                        monkeypatch,
                                                        capsys):
    from vidlift.cli import build_parser
    import vidlift.cli as cli_mod

    parser = build_parser()
    args_off = parser.parse_args(["run", "x.avi"])
    assert args_off.suppress_srt is False
    args_on = parser.parse_args(["run", "x.avi", "--suppress-srt"])
    assert args_on.suppress_srt is True
    subs_off = parser.parse_args(["subs", "x.avi"])
    assert subs_off.suppress_srt is False
    subs_on = parser.parse_args(["subs", "x.avi", "--suppress-srt"])
    assert subs_on.suppress_srt is True

    # _cmd_subs threads the flag into run_subs_job (the lazy import seam)
    observed = {}

    def _spy_subs(path, **kw):
        observed["subs_suppress_srt"] = kw.get("suppress_srt")
        return type(
            "R", (),
            {
                "source_path": path,
                "output_path": path + ".subs.mp4",
                "srt_path": "",
                "cue_count": 0,
                "language": "en",
                "language_confidence": 1.0,
                "model": "m",
                "device": "",
                "notes": [],
                "started_at": 0.0,
                "finished_at": 0.0,
            },
        )()

    monkeypatch.setattr("vidlift.subs_job.run_subs_job", _spy_subs)
    rc = cli_mod.main(["subs", "x.avi", "--suppress-srt"])
    assert rc == 0
    assert observed["subs_suppress_srt"] is True

    # _cmd_run identical (vidlift.cli.run_file's module attr)
    observed.clear()

    def _spy_run(path, **kw):
        observed["run_suppress_srt"] = kw.get("suppress_srt")
        import contextlib as _cl

        class _V:
            ok = True

        class _Plan:
            output_path = path + ".out.mp4"
            expectation = ""

        class _Log:
            path = None

            def tail(self, n):
                return []

        class _State:
            file_path = path
            output_path = path + ".out.mp4"
            log = _Log()
            fail_open = False
            engine_simulated = False
            engine_note = ""
            subtitles = None
            phases = []
            upscale_profile = None

        class _Res:
            ok = True
            state = _State()
            plan = _Plan()
            verification = _V()
            manifest_path = "m.json"

        return _Res()

    monkeypatch.setattr(cli_mod, "run_file", _spy_run)
    rc = cli_mod.main(["run", "x.avi", "--suppress-srt"])
    assert rc == 0
    assert observed["run_suppress_srt"] is True
