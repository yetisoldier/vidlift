from pathlib import Path

import pytest

from vidlift.srt import Cue, write_srt_file
from vidlift.subs_job import SubsJobError, mplex, replace_subs_output, run_subs_job
from test_v0414_subs_job import FakeRunner, FakeEngine, _probe_payload, engine_on, no_lock


def test_srt_writer_preserves_existing_sidecar(tmp_path):
    target = tmp_path / "movie.subs.srt"
    target.write_bytes(b"incumbent")
    with pytest.raises(FileExistsError):
        write_srt_file(str(target), [Cue(0, 1000, "new")])
    assert target.read_bytes() == b"incumbent"


def test_mux_publication_does_not_clobber_late_target(tmp_path, monkeypatch):
    source = tmp_path / "movie.mkv"
    srt = tmp_path / "movie.srt"
    target = tmp_path / "movie.subs.mp4"
    source.write_bytes(b"source")
    srt.write_bytes(b"subtitle")

    def runner(_binary, args):
        Path(args[-1]).write_bytes(b"verified synthetic mux")
        target.write_bytes(b"late unrelated incumbent")
        return 0, "", ""

    monkeypatch.setattr("vidlift.subs_job.verify_mux", lambda *a, **k: None)
    with pytest.raises(SubsJobError):
        mplex(str(source), str(srt), output_path=str(target), language="eng",
              ffmpeg="ffmpeg", runner=runner)
    assert source.read_bytes() == b"source"
    assert target.read_bytes() == b"late unrelated incumbent"


def test_mux_verification_failure_keeps_source_and_cleans_owned_temp(tmp_path, monkeypatch):
    source = tmp_path / "movie.mkv"
    srt = tmp_path / "movie.srt"
    target = tmp_path / "movie.subs.mp4"
    source.write_bytes(b"original source")
    srt.write_bytes(b"old sidecar bytes")

    def runner(_binary, args):
        Path(args[-1]).write_bytes(b"corrupt fake mux")
        return 0, "", ""

    def reject(*_args, **_kwargs):
        raise SubsJobError("injected corrupt mux")

    monkeypatch.setattr("vidlift.subs_job.verify_mux", reject)
    with pytest.raises(SubsJobError, match="could not finalize"):
        mplex(str(source), str(srt), output_path=str(target), language="eng",
              ffmpeg="ffmpeg", runner=runner)
    assert source.read_bytes() == b"original source"
    assert srt.read_bytes() == b"old sidecar bytes"
    assert not target.exists()
    assert not list(tmp_path.glob(".vidlift-mux-*.muxing.mp4"))


@pytest.mark.usefixtures("engine_on", "no_lock")
def test_replace_mode_preserves_recoverable_original(tmp_path, monkeypatch):
    source = tmp_path / "movie.mp4"
    source.write_bytes(b"original source")
    runner = FakeRunner(_probe_payload())
    result = run_subs_job(str(source), ffmpeg="ffmpeg", replace_source=True, runner=runner,
                          probe_runner=runner, engine_factory=FakeEngine)
    assert source.read_bytes() == b"muxed-out"
    assert result.output_path == str(source)
    journal, = tmp_path.glob(".vidlift-media-*/journal.json")
    assert (journal.parent / "original.backup").read_bytes() == b"original source"


@pytest.mark.usefixtures("engine_on", "no_lock")
def test_suppress_withholds_own_sidecar_and_preserves_incumbent(tmp_path, monkeypatch):
    source = tmp_path / "movie.mp4"
    sidecar = tmp_path / "movie.subs.srt"
    source.write_bytes(b"original source")
    sidecar.write_bytes(b"user sidecar")
    runner = FakeRunner(_probe_payload())
    result = run_subs_job(str(source), ffmpeg="ffmpeg", suppress_srt=True, runner=runner,
                          probe_runner=runner, engine_factory=FakeEngine)
    assert result.srt_path == ""
    assert source.read_bytes() == b"original source"
    assert sidecar.read_bytes() == b"user sidecar"
    assert Path(result.output_path).read_bytes() == b"muxed-out"


def test_direct_replace_helper_is_disabled_and_preserves_everything(tmp_path):
    source = tmp_path / "movie.mp4"
    output = tmp_path / "movie.subs.mp4"
    unrelated = tmp_path / "unrelated.bin"
    source.write_bytes(b"original")
    output.write_bytes(b"verified output")
    unrelated.write_bytes(b"unrelated")
    with pytest.raises(SubsJobError, match="prepared verified operation"):
        replace_subs_output(str(source), str(output))
    assert source.read_bytes() == b"original"
    assert output.read_bytes() == b"verified output"
    assert unrelated.read_bytes() == b"unrelated"
