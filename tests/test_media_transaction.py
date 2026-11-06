"""Adversarial synthetic-byte transactions, independent of ffmpeg/GUI."""
import json
import multiprocessing
import os
from pathlib import Path

import pytest

import vidlift.media_transaction as tx
from vidlift.run import EngineCancelled


def prepared(root, *, replacing=True, same=True, suppress=False):
    root.mkdir(parents=True, exist_ok=True)
    source = root / 'movie.avi'
    source.write_bytes(b'original-bytes')
    target = source if same else root / 'movie.mp4'
    op = tx.prepare_media_operation(source, target, replace_source=replacing, suppress_srt=suppress)
    candidate = op.directory / 'movie.subs.mp4'
    candidate.write_bytes(b'verified-output')
    srt = op.directory / 'movie.subs.srt'
    srt.write_bytes(b'generated-subtitles')
    op.embedded = True
    op.fallback = str(root / 'movie.subs.mp4')
    op.sidecar_target = str(root / 'movie.srt')
    tx.record_verified_output(op, candidate, verification=lambda _: {'ok': True, 'kind': 'synthetic-test-verifier', 'checks': ['bytes']}, owned_srt=srt)
    return op


@pytest.mark.parametrize('same', [False, True])
@pytest.mark.parametrize('suppress', [False, True])
def test_recoverable_replacement_and_suppression(tmp_path, same, suppress):
    op = prepared(tmp_path, same=same, suppress=suppress)
    result = tx.commit_media_operation(op)
    assert Path(result.output_path).read_bytes() == b'verified-output'
    assert op.backup.read_bytes() == op.captured.read_bytes() == b'original-bytes'
    assert op.backup.stat().st_ino != op.captured.stat().st_ino
    assert result.sidecar_suppressed is suppress
    assert bool(result.srt_path) is not suppress
    assert json.loads(op.journal.read_text())['phase'] == 'complete'
    before = {p: p.read_bytes() for p in op.directory.iterdir() if p.is_file()}
    report = tx.inspect_recovery(op.journal)
    assert report['files']['target']['matches_output']
    assert tx.inspect_recovery(op.journal) == report
    assert before == {p: p.read_bytes() for p in op.directory.iterdir() if p.is_file()}


def test_structured_verification_not_boolean_and_changed_candidate(tmp_path):
    op = prepared(tmp_path)
    with pytest.raises(tx.MediaTransactionError, match='structured'):
        tx.record_verified_output(op, op.candidate, verification=True)
    Path(op.candidate).write_bytes(b'corrupt')
    with pytest.raises(tx.MediaTransactionError, match='candidate changed'):
        tx.commit_media_operation(op)
    assert op.source.read_bytes() == b'original-bytes'
    assert not op.backup.exists()


@pytest.mark.parametrize('dangling', [False, True])
def test_late_target_and_sidecar_never_clobbered(tmp_path, dangling):
    op = prepared(tmp_path, same=False)
    if dangling:
        op.target.symlink_to(tmp_path / 'missing')
    else:
        op.target.write_bytes(b'incumbent')
    incumbent = tmp_path / 'movie.srt'
    incumbent.write_bytes(b'incumbent subtitle')
    result = tx.commit_media_operation(op)
    assert os.path.lexists(op.target)
    assert op.source.read_bytes() == b'original-bytes'
    if not dangling:
        assert op.target.read_bytes() == b'incumbent'
    assert incumbent.read_bytes() == b'incumbent subtitle'
    assert Path(result.output_path).read_bytes() == b'verified-output'
    assert Path(result.srt_path).read_bytes() == b'generated-subtitles'


def test_source_changed_before_commit_keeps_new_source(tmp_path):
    op = prepared(tmp_path)
    op.source.write_bytes(b'foreign-changed-source')
    result = tx.commit_media_operation(op)
    assert op.source.read_bytes() == b'foreign-changed-source'
    assert Path(result.output_path).read_bytes() == b'verified-output'
    assert not op.captured.exists()


