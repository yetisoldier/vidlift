"""Group identities in reviewed GUI submissions, offscreen and no executor."""
import pytest

from test_gui_best_practices import qapp, ui, media
from test_gui import _plan_for
from vidlift.mcp_bridge.client_local import LocalBridgeClient
from vidlift.plan import BatchPlan


@pytest.mark.parametrize("kind", ["subs", "run"])
def test_gui_multifile_group_matches_real_registry(ui, tmp_path, kind):
    win, _ = ui
    bridge = LocalBridgeClient(state_dir=tmp_path / "registry", start_executor=False)
    win.client = bridge
    if kind == "subs":
        paths = [media(tmp_path, name) for name in ("a.mkv", "b.mkv")]
        win.subs_device_combo.setCurrentIndex(win.subs_device_combo.findData("cuda"))
        win._enqueue_subs_paths(paths)
        win.start_subtitles()
    else:
        folders = [tmp_path / "one", tmp_path / "two"]
        for folder in folders:
            folder.mkdir()
        win.batch = BatchPlan([_plan_for(folder) for folder in folders])
        win.render_batch(win.batch)
        win.start_upscale()
    queued = bridge.registry.queued()
    assert len(queued) == 2
    batch = queued[0].options["batch_id"]
    assert batch and queued[1].options["batch_id"] == batch
    if kind == "subs":
        assert all(r.options["device"] == "cuda" for r in queued)
    for record in queued:
        bridge.registry.transition(record, "running")
        bridge.registry.transition(record, "failed")
    win._tick_elapsed()
    win._tick_elapsed()
    if kind == "subs":
        win._enqueue_subs_paths([media(tmp_path, "next.mkv")])
        win.start_subtitles()
    else:
        win.start_upscale()
    new = bridge.registry.queued()
    assert new and all(r.options["batch_id"] != batch for r in new)
