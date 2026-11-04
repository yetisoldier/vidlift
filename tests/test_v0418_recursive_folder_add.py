"""v0.4.18 WS1 — recursive folder-add (both modes + drop parity).

Bob spec §12 file A. Contracts (red-first against pre-0.4.18 gui.py):

  Group 1 — collect_video_files (pure, tmp_path):
    1 nested tree -> every video, fully sorted, full paths
    2 uppercase/mixed extensions; non-videos skipped
    3 empty dir / video-less tree -> []
    4 symlinked DIRECTORY: no loop, contents exactly once (realpath)
    5 symlinked FILES to one inode: one entry, lexicographic survivor
    6 direct-children-only == the pre-0.4.18 iterdir() result (regression
      identity)
  Group 2 — Mode A add_folder (offscreen window, dialog monkeypatched):
    7 nested real tree -> _add_paths receives the full sorted list
    8 501-name collect -> S-4 warning, nothing added; 500 passes
      (boundary)
  Group 3 — Mode B choose_subs_folder + drop parity:
    9 nested tree -> ONE enqueue_subs call, sorted, one row per file
   10 501-name guard -> warning, client never called
   11 empty folder -> silent no-op (no dialogs, no enqueue)
   12 a nested file colliding with a LIVE subs job -> v0.4.15.1 block
      dialog, nothing enqueued
   13 dropEvent directory arm over a REAL nested folder -> every nested
      video reaches _add_paths (drop parity, spec deviation 1)

Conventions: the v0.4.14 Mode B harness (offscreen, per-test isolated
QSettings ini, dialogs monkeypatched); NO real ffmpeg/whisper/network;
symlink-creating tests skip on Windows (privileges).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

os.environ.setdefault("VIDLIFT_MONITOR_AUTOSTART", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl
from PyQt6.QtGui import QDropEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

from vidlift.gui import MainWindow, collect_video_files, MAX_FILES

from test_v0414_mode_b_gui import _ScriptedClient, _window  # house harness
from test_v04151_subs_guard import _QueueViewsClient  # the conflict client


# ---------------------------------------------------------------- harness
@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _nested_tree(root: Path) -> list[str]:
    """A show tree: two seasons plus a top-level pilot. Returns the
    expected SORTED full path list (determinism asserted everywhere)."""
    (root / "show" / "Season 1").mkdir(parents=True, exist_ok=True)
    (root / "show" / "Season 2").mkdir(parents=True, exist_ok=True)
    (root / "show" / "Season 1" / "e1.avi").write_bytes(b"v")
    (root / "show" / "Season 2" / "e2.mkv").write_bytes(b"v")
    (root / "pilot.mp4").write_bytes(b"v")
    return sorted(
        str(p) for p in root.rglob("*") if p.is_file() and p.suffix.lower()
        in {".avi", ".mkv", ".mp4"}
    )


def _drop_dir_event(folder: str):
    # both the event AND its mime must survive the call (the QDropEvent
    # holds only a C++ pointer — test_gui.py's helper keeps the pair
    # alive the same way; dropping the mime segfaults the read).
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(folder)])
    event = QDropEvent(
        QPointF(1.0, 1.0),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    return event, mime


# ---------------------------------------------------------------- group 1
def test_01_nested_tree_is_fully_sorted_full_paths(tmp_path):
    expected = _nested_tree(tmp_path)
    assert len(expected) == 3
    assert collect_video_files(tmp_path) == expected


def test_02_case_insensitive_extensions_and_nonvideo_skip(tmp_path):
    (tmp_path / "movie.MP4").write_bytes(b"v")
    (tmp_path / "clip.Ts").write_bytes(b"v")  # '.Ts' -> lower '.ts'
    (tmp_path / "subtitles.srt").write_bytes(b"n")
    (tmp_path / "movie.nfo").write_bytes(b"n")
    (tmp_path / "notes.txt").write_bytes(b"n")
    got = collect_video_files(tmp_path)
    assert got == [
        str(tmp_path / "clip.Ts"),
        str(tmp_path / "movie.MP4"),
    ]


def test_03_empty_dir_and_videoless_tree_yield_empty(tmp_path):
    assert collect_video_files(tmp_path) == []
    (tmp_path / "nested-empty").mkdir()
    (tmp_path / "readme.txt").write_bytes(b"n")
    assert collect_video_files(tmp_path) == []


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation needs privileges on Windows"
)
def test_04_symlinked_dir_and_self_loop_terminate_once(tmp_path):
    """The loop-safety proof (D3): os.walk(followlinks=False) never
    descends into a symlinked directory, so `seasons -> real` shows the
    same CONTENTS as `real` itself (once only) and a literal self-loop
    (`link -> ..`) terminates instead of hanging."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "ep1.avi").write_bytes(b"v")
    (real / "ep2.mkv").write_bytes(b"v")
    (tmp_path / "seasons").symlink_to(real)
    (tmp_path / "loop").mkdir()
    link = tmp_path / "loop" / "link"
    link.symlink_to("..")  # self-loop by construction
    got = collect_video_files(tmp_path)
    assert got == [str(real / "ep1.avi"), str(real / "ep2.mkv")]


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation needs privileges on Windows"
)
def test_05_symlinked_file_duplicates_keep_lexicographic_first(tmp_path):
    target = tmp_path / "a-real.avi"
    target.write_bytes(b"v")
    (tmp_path / "b-copy.avi").symlink_to(target)
    # sorted list: ['b-copy.avi', 'a-real.avi'] -> dedupe keeps the
    # lexicographically FIRST name (sort-then-dedupe order proof).
    got = collect_video_files(tmp_path)
    first_name = sorted(["a-real.avi", "b-copy.avi"])[0]
    assert got == [str(tmp_path / first_name)]
    assert len(got) == 1