@pytest.mark.parametrize('occupied', [False, True])
def test_capture_then_validate_never_deletes_unexpected_object(tmp_path, monkeypatch, occupied):
    op = prepared(tmp_path)
    real = os.rename
    def swap(source, captured):
        if Path(source) == op.source:
            op.source.rename(tmp_path / 'original-external-save')
            op.source.write_bytes(b'foreign-raced-source')
        real(source, captured)
        if occupied:
            op.source.write_bytes(b'late-occupant')
    monkeypatch.setattr(tx.os, 'rename', swap)
    # Avoid recursive patched rename in swap.
    monkeypatch.setattr(Path, 'rename', lambda self, dst: real(self, dst))
    result = tx.commit_media_operation(op)
    assert op.backup.read_bytes() == b'original-bytes'
    assert op.captured.read_bytes() == b'foreign-raced-source'
    assert op.source.read_bytes() == (b'late-occupant' if occupied else b'foreign-raced-source')
    assert result.needs_attention
    assert Path(result.output_path).read_bytes() == b'verified-output'


def test_incumbent_after_capture_wins(tmp_path, monkeypatch):
    op = prepared(tmp_path)
    real = tx._capture
    def capture(operation):
        real(operation)
        operation.source.write_bytes(b'late-incumbent')
    monkeypatch.setattr(tx, '_capture', capture)
    result = tx.commit_media_operation(op)
    assert op.source.read_bytes() == b'late-incumbent'
    assert op.captured.read_bytes() == op.backup.read_bytes() == b'original-bytes'
    assert result.needs_attention
    assert Path(result.output_path).read_bytes() == b'verified-output'


@pytest.mark.parametrize('phase', ['backup_ready', 'commit_intent', 'source_captured', 'target_published', 'complete'])
@pytest.mark.parametrize('after', [False, True])
def test_journal_failure_gaps_preserve_evidence(tmp_path, monkeypatch, phase, after):
    op = prepared(tmp_path)
    real = tx.PreparedOperation.persist
    def persist(self, checkpoint):
        if checkpoint == phase and not after:
            raise OSError('journal fault before ' + phase)
        real(self, checkpoint)
        if checkpoint == phase and after:
            raise OSError('journal fault after ' + phase)
    monkeypatch.setattr(tx.PreparedOperation, 'persist', persist)
    result = tx.commit_media_operation(op)
    assert Path(result.output_path).read_bytes() == b'verified-output'
    assert op.backup.read_bytes() == b'original-bytes'
    report = tx.inspect_recovery(op.journal)
    assert any(f.get('matches_original') for f in report['files'].values())
    assert any(f.get('matches_output') for f in report['files'].values())


@pytest.mark.parametrize('phase', ['output_verified', 'backup_ready', 'commit_intent', 'source_captured', 'target_published'])
def test_cancellation_safe_checkpoints(tmp_path, monkeypatch, phase):
    op = prepared(tmp_path)
    cancelled = phase == 'output_verified'
    real = tx.PreparedOperation.persist
    def persist(self, checkpoint):
        nonlocal cancelled
        real(self, checkpoint)
        if checkpoint == phase:
            cancelled = True
    monkeypatch.setattr(tx.PreparedOperation, 'persist', persist)
    if phase in {'output_verified', 'backup_ready'}:
        with pytest.raises(EngineCancelled):
            tx.commit_media_operation(op, cancel=lambda: cancelled)
        assert op.source.read_bytes() == b'original-bytes'
    else:
        result = tx.commit_media_operation(op, cancel=lambda: cancelled)
        assert Path(result.output_path).read_bytes() == b'verified-output'
        assert any('late cancellation' in w for w in result.warnings)
    assert Path(op.candidate).read_bytes() == b'verified-output'


@pytest.mark.parametrize('fault', ['copy', 'fsync', 'permission', 'hash'])
def test_backup_failure_never_retires_source(tmp_path, monkeypatch, fault):
    op = prepared(tmp_path)
    real = tx._copy_checked
    def copy(source, destination, expected, cancel=None):
        if destination == op.backup:
            raise OSError({'copy': 'No space left on device', 'fsync': 'fsync failed', 'permission': 'permission denied', 'hash': 'copy hash mismatch'}[fault])
        return real(source, destination, expected, cancel)
    monkeypatch.setattr(tx, '_copy_checked', copy)
    result = tx.commit_media_operation(op)
    assert op.source.read_bytes() == b'original-bytes'
    assert not op.captured.exists()
    assert Path(result.output_path).read_bytes() == b'verified-output'


def test_alias_hardlink_and_symlink_preflight(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'original')
    with pytest.raises(tx.MediaTransactionError, match='alias'):
        tx.prepare_media_operation(source, source, replace_source=False, suppress_srt=False)
    os.link(source, tmp_path / 'alias')
    with pytest.raises(tx.MediaTransactionError, match='hardlink'):
        tx.prepare_media_operation(source, source, replace_source=True, suppress_srt=False)
    symlink = tmp_path / 'symlink'
    symlink.symlink_to(source)
    with pytest.raises(tx.MediaTransactionError):
        tx.prepare_media_operation(symlink, tmp_path / 'out', replace_source=True, suppress_srt=False)


