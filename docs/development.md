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

```bash
ffmpeg -f lavfi -i testsrc2=size=640x360:rate=24 -f lavfi -i sine=frequency=1000 -t 2 -c:v libx264 -c:a aac /tmp/vidlift-smoke.mp4
python -m vidlift plan --target 720p --gbph 1.5 /tmp/vidlift-smoke.mp4
```

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
