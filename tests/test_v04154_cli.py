"""v0.4.15.4 CLI-surface gates.

Shared CLI home for the v0.4.15.4 spec's CLI pins:
- F-8 (commit 2): a missing ffprobe binary surfaces as the documented
  named ProbeError through the probe/plan CLI — per-file error + exit 1,
  NEVER a traceback.
- F-14a (commit 4): the ``--no-transcribe`` headless off switch parses
  and threads into run_file/build_batch_plan through the absent-defaults
  rule.
The runner here replays the REAL failure mode (subprocess-style
FileNotFoundError on a nonexistent binary) so the production wrapping in
probe_file is what gets exercised.
"""

from __future__ import annotations

import json

import pytest

from vidlift import cli


@pytest.fixture
def media_file(tmp_path):
    """An empty placeholder: probe_file's existence gate passes; the
    runner seam decides what ffprobe 'reports'."""
    path = tmp_path / "movie.avi"
    path.write_text("")
    return str(path)


@pytest.fixture
def missing_binary_probe(monkeypatch, tmp_path):
    """Route the CLI's probe_and_classify through the REAL probe_file
    with a nonexistent ffprobe binary — the F-8 failure, exercised end
    to end through _cmd_probe/_cmd_plan's own error boundary."""

    def failing(path, **kwargs):
        from vidlift.probe import probe_file

        return probe_file(path, binary=str(tmp_path / "no-such-ffprobe"))

    monkeypatch.setattr(cli, "probe_and_classify", failing)


# ---------------------------------------------------------------------------
# F-8 (v0.4.15.4): the probe/plan CLI never tracebacks on a missing binary
# ---------------------------------------------------------------------------


def test_probe_cli_missing_binary_exits_named_error(
    media_file, missing_binary_probe, capsys
):
    rc = cli.main(["probe", media_file])
    captured = capsys.readouterr()
    assert rc == 1
    assert "ffprobe binary not found" in captured.err
    assert "no-such-ffprobe" in captured.err
    assert "Traceback" not in captured.err


def test_plan_cli_missing_binary_exits_named_error(
    media_file, missing_binary_probe, capsys
):
    rc = cli.main(["plan", media_file])
    captured = capsys.readouterr()
    assert rc == 1
    assert "ffprobe binary not found" in captured.err
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# F-14a (v0.4.15.4): the headless --no-transcribe off switch
# ---------------------------------------------------------------------------


class TestNoTranscribeFlagParsing:
    def test_run_parser_defaults_false(self):
        args = cli.build_parser().parse_args(["run", "movie.avi"])
        assert args.no_transcribe is False

    def test_run_parser_flag_true(self):
        args = cli.build_parser().parse_args(
            ["run", "movie.avi", "--no-transcribe"]
        )
        assert args.no_transcribe is True

    def test_plan_parser_defaults_false(self):
        args = cli.build_parser().parse_args(["plan", "movie.avi"])
        assert args.no_transcribe is False

    def test_plan_parser_flag_true(self):
        args = cli.build_parser().parse_args(
            ["plan", "movie.avi", "--no-transcribe"]
        )
        assert args.no_transcribe is True


class _StubState:
    file_path = "movie.avi"
    manifest_path = None
    fail_open = False
    fail_open_reason = None
    engine_simulated = False
    engine_note = None
    upscale_profile = "live-action"
    subtitles = {"status": "none"}
    phases = []
    log = type("L", (), {"path": None, "tail": lambda self, n: []})()


def _stub_run_result():
    return type(
        "R",
        (),
        {
            "ok": True,
            "manifest_path": None,
            "state": _StubState(),
            "plan": type("P", (), {"output_path": "x", "expectation": ""})(),
            "verification": type("V", (), {"ok": True})(),
        },
    )()


class TestNoTranscribeThreading:
    def test_run_flag_threads_transcription_enabled_false(
        self, monkeypatch
    ):
        """`run --no-transcribe` must thread transcription_enabled=FALSE
        explicitly (run.py's false is the load-bearing off switch) — the
        engine-off env keeps the thread cheap (no resolution)."""
        captured: dict = {}

        def fake_run_file(*args, **kwargs):
            captured.update(kwargs)
            return _stub_run_result()

        monkeypatch.setattr(cli, "run_file", fake_run_file)
        monkeypatch.setattr(cli, "find_tools", lambda **k: object())
        rc = cli.main(["run", "--no-transcribe", "movie.avi"])
        assert rc == 0
        assert captured["transcription_enabled"] is False

    def test_run_absent_flag_threads_none(self, monkeypatch):
        """The absent-defaults rule: no flag → None → run.py/config own
        the default (the CLI hardcodes no truth of its own)."""
        captured: dict = {}

        def fake_run_file(*args, **kwargs):
            captured.update(kwargs)
            return _stub_run_result()

        monkeypatch.setattr(cli, "run_file", fake_run_file)
        monkeypatch.setattr(cli, "find_tools", lambda **k: object())
        rc = cli.main(["run", "movie.avi"])
        assert rc == 0
        assert captured["transcription_enabled"] is None

    def test_plan_flag_threads_transcription_enabled_false(
        self, monkeypatch
    ):
        """Plan gets the flag too — otherwise `vidlift plan` would
        render the F-6-fixed table under the default while a sibling
        `run --no-transcribe` ships without subs."""
        from vidlift.probe import classify, parse_probe

        payload = json.dumps(
            {
                "streams": [
                    {"index": 0, "codec_type": "video",
                     "codec_name": "mpeg4", "width": 640, "height": 272,
                     "sample_aspect_ratio": "1:1",
                     "field_order": "progressive",
                     "r_frame_rate": "25/1", "avg_frame_rate": "25/1"},
                    {"index": 1, "codec_type": "audio",
                     "codec_name": "mp3", "channels": 2},
                ],
                "format": {"format_name": "avi", "duration": "3600.0"},
            }
        )

        def fake_report(path, **kwargs):
            return classify(parse_probe(path, payload))

        captured: dict = {}
        real_build_batch_plan = cli.build_batch_plan

        def spy(reports, **kwargs):
            captured.update(kwargs)
            return real_build_batch_plan(reports, **kwargs)

        monkeypatch.setattr(cli, "probe_and_classify", fake_report)
        monkeypatch.setattr(cli, "build_batch_plan", spy)
        rc = cli.main(["plan", "--no-transcribe", "movie.avi"])
        assert rc == 0
        assert captured["transcription_enabled"] is False

    def test_plan_absent_flag_threads_none(self, monkeypatch):
        from vidlift.probe import classify, parse_probe

        payload = json.dumps(
            {
                "streams": [
                    {"index": 0, "codec_type": "video",
                     "codec_name": "mpeg4", "width": 640, "height": 272,
                     "sample_aspect_ratio": "1:1",
                     "field_order": "progressive",
                     "r_frame_rate": "25/1", "avg_frame_rate": "25/1"},
                    {"index": 1, "codec_type": "audio",
                     "codec_name": "mp3", "channels": 2},
                ],
                "format": {"format_name": "avi", "duration": "3600.0"},
            }
        )

        def fake_report(path, **kwargs):
            return classify(parse_probe(path, payload))

        captured: dict = {}
        real_build_batch_plan = cli.build_batch_plan

        def spy(reports, **kwargs):
            captured.update(kwargs)
            return real_build_batch_plan(reports, **kwargs)

        monkeypatch.setattr(cli, "probe_and_classify", fake_report)
        monkeypatch.setattr(cli, "build_batch_plan", spy)
        rc = cli.main(["plan", "movie.avi"])
        assert rc == 0
        assert captured["transcription_enabled"] is None