def _kill_worker(root, phase, after):
    op = prepared(Path(root))
    real = tx.PreparedOperation.persist
    def persist(self, checkpoint):
        if checkpoint == phase and not after:
            os._exit(77)
        real(self, checkpoint)
        if checkpoint == phase and after:
            os._exit(77)
    tx.PreparedOperation.persist = persist
    tx.commit_media_operation(op)


@pytest.mark.parametrize('phase', ['backup_ready', 'commit_intent', 'source_captured', 'target_published', 'complete'])
@pytest.mark.parametrize('after', [False, True])
def test_spawn_kill_and_read_only_recovery(tmp_path, phase, after):
    child = multiprocessing.get_context('spawn').Process(target=_kill_worker, args=(str(tmp_path), phase, after))
    child.start()
    child.join(15)
    assert child.exitcode == 77
    journal, = tmp_path.glob('.vidlift-media-*/journal.json')
    report = tx.inspect_recovery(journal)
    assert any(f.get('matches_original') for f in report['files'].values())
    assert any(f.get('matches_output') for f in report['files'].values())
    assert report == tx.inspect_recovery(journal)


def _competing_writer(root, state, name, barrier, results):
    root = Path(root)
    os.environ['VIDLIFT_STATE_DIR'] = str(root / state)
    source = root / 'source.mp4'
    op = tx.prepare_media_operation(source, source, replace_source=True, suppress_srt=True)
    candidate = op.directory / 'output.mp4'
    candidate.write_bytes(name.encode())
    op.fallback = str(root / (name + '.mp4'))
    tx.record_verified_output(op, candidate, verification=lambda _: {'ok': True, 'kind': 'synthetic-competitor'})
    barrier.wait(timeout=15)
    result = tx.commit_media_operation(op)
    results.put((name, result.output_path, result.backup_path, result.needs_attention))


def test_spawn_competing_writers_different_state_roots_share_locks(tmp_path):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    ctx = multiprocessing.get_context('spawn')
    barrier, results = ctx.Barrier(2), ctx.Queue()
    children = [ctx.Process(target=_competing_writer, args=(str(tmp_path), 'state-' + name, name, barrier, results)) for name in ['one', 'two']]
    for child in children:
        child.start()
    for child in children:
        child.join(20)
        assert child.exitcode == 0
    rows = [results.get(timeout=2) for _ in children]
    replaced = [row for row in rows if row[1] == str(source)]
    assert len(replaced) == 1
    assert source.read_bytes() == replaced[0][0].encode()
    assert Path(replaced[0][2]).read_bytes() == b'original'
    for name, output, _, _ in rows:
        assert Path(output).read_bytes() == name.encode()


def _barrier_capture_worker(root, timing, ready, proceed, results):
    op = prepared(Path(root))
    real = tx._capture
    def capture(operation):
        if timing == 'after':
            real(operation)
        ready.set()
        assert proceed.wait(15)
        if timing == 'before':
            real(operation)
    tx._capture = capture
    result = tx.commit_media_operation(op)
    results.put((result.output_path, result.needs_attention, str(op.directory)))


@pytest.mark.parametrize('timing', ['before', 'after'])
def test_spawn_external_writer_at_capture_barrier(tmp_path, timing):
    ctx = multiprocessing.get_context('spawn')
    ready, proceed, results = ctx.Event(), ctx.Event(), ctx.Queue()
    child = ctx.Process(target=_barrier_capture_worker, args=(str(tmp_path), timing, ready, proceed, results))
    child.start()
    assert ready.wait(15)
    source = tmp_path / 'movie.avi'
    if timing == 'before':
        source.rename(tmp_path / 'external-save')
    source.write_bytes(b'foreign-incumbent')
    proceed.set()
    child.join(20)
    assert child.exitcode == 0
    output, attention, directory = results.get(timeout=2)
    assert attention
    assert source.read_bytes() == b'foreign-incumbent'
    assert Path(output).read_bytes() == b'verified-output'
    assert (Path(directory) / 'original.backup').read_bytes() == b'original-bytes'
    assert (Path(directory) / 'original.captured').read_bytes() == (b'foreign-incumbent' if timing == 'before' else b'original-bytes')


