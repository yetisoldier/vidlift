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