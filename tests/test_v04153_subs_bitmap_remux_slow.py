"""F-1 integration gate (v0.4.15.3) — real ffmpeg through ``mplex`` on
the designed Mode B source class: a bitmap-subs (PGS) mkv.

Bob's v0.4.15.3 F-1 spec asked for a real-ffmpeg pin that the guard-
admitted source completes the remux and the output's only subtitle
stream is mov_text. Fixture note (the spec's documented "iterate the
fixture command" clause): the sketched fixture (`-c:s dvdsub` from an
SRT) fails ffmpeg's categorical text→bitmap guard ("Subtitle encoding
currently only possible from text to text or bitmap to bitmap") —
ffmpeg cannot SYNTHESIZE a bitmap subtitle track from text by design.
The fixture that works: a hand-crafted MINIMAL valid PGS display set
(raw HDMV ``.sup`` segments: PDS/ODS/PCS/WDS/END) muxed with
``-c:s copy`` — producing the exact designed source class
(``hdmv_pgs_subtitle``) with zero network, zero real model, and zero
decoding of the bitmap track (the allowlist never maps it).

Skips when the host has no ffmpeg/ffprobe; the argv pins in
tests/test_v0414_subs_job.py are the always-on gate.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from vidlift.subs_job import mplex

_FFMPEG = shutil.which("ffmpeg")
_FPROBE = shutil.which("ffprobe")

pytestmark = [
    pytest.mark.skipif(_FFMPEG is None, reason="host ffmpeg required"),
    pytest.mark.skipif(_FPROBE is None, reason="host ffprobe required"),
]


def _run(cmd: list[str]) -> None:
    subprocess.run(
        [str(part) for part in cmd],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _probe_streams(path: Path) -> list[str]:
    proc = subprocess.run(
        [
            _FPROBE, "-v", "error", "-show_entries",
            "stream=index,codec_type,codec_name", "-of", "csv", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _write_srt(path: Path) -> None:
    path.write_text(
        "1\n00:00:00,000 --> 00:00:01,500\nHello there.\n",
        encoding="utf-8",
    )


def _craft_minimal_pgs_sup(path: Path) -> None:
    """A structurally valid one-display-set PGS stream: a 2-entry PDS,
    a 4-byte RLE ODS (two 4-pixel runs of palette index 1), an
    epoch-start PCS (128x96, zero composition objects), one full-frame
    WDS window, and END. Segment frame: 'PG' + u16 size + payload
    (type + u16 size + data). The content only needs to mux and probe;
    the bitmap track is never decoded downstream (the allowlist map
    skips it entirely)."""
    def seg(ctype: int, data: bytes) -> bytes:
        payload = bytes([ctype]) + struct.pack(">H", len(data)) + data
        return b"PG" + struct.pack(">H", len(payload)) + payload

    pds = bytes([0x01, 0x00])  # palette id, version
    for pid, (y, cr, cb, a) in (
        (0, (0x00, 0x80, 0x80, 0xFF)),
        (1, (0xFF, 0x80, 0x80, 0xFF)),
    ):
        pds += bytes([pid, y, cr, cb, a])
    # ODS: object 1, first-in-epoch flag, 4 bytes of RLE data = two
    # 4-pixel runs of palette index 1 (count byte 0x44 -> 0x44 & 0x3F).
    ods = (
        bytes([0x01, 0x01, 0x80])
        + bytes([0, 0, 4])
        + bytes([0x44, 0x01, 0x44, 0x01])
    )
    pcs = (
        struct.pack(">HHB", 128, 96, 0x21)  # width, height, frame rate
        + struct.pack(">H", 1)              # composition number
        + bytes([0x80])                     # composition state: epoch start
        + bytes([0x00])                     # palette update flag
        + bytes([0x00])                     # number of composition objects
        + b"\x00\x00\x00\x80"               # end-of-PCS marker
    )
    wds = bytes([1]) + bytes([0]) + struct.pack(">HHHH", 0, 0, 128, 96)
    path.write_bytes(
        b"".join(
            [
                seg(0x14, pds),
                seg(0x15, ods),
                seg(0x16, pcs),
                seg(0x17, wds),
                seg(0x80, b""),  # END
            ]
        )
    )


def _real_runner(binary: str, args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run([binary, *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def test_real_bitmap_subs_mkv_remuxes_with_only_mov_text(tmp_path):
    """The designed F-1 evidence class: the probe admits the file (no
    usable text subs), the real ffmpeg remux COMPLETES, and the output
    carries exactly one subtitle stream — mov_text (the transcription).
    The pre-fix blanket `-map 0` failed every such source at the mux."""
    # -- fixture: v + a + bitmap-subs mkv (PGS via hand-crafted .sup) --
    src = tmp_path / "pgs-source.mkv"
    sup = tmp_path / "sub.sup"
    srt = tmp_path / "out.srt"
    _craft_minimal_pgs_sup(sup)
    _write_srt(srt)
    _run(
        [
            _FFMPEG, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=128x96:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:d=2",
            "-i", str(sup),
            "-map", "0", "-map", "1", "-map", "2",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac",
            "-c:s", "copy",
            str(src),
        ]
    )
    src_streams = "\n".join(_probe_streams(src))
    assert "hdmv_pgs_subtitle" in src_streams, (
        "fixture must carry the bitmap (PGS) track the guard admits:\n"
        + src_streams
    )
    assert "h264" in src_streams and "aac" in src_streams

    # -- the real mplex (allowlist map; temp-sibling + os.replace) --
    output = tmp_path / "pgs-source.subs.mp4"
    out = mplex(
        str(src),
        str(srt),
        output_path=str(output),
        language="en",
        ffmpeg=_FFMPEG,
        runner=_real_runner,
    )
    assert out == str(output)
    assert output.is_file() and output.stat().st_size > 0
    assert src.is_file(), "source-blind: the bitmap track stays in the source"

    # exactly ONE subtitle stream in the output, and it is mov_text.
    out_streams = _probe_streams(output)
    sub_lines = [line for line in out_streams if line.endswith(",subtitle")]
    assert len(sub_lines) == 1, "\n".join(out_streams)
    assert sub_lines[0].endswith(",mov_text,subtitle"), "\n".join(out_streams)


def test_real_pgs_source_classifies_as_bitmap_not_usable(tmp_path):
    """Guard sanity on the REAL fixture path: the PGS mkv classifies
    kind="image" (not usable text), which is exactly why the remux may
    run on it — and the allowlist leaves the bitmap in the source."""
    from vidlift.probe import classify, parse_probe
    from vidlift.subs_job import source_has_usable_subtitles

    src = tmp_path / "pgs-guardcheck.mkv"
    sup = tmp_path / "sub.sup"
    _craft_minimal_pgs_sup(sup)
    _run(
        [
            _FFMPEG, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=128x96:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:d=2",
            "-i", str(sup),
            "-map", "0", "-map", "1", "-map", "2",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac",
            "-c:s", "copy",
            str(src),
        ]
    )
    proc = subprocess.run(
        [
            _FPROBE, "-v", "error", "-show_streams", "-of", "json", str(src),
        ],
        check=True, capture_output=True, text=True,
    )
    report = classify(parse_probe(str(src), proc.stdout))
    assert [s.kind for s in report.subtitles] == ["image"]
    assert not source_has_usable_subtitles(report)
    assert report.audio, "the fixture keeps its audio (0:a mapping anchor)"