def test_prepared_replay_is_refused_without_mutation(tmp_path):
    op = prepared(tmp_path)
    tx.commit_media_operation(op)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = tx.commit_media_operation(op)
    assert result.needs_attention
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_parent_swap_and_occupied_capture_retained(tmp_path):
    op = prepared(tmp_path)
    op.captured.write_bytes(b'foreign-slot')
    result = tx.commit_media_operation(op)
    assert result.needs_attention
    assert op.source.read_bytes() == b'original-bytes'
    assert op.captured.read_bytes() == b'foreign-slot'


def test_changed_parent_identity_refuses_destructive_progress(tmp_path):
    root = tmp_path / 'root'
    op = prepared(root)
    root.rename(tmp_path / 'saved-root')
    root.mkdir()
    op.source.write_bytes(b'foreign')
    with pytest.raises(OSError):
        tx.commit_media_operation(op)
    assert op.source.read_bytes() == b'foreign'
    assert (tmp_path / 'saved-root' / op.directory.name / 'movie.subs.mp4').read_bytes() == b'verified-output'


def test_atomic_publication_capability_failure_refuses_before_work(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original')
    def unsupported(*a, **k):
        raise OSError('unsupported atomic hardlink publication')
    monkeypatch.setattr(tx, 'publish_new', unsupported)
    with pytest.raises(OSError, match='unsupported atomic'):
        tx.prepare_media_operation(source, source, replace_source=True, suppress_srt=False)
    assert source.read_bytes() == b'original'


def test_lock_timeout_retains_verified_private_output(tmp_path, monkeypatch):
    op = prepared(tmp_path)
    monkeypatch.setattr(tx.FileMutex, 'acquire', lambda *a, **k: False)
    result = tx.commit_media_operation(op)
    assert result.needs_attention
    assert result.output_path == op.candidate
    assert op.source.read_bytes() == b'original-bytes'
    assert any('lock timeout' in warning for warning in result.warnings)


def test_lock_wait_cancellation_is_not_warning_success(tmp_path, monkeypatch):
    op = prepared(tmp_path)
    calls = 0
    def unavailable(*a, **k):
        nonlocal calls
        calls += 1
        return False
    monkeypatch.setattr(tx.FileMutex, 'acquire', unavailable)
    with pytest.raises(EngineCancelled):
        tx.commit_media_operation(op, cancel=lambda: calls > 0)
    assert op.source.read_bytes() == b'original-bytes'


def test_private_sidecar_identity_change_never_suppresses_foreign(tmp_path):
    op = prepared(tmp_path, suppress=True)
    Path(op.owned_srt).write_bytes(b'foreign-private-file')
    result = tx.commit_media_operation(op)
    assert not result.sidecar_suppressed
    assert result.needs_attention
    assert Path(op.owned_srt).read_bytes() == b'foreign-private-file'


def test_copy_and_verification_observe_cancellation(tmp_path):
    op = prepared(tmp_path)
    with pytest.raises(EngineCancelled):
        tx._copy_checked(op.source, op.directory / 'cancel-copy', op.source_fingerprint, lambda: True)
    def verify(_):
        op.cancel = lambda: True
        return {'ok': True, 'kind': 'synthetic'}
    with pytest.raises(EngineCancelled):
        tx.record_verified_output(op, op.candidate, verification=verify)
    assert op.source.read_bytes() == b'original-bytes'


def test_cross_device_publication_uses_target_sibling_stage(tmp_path, monkeypatch):
    op = prepared(tmp_path)
    elsewhere = tmp_path / 'output-fs'
    elsewhere.mkdir()
    destination = elsewhere / 'movie.mp4'
    real_stat = Path.stat
    def other_device(self, *a, **k):
        st = real_stat(self, *a, **k)
        if self == elsewhere:
            from types import SimpleNamespace
            return SimpleNamespace(st_dev=st.st_dev + 1, st_ino=st.st_ino, st_uid=st.st_uid, st_mode=st.st_mode)
        return st
    monkeypatch.setattr(Path, 'stat', other_device)
    staged = tx._stage_publication(op, destination)
    assert staged.parent.parent == elsewhere
    assert staged.read_bytes() == Path(op.candidate).read_bytes()
    assert op.source.read_bytes() == b'original-bytes'


@pytest.mark.parametrize('replace', [False, True])
@pytest.mark.parametrize('suppress', [False, True])
def test_mode_b_all_flag_combinations_with_real_generated_media(tmp_path, monkeypatch, replace, suppress):
    import shutil
    import subprocess
    from vidlift import subs_job
    from vidlift.transcribe import TranscriptionResult
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    assert ffmpeg and ffprobe, 'real generated-media gate requires ffmpeg and ffprobe'
    source = tmp_path / 'generated.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=160x120:r=10:d=1',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(source)], check=True)
    original = source.read_bytes()
    def transcribe(_source, *, output_path, **kwargs):
        srt = Path(output_path).with_suffix('.srt')
        srt.write_text('1\n00:00:00,000 --> 00:00:00,800\nGenerated fixture\n')
        return TranscriptionResult(srt_path=str(srt), cue_count=1, language='en', language_confidence=1.0, model='synthetic')
    monkeypatch.setattr(subs_job, 'transcribe_source_to_srt', transcribe)
    result = subs_job.run_subs_job(str(source), ffmpeg=ffmpeg, ffprobe=ffprobe, device_pin='cpu', replace_source=replace, suppress_srt=suppress)
    media = subs_job.probe_file(result.output_path, binary=ffprobe)
    assert any(s.codec_name == 'mov_text' for s in media.subtitles)
    subprocess.run([ffmpeg, '-v', 'error', '-i', result.output_path, '-f', 'null', '-'], check=True)
    assert bool(result.srt_path) is not suppress
    if replace:
        assert result.output_path == str(source)
        backup, = tmp_path.glob('.vidlift-media-*/original.backup')
        assert backup.read_bytes() == original
    else:
        assert source.read_bytes() == original


