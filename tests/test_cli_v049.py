"""v0.4.9 CLI parsing tests (design §12 N1-N4): `run --resume` and
`runs prune [--dry-run]`. The design's UX surface: resume is opt-in
everywhere (GUI checkbox, CLI flag, MCP option) — absent means the
v0.4.8 behavior, byte-identical."""

from __future__ import annotations

import json

import pytest

from vidlift import cli


class _StubState:
    file_path = "movie.avi"
    manifest_path = None
    output_path = None
    fail_open = False
    fail_open_reason = None
    engine_simulated = False
    engine_note = None
    upscale_profile = "live-action"
    subtitles = {"status": "none"}
    phases = []
    log = type("L", (), {"path": "x.log", "tail": lambda self, n: []})()


class _StubRunResult:
    ok = True
    manifest_path = None

    def __init__(self) -> None:
        self.state = _StubState()
        self.plan = type(
            "P", (), {"output_path": "movie.720p.hevc.mp4",
                      "expectation": "bigger"}
        )()
        self.verification = type("V", (), {"ok": True})()


class TestResumeFlagParsing:
    def test_n1_run_resume_defaults_off(self):
        """N1: `vidlift run FILE` has resume=False (v0.4.8 behavior)."""
        args = cli.build_parser().parse_args(["run", "movie.avi"])
        assert args.resume is False

    def test_n2_run_resume_flag_true(self):
        """N2: `vidlift run FILE --resume` parses to True."""
        args = cli.build_parser().parse_args(
            ["run", "movie.avi", "--resume"]
        )
        assert args.resume is True

    def test_n3_resume_flag_threads_to_run_file(self, monkeypatch, tmp_path):
        """N3: _cmd_run passes resume=True into run_file's options (the
        claim layer only runs when the user asked for it)."""
        captured: dict = {}

        def fake_run_file(*args, **kwargs):
            captured.update(kwargs)
            return _StubRunResult()

        monkeypatch.setattr(cli, "run_file", fake_run_file)
        monkeypatch.setattr(
            cli, "find_tools", lambda **k: object()
        )
        rc = cli.main(["run", "--resume", "movie.avi"])
        assert rc == 0
        assert captured["resume"] is True

    def test_n4_without_flag_run_file_gets_resume_false(self, monkeypatch, tmp_path):
        """N4: the default invocation threads resume=False — the
        claim layer is dormant, byte-identical to v0.4.8."""
        captured: dict = {}

        def fake_run_file(*args, **kwargs):
            captured["positional"] = args
            captured.update(kwargs)

            return _StubRunResult()

        monkeypatch.setattr(cli, "run_file", fake_run_file)
        monkeypatch.setattr(cli, "find_tools", lambda **k: object())
        rc = cli.main(["run", "movie.avi"])
        assert rc == 0
        assert captured["resume"] is False


class TestRunsPrune:
    def test_n5_runs_prune_dry_run_flag_parses(self):
        args = cli.build_parser().parse_args(
            ["runs", "prune", "--dry-run"]
        )
        assert args.command == "runs"
        assert args.runs_command == "prune"
        assert args.dry_run is True

    def test_n6_runs_prune_default_is_a_real_prune(self):
        args = cli.build_parser().parse_args(["runs", "prune"])
        assert args.dry_run is False

    def test_n7_runs_prune_reports_decisions(self, tmp_path, capsys):
        """N7: `vidlift runs prune --log-dir DIR --dry-run` prints one
        decision line per stale set and deletes nothing."""
        from vidlift.run import prune_plan

        # build two STALE sets for one source (keep-2 protects none:
        # both are old and the newest is... also old, so the older
        # extra is beyond keep-2)
        import os
        import time

        from vidlift.engine_ncnn import _workdir_for

        log_root = tmp_path / ".vidlift-runs"
        log_root.mkdir()
        old = time.time() - 30 * 86400
        for run_id in (
            "Show.20261020-080000",
            "Show.20261024-090000",
            "Show.20261027-201001",
        ):
            prepared = log_root / f"{run_id}.prepared.mkv"
            prepared.write_bytes(b"p")
            workdir = log_root / f"{run_id}.engine-work"
            workdir.mkdir()
            (workdir / "chunk_state.json").write_text(
                json.dumps(
                    {
                        "completed": [{"idx": 0, "count": 300}],
                        "prepared_path": str(prepared),
                        "prepared_total_frames": 900,
                        "chunk_frames": 300,
                        "source": {
                            "path": "/nowhere/Show.avi",
                            "size": 1,
                            "mtime_ns": 1,
                            "duration_s": 900.0,
                        },
                        "plan": {
                            "target": "720p",
                            "gb_per_hour": 1.5,
                            "geometry": [1280, 720],
                            "profile": "live-action",
                            "subtitle_bitmap_ocr": None,
                            "source_flagged_interlaced": False,
                            "prepare_filters": {},
                            "flag_fallback": False,
                        },
                    }
                )
            )
            os.utime(prepared, (old, old))
        # NO .vidlift-runs FILE argument form: use --log-dir
        rc = cli.main(
            [
                "runs", "prune", "--log-dir", str(log_root), "--dry-run",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "would remove" in out.lower()
        assert "Show.20261020-080000" in out
        # nothing deleted
        assert (log_root / "Show.20261020-080000.prepared.mkv").exists()

    def test_n8_runs_prune_executes_and_reports(self, tmp_path, capsys):
        """N8: without --dry-run the stale set is gone; the report
        names each removed artifact."""
        import os
        import time

        log_root = tmp_path / ".vidlift-runs"
        log_root.mkdir()
        old = time.time() - 30 * 86400
        for run_id in (
            "Show.20261020-080000",
            "Show.20261024-090000",
            "Show.20261027-201001",
        ):
            prepared = log_root / f"{run_id}.prepared.mkv"
            prepared.write_bytes(b"p")
            workdir = log_root / f"{run_id}.engine-work"
            workdir.mkdir()
            (workdir / "chunk_state.json").write_text(
                json.dumps({"completed": [{"idx": 0, "count": 300}]})
            )
            os.utime(prepared, (old, old))
        rc = cli.main(["runs", "prune", "--log-dir", str(log_root)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Show.20261020-080000" in out
        assert not (
            log_root / "Show.20261020-080000.prepared.mkv"
        ).exists()
        assert not (
            log_root / "Show.20261020-080000.engine-work"
        ).exists()
        # keep-2 survived
        assert (log_root / "Show.20261024-090000.prepared.mkv").exists()
        assert (log_root / "Show.20261027-201001.prepared.mkv").exists()

    def test_n9_runs_prune_missing_dir_is_clean_error(self, tmp_path, capsys):
        """N9: a nonexistent --log-dir exits 2 with an error line,
        never a traceback."""
        rc = cli.main(
            ["runs", "prune", "--log-dir", str(tmp_path / "nope")]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "error" in err.lower() or "not found" in err.lower()