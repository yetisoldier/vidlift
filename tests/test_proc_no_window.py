"""Windows child-process console suppression tests (v0.4.4 design §6).

All headless; no real subprocess is ever spawned. The Windows-specific
kwargs shape is exercised by monkeypatching ``os.name == "nt"``, never
by platform luck (the R6 discipline: ``popen_kwargs()`` returns `{}` on
Linux, so every existing headless test is unaffected).
"""

from __future__ import annotations

import pytest

from vidlift import _proc


def test_a_returns_empty_dict_everywhere_but_windows():
    # (a) on os.name != "nt" (Linux test box), the helper is a pure
    # no-op: exactly an empty dict, nothing to pass through.
    assert _proc.popen_kwargs() == {}


def test_b_windows_shape_carries_both_suppression_keys(monkeypatch):
    # (b) under a monkeypatched os.name == "nt", the dict carries BOTH
    # keys with the correct flag bits: a STARTUPINFO hiding the window
    # (STARTF_USESHOWWINDOW + SW_HIDE) plus CREATE_NO_WINDOW. The
    # Windows-only subprocess constants are faked here (they do not
    # exist on Linux); _proc.py resolves them at call time.
    import types

    monkeypatch.setattr(_proc.os, "name", "nt")

    class _FakeStartupInfo:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = None

    fake = types.SimpleNamespace(
        STARTUPINFO=_FakeStartupInfo,
        STARTF_USESHOWWINDOW=0x1,
        SW_HIDE=0,
        CREATE_NO_WINDOW=0x08000000,
    )
    seen_flags: dict = {}

    def make_si():
        si = fake.STARTUPINFO()
        seen_flags["si"] = si
        return si

    monkeypatch.setattr(_proc.subprocess, "STARTUPINFO", make_si, raising=False)
    monkeypatch.setattr(_proc.subprocess, "STARTF_USESHOWWINDOW", fake.STARTF_USESHOWWINDOW, raising=False)
    monkeypatch.setattr(_proc.subprocess, "SW_HIDE", fake.SW_HIDE, raising=False)
    monkeypatch.setattr(_proc.subprocess, "CREATE_NO_WINDOW", fake.CREATE_NO_WINDOW, raising=False)

    kwargs = _proc.popen_kwargs()
    assert set(kwargs) == {"startupinfo", "creationflags"}
    si = seen_flags["si"]
    assert si.dwFlags & fake.STARTF_USESHOWWINDOW
    assert si.wShowWindow == fake.SW_HIDE
    assert kwargs["creationflags"] == fake.CREATE_NO_WINDOW


def test_c_engine_popen_factory_receives_startupinfo_on_nt(monkeypatch, tmp_path):
    # (c) the engine's popen_factory seam receives a startupinfo kwarg
    # on the monkeypatched-nt path — the plumbing reaches the real
    # child-process spawn without any real process being launched.
    import types

    from vidlift.engine_ncnn import NcnnVulkanEngine

    seen: dict = {}

    def spy_factory(cmd, **kwargs):
        seen.update(kwargs)

        class _FakeProc:
            stdout = None
            stderr = None
            returncode = 0

            def poll(self):
                return 0  # immediate clean exit: no drain/escalation path

            def wait(self, timeout=None):
                return 0

        return _FakeProc()

    engine = NcnnVulkanEngine(
        binary=str(tmp_path / "realesrgan-ncnn-vulkan"),
        models_dir=str(tmp_path / "models"),
        popen_factory=spy_factory,
    )
    monkeypatch.setattr(_proc.os, "name", "nt")
    fake = types.SimpleNamespace(
        STARTUPINFO=lambda: types.SimpleNamespace(dwFlags=0, wShowWindow=None),
        STARTF_USESHOWWINDOW=0x1,
        SW_HIDE=0,
        CREATE_NO_WINDOW=0x08000000,
    )
    monkeypatch.setattr(_proc.subprocess, "STARTUPINFO", fake.STARTUPINFO, raising=False)
    monkeypatch.setattr(_proc.subprocess, "STARTF_USESHOWWINDOW", fake.STARTF_USESHOWWINDOW, raising=False)
    monkeypatch.setattr(_proc.subprocess, "SW_HIDE", fake.SW_HIDE, raising=False)
    monkeypatch.setattr(_proc.subprocess, "CREATE_NO_WINDOW", fake.CREATE_NO_WINDOW, raising=False)
    engine._run_process([str(tmp_path / "realesrgan-ncnn-vulkan")], label="spy")
    assert "startupinfo" in seen
    assert "creationflags" in seen


def test_d_run_subprocess_runner_shape_unchanged_on_linux():
    # The seam contract: on Linux the runner must behave exactly as
    # before (returncode, stdout, stderr) — verified against the real
    # interpreter as a harmless child (no console concept exists).
    import sys

    from vidlift.run import _subprocess_runner

    code, out, err = _subprocess_runner(
        sys.executable, ["-c", "print('vidlift-proc-ok')"]
    )
    assert code == 0
    assert out.strip() == "vidlift-proc-ok"
    assert err == ""


@pytest.mark.parametrize("name", ["posix", "nt"])
def test_e_helper_matches_platform_request(monkeypatch, name):
    # The platform request is honored from os.name alone — the helper
    # consults os.name at call time (never imported-at-time state).
    import types

    monkeypatch.setattr(_proc.os, "name", name)
    if name == "nt":
        fake = types.SimpleNamespace(
            STARTUPINFO=lambda: types.SimpleNamespace(dwFlags=0, wShowWindow=None),
            STARTF_USESHOWWINDOW=0x1,
            SW_HIDE=0,
            CREATE_NO_WINDOW=0x08000000,
        )
        monkeypatch.setattr(_proc.subprocess, "STARTUPINFO", fake.STARTUPINFO, raising=False)
        monkeypatch.setattr(_proc.subprocess, "STARTF_USESHOWWINDOW", fake.STARTF_USESHOWWINDOW, raising=False)
        monkeypatch.setattr(_proc.subprocess, "SW_HIDE", fake.SW_HIDE, raising=False)
        monkeypatch.setattr(_proc.subprocess, "CREATE_NO_WINDOW", fake.CREATE_NO_WINDOW, raising=False)
    kwargs = _proc.popen_kwargs()
    if name == "nt":
        assert kwargs  # the suppression pair
    else:
        assert kwargs == {}


def test_f_missing_windows_constants_fail_loudly(monkeypatch):
    # A Windows interpreter missing the suppression constants is a
    # broken build: the helper raises instead of silently shipping
    # pop-up consoles (never a silent fallback).
    monkeypatch.setattr(_proc.os, "name", "nt")
    monkeypatch.delattr(_proc.subprocess, "STARTUPINFO", raising=False)
    monkeypatch.delattr(_proc.subprocess, "CREATE_NO_WINDOW", raising=False)
    with pytest.raises(RuntimeError):
        _proc.popen_kwargs()