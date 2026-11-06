"""Private media publication and recoverable, opt-in source retirement.

The mutex coordinates cooperating VidLift processes, not arbitrary writers.
Capture-then-validate can temporarily move an unexpected source. Such an
object is restored only to an absent name, otherwise retained, never deleted.
Journals are evidence, not executable instructions. Originals are kept forever.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid
from typing import Any, Callable

from .file_mutex import FileMutex
from .safe_files import publish_new


class MediaTransactionError(OSError):
    pass


def observe_cancel(cancel):
    if cancel is not None and cancel():
        from .run import EngineCancelled
        raise EngineCancelled("cancelled before media commit; private diagnostics retained")


def _path(value):
    return Path(os.path.abspath(value))


def _regular(path):
    st = Path(path).lstat()
    if not stat.S_ISREG(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
        raise MediaTransactionError(f"regular non-reparse file required: {path}")
    if not st.st_ino or st.st_mtime_ns is None or st.st_ctime_ns is None:
        raise MediaTransactionError(f"stable file identity is unsupported: {path}")
    return st


def _identity(st):
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def _path_matches_handle(path_stat, handle_stat):
    # Windows 3.12/3.13 pathname ctime can mean birth time while fstat
    # reports change time. Only compare compatible fields across APIs;
    # handle-to-handle mutation checks and stored fingerprints keep ctime.
    end = 4 if os.name == "nt" else 5
    return _identity(path_stat)[:end] == _identity(handle_stat)[:end]


def fingerprint(path, cancel=None):
    """Hash via a no-follow stable handle and reject observed mutation."""
    before = _regular(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if not _path_matches_handle(before, opened):
            raise MediaTransactionError(f"identity changed while opening: {path}")
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while block := stream.read(1024 * 1024):
                observe_cancel(cancel)
                digest.update(block)
        after = os.fstat(fd)
        if _identity(after) != _identity(opened) or not _path_matches_handle(_regular(path), after):
            raise MediaTransactionError(f"file changed while hashing: {path}")
        return {"identity": _identity(after), "sha256": digest.hexdigest(), "links": after.st_nlink}
    finally:
        os.close(fd)


def _parent(path):
    # Resolve is deliberately not used to conceal a symlink component.
    path = _path(path)
    for part in (path, *path.parents):
        st = part.lstat()
        if not stat.S_ISDIR(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            raise MediaTransactionError(f"non-directory/symlink/reparse parent: {part}")
    st = path.stat()
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise MediaTransactionError(f"parent is not owned by this user: {path}")
    if os.name == "posix" and st.st_mode & 0o002:
        raise MediaTransactionError(f"parent is writable by other users: {path}")
    return [st.st_dev, st.st_ino]


def sync_dir(path):
    if os.name == "nt":
        # Windows has no directory-fsync equivalent here. Retain recovery
        # material, but do not promise POSIX power-loss durability.
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_file(path):
    # Windows FlushFileBuffers (via _commit) requires write access. These
    # are VidLift's generated files, not the read-only original input.
    with open(path, "rb+" if os.name == "nt" else "rb") as stream:
        os.fsync(stream.fileno())


@contextmanager
def _locks(paths, cancel=None, timeout=2.0):
    held = []
    try:
        for path in sorted(set(_path(p) for p in paths), key=str):
            parent_id = _parent(path.parent)
            root = path.parent / ".vidlift-locks"
            root.mkdir(mode=0o700, exist_ok=True)
            _parent(root)
            if os.name == "posix" and root.stat().st_mode & 0o077:
                raise MediaTransactionError("mutex root is not private")
            name = hashlib.sha256(os.fsencode(os.path.normcase(str(path)))).hexdigest()
            lockpath = root / (name + ".lock")
            fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
                    raise MediaTransactionError("unsafe mutex inode")
                lock_id = [st.st_dev, st.st_ino]
            finally:
                os.close(fd)
            mutex = FileMutex(lockpath)
            deadline = time.monotonic() + timeout
            while not mutex.acquire(timeout=0):
                observe_cancel(cancel)
                if time.monotonic() >= deadline:
                    raise MediaTransactionError(f"media lock timeout: {path}")
                time.sleep(0.01)
            held.append(mutex)
            observe_cancel(cancel)
            current = _regular(lockpath)
            if [current.st_dev, current.st_ino] != lock_id or _parent(path.parent) != parent_id:
                raise MediaTransactionError("mutex/parent changed during acquisition")
        yield
    finally:
        for mutex in reversed(held):
            mutex.release()


@dataclass
class PreparedOperation:
    source: Path
    target: Path
    replace_source: bool
    suppress_srt: bool
    directory: Path
    source_fingerprint: dict
    parents: dict
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: str = "prepared"
    candidate: str = ""
    candidate_fingerprint: dict | None = None
    verification: dict | None = None
    owned_srt: str = ""
    srt_fingerprint: dict | None = None
    embedded: bool = False
    installed: str = ""
    fallback: str = ""
    sidecar_target: str = ""
    publication: str = ""
    publication_fingerprint: dict | None = None
    publish_destination: str = ""
    backup_verified: bool = False
    warnings: list[str] = field(default_factory=list)
    cancel: Callable | None = field(default=None, repr=False)
    result: Any = field(default=None, repr=False)

    @property
    def journal(self):
        return self.directory / "journal.json"

    @property
    def backup(self):
        return self.directory / "original.backup"

    @property
    def captured(self):
        return self.directory / "original.captured"

    def owns(self, path):
        path = _path(path)
        return path.parent == self.directory and _parent(self.directory) == self.parents[str(self.directory)]

    def persist(self, phase):
        self.phase = phase
        # asdict deep-copies callbacks, including cancellation Events/locks.
        # Runtime capabilities are deliberately never part of a journal.
        data = {f.name: getattr(self, f.name) for f in fields(self) if f.name not in {"cancel", "result"}}
        data = json.loads(json.dumps(data, default=str))
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".journal-")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.journal)  # only our private journal, never public media
        sync_dir(self.directory)


@dataclass
class MediaCommitResult:
    output_path: str
    srt_path: str = ""
    backup_path: str = ""
    journal_path: str = ""
    checkpoint: str = ""
    warnings: list[str] = field(default_factory=list)
    sidecar_suppressed: bool = False
    needs_attention: bool = False


def _pending(op):
    for journal in op.source.parent.glob(".vidlift-media-*/journal.json"):
        if journal == op.journal:
            continue
        try:
            data = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise MediaTransactionError(f"unreadable media journal requires inspection: {journal}")
        if data.get("phase") in {"backup_ready", "commit_intent", "source_captured", "target_published", "needs_attention"} and ({data.get("source"), data.get("target")} & {str(op.source), str(op.target)}):
            raise MediaTransactionError(f"incomplete media transaction requires inspection: {journal}")


def prepare_media_operation(source_path, target_path, *, replace_source, suppress_srt, cancel=None):
    observe_cancel(cancel)
    source, target = _path(source_path), _path(target_path)
    parents = {str(p): _parent(p) for p in {source.parent, target.parent}}
    source_fp = fingerprint(source, cancel)
    if target != source and os.path.lexists(target) and not replace_source:
        raise MediaTransactionError(f"output already exists (never overwritten): {target}")
    if target == source and not replace_source:
        raise MediaTransactionError("source/output alias: encoding may not overwrite input")
    if replace_source and source_fp["links"] != 1:
        raise MediaTransactionError("replacement refused: hardlinked source ownership is ambiguous")
    if replace_source and os.name not in {"posix", "nt"}:
        raise MediaTransactionError("replacement refused: unsupported platform")
    directory = Path(tempfile.mkdtemp(dir=source.parent, prefix=".vidlift-media-"))
    parents[str(directory)] = _parent(directory)
    op = PreparedOperation(source, target, bool(replace_source), bool(suppress_srt), directory, source_fp, parents)
    op.cancel = cancel
    # Prove local hard-link publication before work; only POSIX also syncs
    # directory entries. On Windows, prove capture identity on disposable
    # bytes instead of disabling the released replacement feature outright.
    test = directory / "capability"
    test.write_bytes(b"capability")
    sync_file(test)
    if replace_source and os.name == "nt":
        before = fingerprint(test, cancel)
        captured = directory / "capability-captured"
        os.rename(test, captured)
        after = fingerprint(captured, cancel)
        if after["identity"][:4] != before["identity"][:4] or after["sha256"] != before["sha256"]:
            raise MediaTransactionError("replacement refused: local capture identity check failed")
        test = captured
        op.warnings.append(
            "Windows replacement: directory-entry power-loss durability is not guaranteed; "
            "original backup, captured source and verified candidate are retained for recovery"
        )
    publish_new(test, directory / "capability-published")
    (directory / "capability-published").unlink()
    sync_dir(directory)
    with _locks([source, target], cancel):
        _pending(op)
        op.persist("prepared")
        sync_dir(source.parent)
    return op


def record_verified_output(op, path, *, verification, owned_srt=None):
    """Run independent verification and bind its evidence to these exact bytes.

    verification is a callable returning structured probe/decode evidence, never
    a caller's bare bool. Default standalone producers keep their public APIs.
    """
    path = _path(path)
    if path == op.source or (path.exists() and os.path.samefile(path, op.source)):
        raise MediaTransactionError("source/output alias is not a verified candidate")
    before = fingerprint(path, op.cancel)
    observe_cancel(op.cancel)
    if not callable(verification):
        raise MediaTransactionError("independent structured verification required")
    evidence = verification(str(path))
    if is_dataclass(evidence) and not isinstance(evidence, type):
        evidence = asdict(evidence)
    if not isinstance(evidence, dict) or not evidence or evidence.get("ok") is not True:
        raise MediaTransactionError("output verification failed or returned no evidence")
    observe_cancel(op.cancel)
    if fingerprint(path, op.cancel) != before:
        raise MediaTransactionError("verified candidate changed during verification")
    sync_file(path)
    op.candidate, op.candidate_fingerprint, op.verification = str(path), before, evidence
    if owned_srt:
        if not op.owns(owned_srt):
            raise MediaTransactionError("subtitle is not an owned private file")
        op.owned_srt = str(_path(owned_srt))
        op.srt_fingerprint = fingerprint(owned_srt)
    op.persist("output_verified")


def _same_content(path, fp):
    try:
        current = fingerprint(path)
        return current["sha256"] == fp["sha256"] and current["identity"][2] == fp["identity"][2]
    except OSError:
        return False


def _copy_checked(source, destination, expected, cancel=None):
    # Independent copy, not a hardlink. Exclusive open, bounded cancel beats.
    with open(source, "rb") as src, open(destination, "xb") as dst:
        while block := src.read(1024 * 1024):
            observe_cancel(cancel)
            dst.write(block)
        dst.flush()
        os.fsync(dst.fileno())
    if not _same_content(destination, expected):
        raise MediaTransactionError(f"copy hash mismatch: {destination}")
    sync_dir(Path(destination).parent)


def copy_owned_input(op, source, destination):
    if not op.owns(destination) or _path(source) == _path(destination):
        raise MediaTransactionError("adopted subtitle/source alias refused")
    before = fingerprint(source, op.cancel)
    _copy_checked(source, destination, before, op.cancel)
    if fingerprint(source, op.cancel) != before:
        raise MediaTransactionError("adopted subtitle changed during copy")


def _validate(op):
    for path, identity in op.parents.items():
        if _parent(path) != identity:
            raise MediaTransactionError(f"parent identity changed: {path}")
    if fingerprint(op.source) != op.source_fingerprint:
        raise MediaTransactionError("source identity/content changed since preparation")
    if fingerprint(op.candidate) != op.candidate_fingerprint:
        raise MediaTransactionError("verified candidate identity/content changed")


def _restore_absent(op):
    if not os.path.lexists(op.source) and os.path.lexists(op.captured):
        try:
            os.link(op.captured, op.source, follow_symlinks=False)
            sync_dir(op.source.parent)
        except OSError as exc:
            op.warnings.append(f"original capture retained at {op.captured}; restore failed: {exc}")


def _capture(op):
    # Destination is absent inside our private directory, under held locks.
    if os.path.lexists(op.captured):
        raise MediaTransactionError("capture slot unexpectedly occupied; retained")
    os.rename(op.source, op.captured)
    sync_dir(op.source.parent)
    sync_dir(op.directory)
    captured = fingerprint(op.captured)
    # Rename changes ctime; compare object identity, mtime, length and hash.
    expected = op.source_fingerprint
    if captured["identity"][:4] != expected["identity"][:4] or captured["sha256"] != expected["sha256"]:
        _restore_absent(op)
        raise MediaTransactionError(f"unexpected source captured; retained/restored at {op.captured}")
    op.persist("source_captured")


def _stage_publication(op, destination, cancel=None):
    temp = op.directory / ("publish-" + uuid.uuid4().hex + ".mp4")
    if temp.parent.stat().st_dev != Path(destination).parent.stat().st_dev:
        stage = Path(tempfile.mkdtemp(dir=Path(destination).parent, prefix=".vidlift-media-output-"))
        op.parents[str(stage)] = _parent(stage)
        temp = stage / temp.name
    _copy_checked(op.candidate, temp, op.candidate_fingerprint, cancel)
    return temp


def _publish_copy(op, destination):
    temp = Path(op.publication) if op.publication else None
    if temp is None or not temp.is_file() or temp.parent.stat().st_dev != Path(destination).parent.stat().st_dev:
        temp = _stage_publication(op, destination)
    if not _same_content(temp, op.candidate_fingerprint):
        raise MediaTransactionError("publication stage hash mismatch")
    op.publication = str(temp)
    op.publication_fingerprint = fingerprint(temp)
    op.publish_destination = str(destination)
    op.persist("commit_intent")
    publish_new(temp, destination)
    # Remember syscall success before any fallible read/fsync/journal update.
    op.installed = str(destination)
    if not _same_content(destination, op.candidate_fingerprint):
        raise MediaTransactionError("installed output changed after publication")
    sync_dir(Path(destination).parent)


def _reconcile_publication(op):
    if op.publish_destination and op.publication_fingerprint:
        try:
            fp = fingerprint(op.publish_destination)
            if fp["identity"][:4] == op.publication_fingerprint["identity"][:4] and fp["sha256"] == op.publication_fingerprint["sha256"]:
                op.installed = op.publish_destination
        except OSError:
            pass


def publish_sidecar(op, destination, *, embedded=False):
    if not op.owned_srt:
        return "", bool(op.suppress_srt and embedded)
    if fingerprint(op.owned_srt) != op.srt_fingerprint:
        raise MediaTransactionError("private subtitle identity changed")
    if op.suppress_srt and embedded:
        # Withhold the public name. Keep private recovery evidence, no unlink.
        return "", True
    destination = Path(destination)
    try:
        # Keep the private copy as evidence, even if durability reporting fails.
        os.link(op.owned_srt, destination, follow_symlinks=False)
        sync_file(destination)
        sync_dir(destination.parent)
        return str(destination), False
    except OSError as exc:
        op.warnings.append(f"sidecar publication refused; retained at {op.owned_srt}: {exc}")
        return op.owned_srt, False


def commit_media_operation(op, *, cancel=None, fallback_path=None, sidecar_path=None, retire_source=True):
    """Never retry an uncertain commit; report the verified retained artifact."""
    try:
        return _commit_media_operation(op, cancel=cancel, fallback_path=fallback_path,
                                       sidecar_path=sidecar_path, retire_source=retire_source)
    except OSError as exc:
        if not op.candidate_fingerprint or not _same_content(op.candidate, op.candidate_fingerprint):
            raise
        output = op.installed if op.installed and _same_content(op.installed, op.candidate_fingerprint) else op.candidate
        return MediaCommitResult(
            output_path=output, srt_path=op.owned_srt,
            backup_path=str(op.backup) if op.backup_verified else "",
            journal_path=str(op.journal), checkpoint=op.phase,
            warnings=[*op.warnings, f"media commit refused/uncertain; inspect {op.journal}; verified output retained at {output}: {exc}"],
            needs_attention=True,
        )


def _commit_media_operation(op, *, cancel=None, fallback_path=None, sidecar_path=None, retire_source=True):
    if not op.candidate_fingerprint or not op.verification or op.phase != "output_verified":
        raise MediaTransactionError("prepared verified operation required; all files retained")
    if fingerprint(op.candidate, cancel) != op.candidate_fingerprint:
        raise MediaTransactionError("verified candidate changed; refusing commit")
    observe_cancel(cancel)
    fallback_path = fallback_path or op.fallback
    sidecar_path = sidecar_path or op.sidecar_target
    replacing = op.replace_source and retire_source
    target = op.target if retire_source else _path(fallback_path)
    needs_attention = False
    with _locks([op.source, op.target, target], cancel):
        try:
            _pending(op)
            _validate(op)
            op.publication = str(_stage_publication(op, target, cancel))
            op.persist("output_verified")
            observe_cancel(cancel)
            if replacing:
                if op.source_fingerprint["links"] != 1:
                    raise MediaTransactionError("hardlinked source replacement refused")
                _copy_checked(op.source, op.backup, op.source_fingerprint, cancel)
                _validate(op)
                op.backup_verified = True
                op.persist("backup_ready")
                observe_cancel(cancel)
            op.persist("commit_intent")
            # From here cancellation is deferred to a safe, truthful checkpoint.
            if replacing and target == op.source:
                _capture(op)
                _publish_copy(op, target)
            else:
                _publish_copy(op, target)
                if replacing:
                    if fingerprint(op.source) != op.source_fingerprint:
                        raise MediaTransactionError("source changed before retirement; original name retained")
                    _capture(op)
            op.persist("target_published")
        except OSError as exc:
            _reconcile_publication(op)
            needs_attention = op.phase in {"backup_ready", "commit_intent", "source_captured", "target_published"} or bool(op.installed)
            op.warnings.append(f"WARNING replace-source/publication failed; retained recovery material: {exc}")
            _restore_absent(op)
            if not op.installed and fallback_path and _path(fallback_path) != op.source:
                try:
                    _publish_copy(op, fallback_path)
                except OSError as fallback_exc:
                    _reconcile_publication(op)
                    op.warnings.append(f"fallback retained privately at {op.candidate}: {fallback_exc}")
        # Never report a target as verified after an observed hash failure.
        accepted = bool(op.installed and _same_content(op.installed, op.candidate_fingerprint))
        output = op.installed if accepted else op.candidate
        try:
            srt, suppressed = publish_sidecar(op, sidecar_path or Path(output).with_suffix(".srt"), embedded=accepted and op.embedded)
        except OSError as exc:
            srt, suppressed = op.owned_srt, False
            needs_attention = True
            op.warnings.append(f"subtitle disposition uncertain; retained private location {srt}: {exc}")
        if cancel is not None and cancel():
            if accepted:
                op.warnings.append("late cancellation deferred: output already committed")
            else:
                observe_cancel(cancel)
        try:
            op.persist("needs_attention" if needs_attention or not accepted else "complete")
        except OSError as exc:
            needs_attention = True
            op.warnings.append(f"journal completion failed; inspect retained evidence: {exc}")
        if op.replace_source and op.backup_verified:
            capture_note = str(op.captured) if os.path.lexists(op.captured) else "not captured"
            op.warnings.append(f"original backup retained indefinitely at {op.backup} ({op.source_fingerprint['identity'][2]} bytes; capture: {capture_note}); verified candidate retained at {op.candidate}; journal: {op.journal}")
        return MediaCommitResult(output, srt, str(op.backup) if op.backup_verified else "", str(op.journal), op.phase, list(op.warnings), suppressed, needs_attention)


def inspect_recovery(journal_path):
    """Read-only reconciliation, including syscall-success/journal-failure gaps."""
    path = _path(journal_path)
    _regular(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    directory = path.parent
    report = {"journal_path": str(path), "checkpoint": data.get("phase"), "files": {}, "automatic_replay": False}
    for name, candidate in {"source": data.get("source"), "target": data.get("target"), "candidate": data.get("candidate"), "fallback": data.get("fallback"), "installed": data.get("installed"), "private_srt": data.get("owned_srt"), "public_srt": data.get("sidecar_target"), "backup": str(directory / "original.backup"), "captured": str(directory / "original.captured")}.items():
        if candidate:
            try:
                fp = fingerprint(candidate)
                fp["matches_original"] = fp["sha256"] == (data.get("source_fingerprint") or {}).get("sha256")
                fp["matches_output"] = fp["sha256"] == (data.get("candidate_fingerprint") or {}).get("sha256")
                fp["matches_subtitle"] = fp["sha256"] == (data.get("srt_fingerprint") or {}).get("sha256")
                report["files"][name] = {"path": candidate, **fp}
            except OSError as exc:
                report["files"][name] = {"path": candidate, "error": str(exc)}
    return report