def test_06_direct_children_match_pre_0418_iterdir_result(tmp_path):
    """Regression identity: a folder with only direct-child videos
    returns EXACTLY the v0.4.17 expression's list, byte-for-byte."""
    for name in ("one.avi", "two.mkv", "skip.txt"):
        (tmp_path / name).write_bytes(b"v")
    # the pre-0.4.18 add_folder expression, reconstructed literally:
    legacy = [
        str(path)
        for path in sorted(Path(str(tmp_path)).iterdir())
        if path.is_file() and path.suffix.lower() in {
            ".avi", ".divx", ".flv", ".m4v", ".mkv", ".mov", ".mp4",
            ".mpeg", ".mpg", ".ts", ".vob", ".wmv",
        }
    ]
    assert collect_video_files(str(tmp_path)) == legacy


# ---------------------------------------------------------------- group 2
def test_07_mode_a_add_folder_walks_the_recursive_tree(
    qapp, monkeypatch, tmp_path
):
    expected = _nested_tree(tmp_path)
    tree_root = tmp_path  # contains show/ + pilot.mp4
    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog,
        "getExistingDirectory",
        staticmethod(lambda *a, **k: str(tree_root)),
    )
    added: list[list[str]] = []
    monkeypatch.setattr(
        MainWindow, "_add_paths", lambda self, paths: added.append(list(paths))
    )
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_folder()
    assert added == [expected]
    next(gen, None)


def test_08_mode_a_folder_guard_names_count_and_tree(
    qapp, monkeypatch, tmp_path
):
    import vidlift.gui as gui_mod

    fake = [str(Path("/fake") / f"tv-{i}.mp4") for i in range(501)]
    monkeypatch.setattr(
        gui_mod.QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: str(tmp_path)),
    )
    warned: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: warned.append(a[2] if len(a) > 2 else "")),
    )
    added: list[list[str]] = []
    monkeypatch.setattr(
        MainWindow, "_add_paths", lambda self, paths: added.append(list(paths))
    )
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)

    monkeypatch.setattr(gui_mod, "collect_video_files", lambda folder: list(fake))
    win.add_folder()
    assert len(warned) == 1
    assert "501" in warned[0] and "500" in warned[0]
    assert "subfolders included" in warned[0]
    assert added == [], "the 501-video tree must not be added"

    # boundary: exactly MAX_FILES passes (the cap is >500, not >=500)
    warned.clear()
    monkeypatch.setattr(gui_mod, "collect_video_files", lambda folder: list(fake[:MAX_FILES]))
    win.add_folder()
    assert warned == []
    assert added == [fake[:MAX_FILES]]
    next(gen, None)


# ---------------------------------------------------------------- group 3
class _RecordingClient(_ScriptedClient):
    """Records every enqueue_subs call (paths + kwargs) for the folder-
    add assertions; kwargs forwarding keeps it green for the WS2
    suppress_srt kwarg once that lands (defaulted on both sides)."""

    def __init__(self):
        super().__init__()
        self.subs_calls = []

    def enqueue_subs(self, paths, **kwargs):
        self.subs_calls.append((list(paths), dict(kwargs)))
        return super().enqueue_subs(paths, **kwargs)