@pytest.mark.parametrize('fault', ['missing-subtitle', 'wrong-subtitle', 'video', 'audio', 'duration', 'decode'])
def test_mode_a_bad_final_mux_keeps_verified_encode_and_srt(tmp_path, fault):
    from test_subtitles_v041 import TestS12Mux
    from vidlift.subtitles import mux_srt_into_mp4, SubtitleOcrError
    from types import SimpleNamespace
    harness = TestS12Mux()
    video = harness._mp4(tmp_path)
    before = video.read_bytes()
    srt = tmp_path / 'adopted-original.srt'
    srt.write_bytes(b'original-subtitle')
    def runner(_binary, args):
        Path(args[-1]).write_bytes(b'invalid-mux')
        return 0, '', ''
    def probe(binary, args):
        code, payload, err = harness._probe(binary, args)
        data = json.loads(payload)
        if args[-1].endswith('.muxing.mp4'):
            if fault == 'missing-subtitle':
                data['streams'].pop()
            elif fault == 'wrong-subtitle':
                data['streams'][-1]['codec_name'] = 'subrip'
            elif fault == 'video':
                data['streams'][0]['width'] = 12
            elif fault == 'audio':
                data['streams'][1]['channels'] = 1
            elif fault == 'duration':
                data['format']['duration'] = '50'
        return code, json.dumps(data), err
    with pytest.raises(SubtitleOcrError):
        mux_srt_into_mp4(str(video), str(srt), ffmpeg='synthetic', runner=runner,
                        ownership=harness.operation, probe_runner=probe,
                        verifier=lambda _: SimpleNamespace(ok=fault != 'decode'))
    assert video.read_bytes() == before
    assert srt.read_bytes() == b'original-subtitle'


def test_mode_a_real_generated_adoption_replace_suppress(tmp_path):
    import shutil
    import subprocess
    from vidlift.run import run_file, ToolBox
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    assert ffmpeg and ffprobe
    source = tmp_path / 'generated.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=640x360:r=10:d=1',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(source)], check=True)
    original = source.read_bytes()
    adopted = source.with_suffix('.srt')
    adopted.write_text('1\n00:00:00,000 --> 00:00:00,800\nGenerated adoption\n')
    srt_before = adopted.read_bytes()
    result = run_file(str(source), tools=ToolBox(ffmpeg=ffmpeg, ffprobe=ffprobe),
                      engine_request='stub', transcription_enabled=False,
                      replace_source=True, suppress_srt=True, log_dir=str(tmp_path / 'logs'))
    assert result.ok and result.state.output_path == str(source)
    assert result.verification.full_decode_ok and result.verification.subtitle_ok
    assert result.state.subtitles['sidecar_suppressed']
    assert adopted.read_bytes() == srt_before
    backup, = tmp_path.glob('.vidlift-media-*/original.backup')
    assert backup.read_bytes() == original
    manifest = json.loads(Path(result.manifest_path).read_text())
    assert str(source) in json.dumps(manifest['artifacts'])


