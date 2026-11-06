"""Windows-selected media branches, not native Windows certification.

Only media_transaction.os is proxied. pathlib, subprocess and FileMutex keep
using the host OS, so a Linux pass must never be called Windows execution.
On a Windows runner the same assertions also use the real Windows primitives.
"""
import builtins
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import vidlift.media_transaction as tx


@pytest.fixture
def windows_media(monkeypatch):
    proxy = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)
                              if name != 'getuid'})
    proxy.name = 'nt'
    monkeypatch.setattr(tx, 'os', proxy)
    return proxy


def test_windows_path_handle_ctime_discrepancy_is_compatible(tmp_path, monkeypatch, windows_media):
    source = tmp_path / 'renamed.mp4'
    source.write_bytes(b'unchanged renamed bytes')
    with source.open('rb') as stream:
        expected = tx._identity(os.fstat(stream.fileno()))
    regular = tx._regular
    def pathname_stat(path):
        st = regular(path)
        return SimpleNamespace(**{name: getattr(st, name) for name in
                                  ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')},
                               st_ctime_ns=expected[4] - 100)
    monkeypatch.setattr(tx, '_regular', pathname_stat)
    fp = tx.fingerprint(source)
    assert fp['identity'] == expected
    assert fp['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize('field', ['st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'])
def test_windows_handle_mutation_still_rejected(tmp_path, monkeypatch, windows_media, field):
    source = tmp_path / 'changing.mp4'
    source.write_bytes(b'mutation sentinel')
    real_fstat = windows_media.fstat
    calls = 0
    def fstat(fd):
        nonlocal calls
        calls += 1
        st = real_fstat(fd)
        values = {name: getattr(st, name) for name in
                  ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')}
        if calls == 2:
            values[field] += 1
        return SimpleNamespace(**values)
    monkeypatch.setattr(windows_media, 'fstat', fstat)
    with pytest.raises(tx.MediaTransactionError, match='changed while hashing'):
        tx.fingerprint(source)


@pytest.mark.parametrize('field', ['st_dev', 'st_ino', 'st_size', 'st_mtime_ns'])
@pytest.mark.parametrize('timing', ['opening', 'hashing'])
def test_windows_cross_api_identity_mismatch_rejected(tmp_path, monkeypatch, windows_media, field, timing):
    source = tmp_path / 'foreign.mp4'
    source.write_bytes(b'foreign sentinel')
    regular = tx._regular
    calls = 0
    def pathname_stat(path):
        nonlocal calls
        calls += 1
        st = regular(path)
        values = {name: getattr(st, name) for name in
                  ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
        if calls == (1 if timing == 'opening' else 2):
            values[field] += 1
        return SimpleNamespace(**values)
    monkeypatch.setattr(tx, '_regular', pathname_stat)
    with pytest.raises(tx.MediaTransactionError, match='while ' + timing):
        tx.fingerprint(source)


@pytest.mark.parametrize('same', [False, True])
@pytest.mark.parametrize('suppress', [False, True])
def test_windows_selected_replacement_succeeds_and_keeps_recovery(tmp_path, windows_media, same, suppress):
    from test_media_transaction import prepared
    op = prepared(tmp_path, same=same, suppress=suppress)
    result = tx.commit_media_operation(op)
    assert result.output_path == str(op.target)
    assert op.target.read_bytes() == b'verified-output'
    assert op.backup.read_bytes() == op.captured.read_bytes() == b'original-bytes'
    assert op.backup.stat().st_ino != op.captured.stat().st_ino
    assert Path(op.candidate).read_bytes() == b'verified-output'
    assert result.sidecar_suppressed is suppress
    assert bool(result.srt_path) is not suppress
    assert not result.needs_attention
    assert result.checkpoint == 'complete'
    assert any('power-loss durability is not guaranteed' in w for w in result.warnings)
    report = tx.inspect_recovery(op.journal)
    assert report['files']['backup']['matches_original']
    assert report['files']['target']['matches_output']
    assert any('power-loss durability is not guaranteed' in w
               for w in json.loads(op.journal.read_text())['warnings'])


@pytest.mark.parametrize('fault', ['capture', 'identity', 'publication'])
def test_windows_local_capability_failure_keeps_source(tmp_path, monkeypatch, windows_media, fault):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    real_rename = windows_media.rename
    def capture(src, dst):
        if fault == 'capture':
            raise PermissionError('Windows sharing violation')
        real_rename(src, dst)
        if fault == 'identity':
            Path(dst).write_bytes(b'changed capability')
    monkeypatch.setattr(windows_media, 'rename', capture)
    if fault == 'publication':
        def unsupported(*args, **kwargs):
            raise OSError('unsupported hard-link publication')
        monkeypatch.setattr(tx, 'publish_new', unsupported)
    with pytest.raises(OSError):
        tx.prepare_media_operation(source, source, replace_source=True, suppress_srt=False)
    assert source.read_bytes() == b'original'
    assert not list(tmp_path.glob('.vidlift-media-*/original.backup'))
    assert not list(tmp_path.glob('.vidlift-media-*/original.captured'))


@pytest.mark.parametrize('fault', ['sharing', 'backup', 'late-target', 'changed-source'])
def test_windows_commit_failure_keeps_verified_success_and_original(tmp_path, monkeypatch, windows_media, fault):
    from test_media_transaction import prepared
    op = prepared(tmp_path)
    if fault == 'sharing':
        def locked(*args, **kwargs):
            raise PermissionError('Windows sharing violation')
        monkeypatch.setattr(windows_media, 'rename', locked)
    elif fault == 'backup':
        real_copy = tx._copy_checked
        def copy(src, dst, expected, cancel=None):
            if dst == op.backup:
                raise PermissionError('backup access denied')
            return real_copy(src, dst, expected, cancel)
        monkeypatch.setattr(tx, '_copy_checked', copy)
    elif fault == 'late-target':
        real_capture = tx._capture
        def capture(operation):
            real_capture(operation)
            operation.source.write_bytes(b'late occupant')
        monkeypatch.setattr(tx, '_capture', capture)
    else:
        op.source.write_bytes(b'changed source')
    result = tx.commit_media_operation(op)
    assert Path(result.output_path).read_bytes() == b'verified-output'
    expected = {'late-target': b'late occupant', 'changed-source': b'changed source'}.get(fault, b'original-bytes')
    assert op.source.read_bytes() == expected
    assert result.warnings
    if fault == 'late-target':
        assert op.backup.read_bytes() == op.captured.read_bytes() == b'original-bytes'
    else:
        assert not op.captured.exists()


def test_windows_flush_uses_writable_handle(tmp_path, monkeypatch, windows_media):
    target = tmp_path / 'owned-output'
    target.write_bytes(b'generated')
    modes = []
    def open_checked(path, mode):
        modes.append(mode)
        return builtins.open(path, mode)
    monkeypatch.setattr(tx, 'open', open_checked, raising=False)
    tx.sync_file(target)
    assert modes == ['rb+']
    assert target.read_bytes() == b'generated'


def test_windows_mode_a_generated_media_replace_suppress(tmp_path, windows_media):
    from test_media_transaction import test_mode_a_real_generated_adoption_replace_suppress as check
    check(tmp_path)


@pytest.mark.parametrize('suppress', [False, True])
def test_windows_mode_b_generated_media_replace(tmp_path, monkeypatch, windows_media, suppress):
    from test_media_transaction import test_mode_b_all_flag_combinations_with_real_generated_media as check
    check(tmp_path, monkeypatch, replace=True, suppress=suppress)


def test_windows_mode_a_sharing_failure_is_warning_success(tmp_path, monkeypatch, windows_media):
    from test_run import FakeRunner, probe_payload
    from vidlift.run import run_file, ToolBox
    source = tmp_path / 'movie.avi'
    source.write_bytes(b'original')
    real_rename = windows_media.rename
    def rename(src, dst):
        if Path(src) == source:
            raise PermissionError('Windows sharing violation')
        return real_rename(src, dst)
    monkeypatch.setattr(windows_media, 'rename', rename)
    runner = FakeRunner(probe_payload())
    result = run_file(str(source), tools=ToolBox(ffmpeg='synthetic', ffprobe='synthetic'),
                      command_runner=runner, probe_runner=runner, engine_request='stub',
                      transcription_enabled=False, replace_source=True, log_dir=str(tmp_path / 'logs'))
    assert result.ok
    assert result.state.output_path is not None
    assert Path(result.state.output_path).read_bytes() == b'output-stub'
    assert source.read_bytes() == b'original'
    backup, = tmp_path.glob('.vidlift-media-*/original.backup')
    assert backup.read_bytes() == b'original'
    assert result.state.media_transaction is not None
    assert result.state.media_transaction['needs_attention']
    journal = Path(result.state.media_transaction['journal_path'])
    assert 'sharing violation' in json.dumps(json.loads(journal.read_text())['warnings'])
