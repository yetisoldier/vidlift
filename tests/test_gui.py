from __future__ import annotations

from types import SimpleNamespace

import pytest

from vidlift.plan import build_file_plan
from vidlift.probe import SubtitleInfo, classify, parse_probe


pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QLabel

from vidlift.gui import MainWindow, UpscaleWorker, detail_text, format_bytes, format_duration, warnings_for
from vidlift.run import _apply_subtitle_ocr_preference


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


def test_detail_text_previews_output_contents_and_ocr_choice():
    plan = build_file_plan(_report(), gb_per_hour=1.5)
    text = detail_text(plan, subtitle_ocr_enabled=False)
    assert "OUTPUT PREVIEW" in text
    assert "Destination: /media/movie.vidlift.720p.hevc.mp4" in text
    assert "Video: HEVC MP4" in text
    assert "Audio: AAC" in text


def test_disabling_ocr_removes_only_bitmap_subtitle_plan():
    report = _report()
    report.subtitles = [
        SubtitleInfo(codec_name="dvd_subtitle", kind="image", lang="eng", index=2)
    ]
    plan = build_file_plan(report)
    assert plan.subtitle_plan is not None
    assert plan.subtitle_plan.ocr_required

    adjusted = _apply_subtitle_ocr_preference(plan, enabled=False)

    assert adjusted.subtitle_plan is None
    assert "OCR disabled" in adjusted.subtitle_note


def test_main_window_shows_version_output_column_and_start_action():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    labels = [label.text() for label in window.findChildren(QLabel)]
    assert any(text.startswith("Version ") for text in labels)
    assert window.file_table.horizontalHeaderItem(4).text() == "Output"
    assert window.start_button.text() == "Start Upscale"
    assert not window.start_button.isEnabled()
    assert window.subtitle_ocr.text() == "Enable subtitle OCR"
    window.close()
    app.processEvents()


def test_worker_passes_subtitle_ocr_preference_to_runner(monkeypatch):
    plan = build_file_plan(_report(), gb_per_hour=1.5)
    observed = {}

    monkeypatch.setattr("vidlift.gui.find_tools", lambda: object())

    def fake_run_file(path, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(
            plan=plan,
            state=SimpleNamespace(fail_open=False, engine_simulated=False),
        )

    monkeypatch.setattr("vidlift.gui.run_file", fake_run_file)
    worker = UpscaleWorker(
        [(0, plan)],
        gb_per_hour=1.5,
        output_dir=None,
        subtitle_ocr_enabled=False,
    )
    worker.run()

    assert observed["subtitle_ocr"] is False