def test_custom_default_spelling_and_unexpected_workdir_contents_retained(tmp_path, monkeypatch):
    from vidlift import subs_job
    from test_v0414_subs_job import FakeRunner, FakeEngine, _probe_payload
    from vidlift import transcribe
    monkeypatch.delenv('VIDLIFT_TRANSCRIPTION_ENGINE', raising=False)
    monkeypatch.setattr(transcribe, 'resolve_model_dir', lambda *a, **k: 'synthetic')
    source = tmp_path / 'movie.avi'
    source.write_bytes(b'original')
    custom = tmp_path / 'movie.subs.mp4.subs-work'
    custom.mkdir()
    (custom / 'keep').write_bytes(b'caller-owned')
    runner = FakeRunner(_probe_payload())
    result = subs_job.run_subs_job(str(source), ffmpeg='synthetic', runner=runner, probe_runner=runner,
                                  engine_factory=FakeEngine, device_pin='cpu', workdir=str(custom))
    assert Path(result.output_path).read_bytes() == b'muxed-out'
    assert (custom / 'keep').read_bytes() == b'caller-owned'
    assert not any('cleanup subs scratch' in n for n in result.notes)
    # Even a default-looking helper call may not recursively erase strangers.
    assert subs_job.cleanup_subs_scratch(str(tmp_path / 'movie.subs.mp4'), str(custom)) == []
    assert (custom / 'keep').read_bytes() == b'caller-owned'


def test_publication_syscall_success_exception_reports_actual_target(tmp_path, monkeypatch):
    op = prepared(tmp_path)
    real = tx.publish_new
    def published_then_failed(source, destination):
        real(source, destination)
        if Path(destination) == op.target:
            raise OSError('injected acknowledgement gap after link')
    monkeypatch.setattr(tx, 'publish_new', published_then_failed)
    result = tx.commit_media_operation(op)
    assert result.output_path == str(op.target)
    assert op.target.read_bytes() == b'verified-output'
    assert op.backup.read_bytes() == op.captured.read_bytes() == b'original-bytes'
    assert result.needs_attention
    assert not Path(op.fallback).exists()


def _kill_mutation_worker(root, boundary):
    op = prepared(Path(root))
    if boundary == 'capture':
        real = tx.os.rename
        def rename(source, destination):
            real(source, destination)
            if Path(source) == op.source:
                os._exit(78)
        tx.os.rename = rename
    else:
        real = tx.publish_new
        def publish(source, destination):
            real(source, destination)
            if Path(destination) == op.target:
                os._exit(78)
        tx.publish_new = publish
    tx.commit_media_operation(op)


@pytest.mark.parametrize('boundary', ['capture', 'publish'])
def test_spawn_kill_in_syscall_acknowledgement_gap(tmp_path, boundary):
    child = multiprocessing.get_context('spawn').Process(target=_kill_mutation_worker, args=(str(tmp_path), boundary))
    child.start()
    child.join(15)
    assert child.exitcode == 78
    journal, = tmp_path.glob('.vidlift-media-*/journal.json')
    report = tx.inspect_recovery(journal)
    assert report['files']['backup']['matches_original']
    assert report['files']['captured']['matches_original']
    assert report['files']['candidate']['matches_output']
    if boundary == 'publish':
        assert report['files']['target']['matches_output']
    assert report == tx.inspect_recovery(journal)


def test_manifest_failure_after_commit_never_invents_manifest_path(tmp_path, monkeypatch):
    from test_run import FakeRunner, probe_payload
    from vidlift.run import run_file, ToolBox
    source = tmp_path / 'movie.avi'
    source.write_bytes(b'original')
    real = Path.write_text
    def write(self, *a, **k):
        if self.name.endswith('.manifest.json'):
            raise OSError('manifest disk failure')
        return real(self, *a, **k)
    monkeypatch.setattr(Path, 'write_text', write)
    runner = FakeRunner(probe_payload())
    result = run_file(str(source), tools=ToolBox(ffmpeg='synthetic', ffprobe='synthetic'),
                      command_runner=runner, probe_runner=runner, engine_request='stub',
                      transcription_enabled=False, replace_source=True, log_dir=str(tmp_path / 'logs'))
    assert result.ok and result.state.output_path == str(source)
    assert result.manifest_path == '' and result.state.manifest_path is None
    assert source.read_bytes() == b'output-stub'
    report = tx.inspect_recovery(result.state.media_transaction['journal_path'])
    assert report['files']['target']['matches_output']
    assert report['files']['backup']['matches_original']
    assert list((tmp_path / 'logs').glob('*.prepared.mkv'))
