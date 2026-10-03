# VidLift Development

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e .
```

For a throwaway verification environment:

```bash
python -m venv /tmp/vidlift-venv
/tmp/vidlift-venv/bin/pip install -e .
```

## Test

```bash
python -m pytest -q
git diff --check
```

The tests use canned `ffprobe` JSON and do not need sample video files. GUI helper tests import PyQt but do not open a visible window.

## Real-File Smoke Test

Probe/plan path:

```bash
ffmpeg -f lavfi -i testsrc2=size=640x360:rate=24 -f lavfi -i sine=frequency=1000 -t 2 -c:v libx264 -c:a aac /tmp/vidlift-smoke.mp4
python -m vidlift plan --target 720p --gbph 1.5 /tmp/vidlift-smoke.mp4
```

Runner path (real ffmpeg/ffprobe, stub engine):

```bash
ffmpeg -f lavfi -i testsrc2=size=640x272:rate=25:duration=2 \
  -f lavfi -i sine=frequency=440:duration=2 -c:v mpeg4 -c:a mp3 \
  -shortest /tmp/vidlift-smoke.avi
python -m vidlift run --log-dir /tmp/vidlift-runs /tmp/vidlift-smoke.avi
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height \
  -of csv /tmp/vidlift-smoke.vidlift.720p.hevc.mp4   # hevc,1280,720
```

The run prints its log/manifest paths and phase timings; the manifest
records `engine_simulated: true` for the stub engine. Clean up
`/tmp/vidlift-smoke*` and the run-log dir afterward.

## UI Smoke Test

```bash
QT_QPA_PLATFORM=offscreen python - <<'PY'
from PyQt6.QtWidgets import QApplication
from vidlift.gui import MainWindow
app = QApplication([])
window = MainWindow()
window.resize(1180, 760)
image = window.grab()
assert not image.isNull()
image.save('/tmp/vidlift-ui-smoke.png')
PY
```

## Local Artifacts

Do not commit caches, generated videos, or screenshots from smoke tests.
