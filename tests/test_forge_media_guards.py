"""Unsupported destructive choices must not alter user files."""
import pytest

from vidlift.run import RunError, replace_source_with_output, run_file
from vidlift.run import ToolBox
from vidlift.subtitles import remove_srt_sidecar
from test_run import FakeRunner, probe_payload


@pytest.mark.parametrize("options", [{"replace_source": True}, {"suppress_srt": True},
                                     {"replace_source": True, "suppress_srt": True}])
def test_explicit_flags_have_safe_supported_paths(tmp_path, options):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original")
    runner = FakeRunner(probe_payload())
    result = run_file(str(source), tools=ToolBox("ffmpeg", "ffprobe"), probe_runner=runner,
                      command_runner=runner, engine_request="stub", transcription_enabled=False,
                      log_dir=str(tmp_path / "logs"), **options)
    assert result.ok
    if options.get("replace_source"):
        assert source.read_bytes() == b"output-stub"
        journal, = tmp_path.glob(".vidlift-media-*/journal.json")
        assert (journal.parent / "original.backup").read_bytes() == b"original"
        assert result.state.output_path == str(source)
    else:
        assert source.read_bytes() == b"original"


def test_direct_destructive_helpers_retain_all_files(tmp_path):
    source = tmp_path / "source.mkv"
    output = tmp_path / "output.mp4"
    sidecar = tmp_path / "output.srt"
    temp = tmp_path / "source.mkv.replace-tmp"
    for path, data in ((source, b"source"), (output, b"output"),
                       (sidecar, b"sidecar"), (temp, b"foreign")):
        path.write_bytes(data)
    with pytest.raises(RunError, match="retained"):
        replace_source_with_output(str(source), str(output))
    assert not remove_srt_sidecar(str(sidecar))
    assert [p.read_bytes() for p in (source, output, sidecar, temp)] == [
        b"source", b"output", b"sidecar", b"foreign"]
