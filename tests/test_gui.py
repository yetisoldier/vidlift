from __future__ import annotations

import pytest

from vidlift.plan import build_file_plan
from vidlift.probe import classify, parse_probe


pytest.importorskip("PyQt6")

from vidlift.gui import detail_text, format_bytes, format_duration, warnings_for


def _report():
    payload = """
    {
      "streams": [
        {
          "index": 0,
          "codec_type": "video",
          "codec_name": "mpeg4",
          "width": 640,
          "height": 360,
          "sample_aspect_ratio": "1:1",
          "field_order": "progressive",
          "r_frame_rate": "25/1",
          "avg_frame_rate": "25/1"
        },
        {"index": 1, "codec_type": "audio", "codec_name": "mp3", "channels": 2}
      ],
      "format": {"format_name": "avi", "duration": "3600.0"}
    }
    """
    return classify(parse_probe("/media/movie.avi", payload))


def test_format_helpers_are_human_readable():
    assert format_bytes(1_500_000_000) == "1.50 GB"
    assert format_bytes(None) == "unknown"
    assert format_duration(3661) == "1:01:01"
    assert format_duration(None) == "unknown"


def test_detail_text_contains_core_plan_fields():
    plan = build_file_plan(_report(), gb_per_hour=1.5)
    text = detail_text(plan)
    assert "Verdict: ready" in text
    assert "Target geometry: 1280x720" in text
    assert "Estimated size: 1.50 GB" in text
    assert "PAL speedup suspected" in text


def test_warnings_include_skip_verdicts():
    report = _report()
    report.upscale_verdict = "already-hd"
    plan = build_file_plan(report)
    assert any("Skipped" in warning for warning in warnings_for(plan))
