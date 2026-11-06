"""Path-aware remux fixtures must not bypass independent output verification."""
import json
from pathlib import Path

import pytest

from test_v0414_subs_job import FakeRunner, _probe_payload
from vidlift.probe import probe_file
from vidlift.subs_job import SubsJobError, mplex


@pytest.mark.parametrize("fault", [None, "video", "audio", "subtitle", "container",
                                   "duration-zero", "duration-change", "video-change",
                                   "audio-change", "probe-failed"])
def test_real_mux_verification_before_publication(tmp_path, fault):
    source = tmp_path / "synthetic.avi"
    sidecar = tmp_path / "synthetic.subs.srt"
    target = tmp_path / "synthetic.subs.mp4"
    source.write_bytes(b"synthetic source sentinel, not playable media")
    sidecar.write_bytes(b"synthetic subtitle sentinel")
    fake = FakeRunner(_probe_payload())
    expected = probe_file(str(source), runner=fake)
    probes = []

    def runner(binary, args):
        code, out, err = fake(binary, args)
        if "-show_format" in args and args[-1].endswith(".muxing.mp4"):
            probes.append(args[-1])
            data = json.loads(out)
            if fault in {"video", "audio", "subtitle"}:
                data["streams"] = [s for s in data["streams"] if s["codec_type"] != fault]
            elif fault == "container":
                data["format"]["format_name"] = "avi"
            elif fault == "duration-zero":
                data["format"]["duration"] = "0"
            elif fault == "duration-change":
                data["format"]["duration"] = "90"
            elif fault == "video-change":
                data["streams"][0]["width"] = 1280
            elif fault == "audio-change":
                data["streams"][1]["channels"] = 1
            elif fault == "probe-failed":
                return 1, "", "injected probe failure"
            return code, json.dumps(data), err
        return code, out, err

    def mux():
        return mplex(str(source), str(sidecar), output_path=str(target), language="eng",
                     ffmpeg="fake", runner=runner, probe_runner=runner, expected_media=expected)

    if fault is None:
        assert mux() == str(target)
        assert target.read_bytes() == b"muxed-out"
    else:
        with pytest.raises(SubsJobError, match="remux verification failed"):
            mux()
        assert not target.exists()
    assert len(probes) == 1
    assert not Path(probes[0]).exists()
    assert source.read_bytes() == b"synthetic source sentinel, not playable media"
    assert sidecar.read_bytes() == b"synthetic subtitle sentinel"
    assert not list(tmp_path.glob(".vidlift-mux-*.muxing.mp4"))