def test_09_mode_b_choose_subs_folder_enqueues_tree_sorted_one_call(
    qapp, monkeypatch, tmp_path
):
    expected = _nested_tree(tmp_path)
    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: str(tmp_path)),
    )
    client = _RecordingClient()
    for path in expected:
        client.make_job(path, [{"state": "queued"}], kind="subs")
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.subs_add_folder_button.click()

    assert len(client.subs_calls) == 1
    assert client.subs_calls[0][0] == expected  # sorted, full tree
    assert win.job_ids == client.job_order
    # one row per nested file; the Source cell carries the FULL nested
    # path (structure preserved — never flattened)
    assert win.file_table.rowCount() == len(expected)
    for row, path in enumerate(expected):
        assert win.file_table.item(row, 1).text() == path
    next(gen, None)


def test_10_mode_b_folder_guard_names_count_and_tree(
    qapp, monkeypatch, tmp_path
):
    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: str(tmp_path)),
    )
    monkeypatch.setattr(
        gui_mod, "collect_video_files",
        lambda folder: [str(Path("/fake") / f"tv-{i}.mp4") for i in range(501)],
    )
    warned: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: warned.append(a[2] if len(a) > 2 else "")),
    )
    client = _RecordingClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.subs_add_folder_button.click()
    assert len(warned) == 1
    assert "501" in warned[0] and "500" in warned[0]
    assert "subfolders included" in warned[0]
    assert client.subs_calls == [], "the guard must refuse the whole tree"
    next(gen, None)


def test_11_mode_b_empty_folder_is_a_silent_noop(qapp, monkeypatch, tmp_path):
    empty = tmp_path / "nothing-in-here"
    empty.mkdir()
    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: str(empty)),
    )
    warned: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a))
    )
    client = _RecordingClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.subs_add_folder_button.click()
    assert warned == []  # no dialog at all — the picker-cancelled posture
    assert client.subs_calls == []
    assert win.file_table.rowCount() == 0
    next(gen, None)


class _RecordingQueueViewsClient(_QueueViewsClient):
    """_QueueViewsClient + enqueue_subs call recording — the conflict
    test needs BOTH the live views surface and the call evidence."""

    def __init__(self):
        super().__init__()
        self.subs_calls = []

    def enqueue_subs(self, paths, **kwargs):
        self.subs_calls.append((list(paths), dict(kwargs)))
        return super().enqueue_subs(paths, **kwargs)


def test_12_mode_b_folder_live_conflict_blocks_nothing_enqueued(
    qapp, monkeypatch, tmp_path
):
    """The v0.4.15.1 conflict discipline through the FOLDER surface: a
    nested file whose source is a LIVE subs job blocks the whole add
    with the same dialog — no partial enqueue under the conflict."""
    expected = _nested_tree(tmp_path)
    colliding = expected[0]  # one nested file is already live
    client = _RecordingQueueViewsClient()
    client.make_job(
        colliding,
        [
            {"state": "queued"},
            {"state": "running", "current_phase": "transcribe",
             "progress": {"segments_done": 2}},
        ],
        kind="subs",
    )
    for path in expected[1:]:
        client.make_job(path, [{"state": "queued"}], kind="subs")

    import vidlift.gui as gui_mod

    # first: enqueue the colliding file through the FILE picker, tick it
    # to running (the live state the guard reads)
    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([colliding], "")),
    )
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()
    win._tick_elapsed()  # queued snapshot
    win._tick_elapsed()  # running snapshot (the live state the guard reads)
    assert "transcribing" in win.stage_label.text()
    calls_before = list(client.subs_calls)  # file-picker call recorded

    blocked: dict = {"text": ""}
    def _capture_info(*a, **k):
        blocked["text"] = a[2] if len(a) > 2 else ""
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "information", staticmethod(_capture_info))
    # (the pre-capture information patch above keeps modals inert while
    # the run row is being set up too — the harness's own information
    # patch is replaced here, so BOTH clicks are auto-ok.)

    # now the folder add over the tree that CONTAINS the live file
    monkeypatch.setattr(
        gui_mod.QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: str(tmp_path)),
    )
    ids_before = list(win.job_ids)
    win.subs_add_folder_button.click()
    assert blocked["text"].startswith("Wait for the current batch")
    assert win.job_ids == ids_before
    assert client.subs_calls == calls_before  # no new enqueue happened
    next(gen, None)


def test_13_drop_directory_arm_walks_a_real_nested_folder(
    qapp, monkeypatch, tmp_path
):
    expected = _nested_tree(tmp_path)
    added: list[list[str]] = []
    monkeypatch.setattr(
        MainWindow, "_add_paths", lambda self, paths: added.append(list(paths))
    )
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    event, _mime = _drop_dir_event(str(tmp_path))
    win.dropEvent(event)
    assert added == [expected]
    next(gen, None)