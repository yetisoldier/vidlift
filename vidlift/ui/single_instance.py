"""GUI-only per-user/session single-instance IPC. Never signals a PID.

A persistent kernel-held startup mutex serialises probe/remove/listen so stale
socket recovery cannot remove an endpoint just created by another contender.
CLI callers deliberately never import this module.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
from pathlib import Path
import socket as native_socket
import stat
import struct
import sys
import time
from dataclasses import dataclass

from PyQt6.QtCore import QCoreApplication, QTimer, QStandardPaths, QObject, QSocketNotifier, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

from ..file_mutex import FileMutex

_LOG = logging.getLogger(__name__)
_ACK = b'{"ok":true}\n'
_ERROR = b'{"ok":false,"error":"invalid_command"}\n'
MAX_COMMAND_BYTES = 256


def _windows_identity() -> dict:
    from ctypes import wintypes
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    advapi = getattr(ctypes, "WinDLL")("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError("Cannot determine Windows user SID")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise OSError("Cannot read Windows token size")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise OSError("Cannot read Windows user token")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            raise OSError("Cannot format Windows user SID")
        try:
            sid = text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        session = wintypes.DWORD()
        if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            raise OSError("Cannot determine Windows session")
        return {"platform": "win32", "sid": sid, "session_id": session.value}
    finally:
        kernel.CloseHandle(token)


def endpoint_name(*, facts_fn=None) -> str:
    facts = facts_fn() if facts_fn else (_windows_identity() if sys.platform == "win32"
                                       else {"platform": "posix", "uid": os.getuid()})
    if facts["platform"] == "win32":
        return f"VidLift-{facts['sid']}-{facts['session_id']}"
    return f"VidLift-{facts['uid']}"


@dataclass(frozen=True)
class Endpoint:
    root: Path
    socket: Path
    uid: int
    identity: tuple


def resolve_posix_endpoint(facts, runtime_root_provider=None) -> Endpoint:
    """Resolve a private Linux UID namespace, never a Qt temporary path."""
    if facts.get("platform") != "linux" or facts.get("uid") != os.getuid():
        raise OSError("Unsupported POSIX instance namespace")
    uid = facts["uid"]
    root = Path(runtime_root_provider(uid) if runtime_root_provider else f"/run/user/{uid}")
    if not root.is_absolute() or '..' in root.parts:
        raise OSError("Invalid runtime root")
    current = Path('/')
    for part in root.parts[1:]:
        current /= part
        parent_st = os.lstat(current)
        if stat.S_ISLNK(parent_st.st_mode):
            raise OSError("Symlink in runtime root")
        if runtime_root_provider is None and current != root and (
                parent_st.st_uid != 0 or parent_st.st_mode & 0o022):
            raise OSError("Unsafe runtime root ancestor")
    st = os.stat(root, follow_symlinks=False)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or stat.S_IMODE(st.st_mode) != 0o700:
        raise OSError("Runtime root is not private")
    path = root / 'vidlift-gui-v3.sock'
    if len(os.fsencode(path)) >= 108:
        raise OSError("Runtime socket path too long")
    return Endpoint(root, path, uid, (st.st_dev, st.st_ino))


class InstanceLifetimeLease:
    """Persistent verified inode and kernel flock, held until explicit release."""
    def __init__(self, endpoint, name):
        self.endpoint = endpoint
        self.name = name
        self.fd = None

    def acquire(self, timeout):
        import fcntl
        rootfd = _open_root(self.endpoint)
        try:
            fd = os.open(self.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=rootfd)
            try:
                st = os.fstat(fd)
                pathst = os.stat(self.name, dir_fd=rootfd, follow_symlinks=False)
                if (not stat.S_ISREG(st.st_mode) or st.st_uid != self.endpoint.uid or
                    stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1 or
                    (st.st_dev, st.st_ino) != (pathst.st_dev, pathst.st_ino)):
                    raise OSError("Unsafe instance lock")
                deadline = time.monotonic() + max(0, timeout)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            return False
                        time.sleep(min(.01, max(0, deadline - time.monotonic())))
                pathst = os.stat(self.name, dir_fd=rootfd, follow_symlinks=False)
                if (st.st_dev, st.st_ino) != (pathst.st_dev, pathst.st_ino):
                    raise OSError("Instance lock replaced")
                self.fd = fd
                return True
            finally:
                if self.fd != fd:
                    os.close(fd)
        finally:
            os.close(rootfd)

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def verify(self):
        if self.fd is None:
            raise OSError("Instance lease not held")
        rootfd = _open_root(self.endpoint)
        try:
            st = os.fstat(self.fd)
            current = os.stat(self.name, dir_fd=rootfd, follow_symlinks=False)
            if (st.st_dev, st.st_ino) != (current.st_dev, current.st_ino) or st.st_nlink != 1:
                raise OSError("Instance lock identity changed")
        finally:
            os.close(rootfd)


def _open_root(endpoint):
    fd = os.open(endpoint.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    st = os.fstat(fd)
    if ((st.st_dev, st.st_ino) != endpoint.identity or st.st_uid != endpoint.uid or
            stat.S_IMODE(st.st_mode) != 0o700):
        os.close(fd)
        raise OSError("Runtime root changed")
    return fd


class _PosixServer(QObject):
    """Own the listener ourselves: QLocalServer.close unlinks replaced paths."""
    newConnection = pyqtSignal()

    def __init__(self, listener, parent):
        super().__init__(parent)
        self.listener = listener
        self.listener.setblocking(False)
        self.pending = []
        self.notifier = QSocketNotifier(listener.fileno(), QSocketNotifier.Type.Read, self)
        self.notifier.activated.connect(self._ready)

    def _ready(self, *_):
        while True:
            try:
                conn, _ = self.listener.accept()
            except BlockingIOError:
                break
            client = QLocalSocket(self)
            fd = conn.detach()
            if not client.setSocketDescriptor(fd):
                os.close(fd)
                client.deleteLater()
                continue
            self.pending.append(client)
        if self.pending:
            self.newConnection.emit()

    def hasPendingConnections(self):
        return bool(self.pending)

    def nextPendingConnection(self):
        return self.pending.pop(0) if self.pending else None

    def isListening(self):
        return self.listener.fileno() >= 0

    def close(self):
        self.notifier.setEnabled(False)
        self.listener.close()
        for client in self.pending:
            client.abort()
            client.deleteLater()
        self.pending.clear()


class SingleInstanceGuard:
    def __init__(self, *, name=None, lock_dir=None, timeout_ms=2000, on_raise=None,
                 runtime_root_provider=None):
        self.name = name
        # Windows named pipes ignore TEMP, so arbitration must not depend on
        # the launching shell's choice of temporary directory either.
        self.lock_dir = Path(lock_dir) if lock_dir is not None else Path(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
        ) / "VidLift" / "instance-arbitration"
        self.timeout_ms = timeout_ms
        self.on_raise = on_raise
        self.server = None
        self.status = "new"
        self.error = None
        self._clients = set()
        self._runtime_root_provider = runtime_root_provider
        self._endpoint = None
        self._lease = None
        self._socket_identity = None
        self._socket_fd = None

    def _connect(self):
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        connected = socket.waitForConnected(max(1, self.timeout_ms // 2))
        return socket, connected

    def _raise_existing(self, socket):
        if sys.platform == 'linux':
            peer = native_socket.fromfd(int(socket.socketDescriptor()), native_socket.AF_UNIX,
                                        native_socket.SOCK_STREAM)
            try:
                _, uid, _ = struct.unpack('3i', peer.getsockopt(native_socket.SOL_SOCKET,
                                                                native_socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    raise OSError("IPC peer UID mismatch")
            finally:
                peer.close()
        socket.write(b'{"cmd":"raise"}\n')
        socket.flush()
        deadline = time.monotonic() + self.timeout_ms / 1000
        reply = bytearray()
        while time.monotonic() < deadline:
            # Also permits deterministic same-process offscreen guard tests.
            QCoreApplication.processEvents()
            reply.extend(socket.readAll().data())
            if b"\n" in reply:
                return bytes(reply) == _ACK
            if socket.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                break
            socket.waitForReadyRead(10)
        return False

    def start(self, app) -> bool:
        """True owner; False signalled or error (see status/error). Never rivals."""
        if sys.platform != "win32":
            return self._start_posix(app)
        mutex = None
        try:
            self.name = self.name or endpoint_name()
            digest = hashlib.sha256(self.name.encode()).hexdigest()[:24]
            mutex = FileMutex(self.lock_dir / f"vidlift-instance-{digest}.mutex")
            if not mutex.acquire(self.timeout_ms / 1000):
                raise OSError("Single-instance startup arbitration timed out")
            absent = {QLocalSocket.LocalSocketError.ConnectionRefusedError,
                      QLocalSocket.LocalSocketError.ServerNotFoundError}
            for _ in range(2):
                socket, connected = self._connect()
                try:
                    if connected:
                        if not self._raise_existing(socket):
                            raise OSError("Existing instance did not acknowledge activation")
                        self.status = "signalled"
                        return False
                    if socket.error() not in absent:
                        raise OSError("Cannot establish local IPC ownership")
                finally:
                    socket.abort()
            # Two definite no-listener results, while all new contenders are
            # excluded by the startup mutex. Never remove on permission/timeout.
            QLocalServer.removeServer(self.name)
            server = QLocalServer(app)
            server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
            if not server.listen(self.name):
                server.deleteLater()
                raise OSError("Cannot claim local IPC endpoint")
            self.server = server
            server.newConnection.connect(self._accept)
            app.aboutToQuit.connect(self.close)
            self.status = "owner"
            return True
        except Exception as exc:
            self.status = "error"
            self.error = ("Another VidLift may already be running; this instance will not start. "
                          "Close the other instance or retry after checking its status. "
                          f"({type(exc).__name__})")
            _LOG.warning(self.error)
            return False
        finally:
            if mutex is not None:
                mutex.release()

    def _start_posix(self, app):
        startup = None
        lease = None
        endpoint_fd = None
        try:
            # Explicit lock_dir is retained as a fixture-only compatibility injection.
            provider = self._runtime_root_provider
            if provider is None and self.lock_dir is not None and self.name is not None:
                provider = lambda uid: self.lock_dir
            self._endpoint = resolve_posix_endpoint(
                {"platform": sys.platform, "uid": os.getuid()}, provider)
            self.name = str(self._endpoint.socket)
            startup = InstanceLifetimeLease(self._endpoint, 'vidlift-gui-v3.start')
            if not startup.acquire(self.timeout_ms / 1000):
                raise OSError("Startup lock timed out")
            lease = InstanceLifetimeLease(self._endpoint, 'vidlift-gui-v3.lease')
            held = lease.acquire(0)
            rootfd = _open_root(self._endpoint)
            try:
                try:
                    endpoint_fd = os.open(self._endpoint.socket.name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                                          dir_fd=rootfd)
                    initial = os.fstat(endpoint_fd)
                except FileNotFoundError:
                    initial = None
            finally:
                os.close(rootfd)
            sock, connected = self._connect()
            try:
                if not held:
                    if not connected or not self._raise_existing(sock):
                        raise OSError("Lease holder did not acknowledge activation")
                    self.status = 'signalled'
                    return False
                if connected:
                    raise OSError("Unleased IPC listener; ownership indeterminate")
                error = sock.error()
            finally:
                sock.abort()
            if error not in (QLocalSocket.LocalSocketError.ConnectionRefusedError,
                              QLocalSocket.LocalSocketError.ServerNotFoundError):
                raise OSError("Indeterminate IPC")
            rootfd = _open_root(self._endpoint)
            try:
                try:
                    before = os.stat(self._endpoint.socket.name, dir_fd=rootfd, follow_symlinks=False)
                except FileNotFoundError:
                    before = None
                if before is not None:
                    if (error != QLocalSocket.LocalSocketError.ConnectionRefusedError or
                        initial is None or (initial.st_dev, initial.st_ino) != (before.st_dev, before.st_ino) or
                        not stat.S_ISSOCK(before.st_mode) or before.st_uid != self._endpoint.uid):
                        raise OSError("Unsafe or indeterminate existing endpoint")
                    probe, connected = self._connect()
                    try:
                        if connected or probe.error() != QLocalSocket.LocalSocketError.ConnectionRefusedError:
                            raise OSError("Endpoint is not demonstrably stale")
                    finally:
                        probe.abort()
                    after = os.stat(self._endpoint.socket.name, dir_fd=rootfd, follow_symlinks=False)
                    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                        raise OSError("Endpoint changed during probe")
                    checkfd = _open_root(self._endpoint)
                    os.close(checkfd)
                    startup.verify()
                    lease.verify()
                    os.unlink(self._endpoint.socket.name, dir_fd=rootfd)
                elif initial is not None:
                    raise OSError("Endpoint disappeared during probe")
                startup.verify()
                lease.verify()
                listener = native_socket.socket(native_socket.AF_UNIX, native_socket.SOCK_STREAM)
                try:
                    listener.bind(self.name)
                    listener.listen(30)
                    st = os.stat(self._endpoint.socket.name, dir_fd=rootfd, follow_symlinks=False)
                    self._socket_identity = (st.st_dev, st.st_ino)
                    self._socket_fd = os.open(self._endpoint.socket.name,
                                              os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=rootfd)
                    pinned = os.fstat(self._socket_fd)
                    if (pinned.st_dev, pinned.st_ino) != self._socket_identity:
                        raise OSError("New endpoint changed")
                    server = _PosixServer(listener, app)
                    listener = None
                finally:
                    if listener is not None:
                        listener.close()
            finally:
                os.close(rootfd)
            self.server = server
            self._lease, lease = lease, None
            server.newConnection.connect(self._accept)
            app.aboutToQuit.connect(self.close)
            self.status = 'owner'
            legacy = QLocalSocket()
            legacy.connectToServer(endpoint_name())
            if legacy.waitForConnected(min(100, self.timeout_ms)):
                _LOG.warning("A legacy VidLift endpoint is reachable; close the older instance.")
            legacy.abort()
            _LOG.warning("Legacy VidLift instances may not cooperate; mixed-version exclusion is unverified. Close older instances before starting.")
            return True
        except Exception as exc:
            self.status = 'error'
            self.error = ("Another VidLift may already be running; this instance will not start. "
                          f"Close older instances and check local IPC ({type(exc).__name__}: {exc}). "
                          "Mixed-version exclusion cannot be verified.")
            _LOG.warning(self.error)
            return False
        finally:
            if endpoint_fd is not None:
                os.close(endpoint_fd)
            if lease is not None:
                lease.release()
                if self._socket_fd is not None:
                    os.close(self._socket_fd)
                    self._socket_fd = None
            if startup is not None:
                startup.release()

    def _accept(self):
        while self.server is not None and self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is None:
                continue
            socket.setReadBufferSize(MAX_COMMAND_BYTES + 1)
            self._clients.add(socket)
            buffer = bytearray()
            timer = QTimer(socket)
            timer.setSingleShot(True)
            def finish(ok, socket=socket, timer=timer):
                if socket not in self._clients:
                    return
                timer.stop()
                socket.write(_ACK if ok else _ERROR)
                socket.flush()
                socket.disconnectFromServer()
                self._clients.discard(socket)
                if not ok:
                    _LOG.warning("Rejected invalid single-instance command")
            def read(socket=socket, buffer=buffer, finish=finish):
                if socket not in self._clients:
                    return
                buffer.extend(socket.readAll().data())
                if len(buffer) > MAX_COMMAND_BYTES:
                    finish(False)
                    return
                if b"\n" not in buffer:
                    return
                try:
                    # Reject duplicate/extra keys as well as unknown commands.
                    pairs = json.loads(bytes(buffer).decode("utf-8"), object_pairs_hook=list)
                    valid = pairs == [("cmd", "raise")]
                except (ValueError, UnicodeError):
                    valid = False
                if valid:
                    try:
                        self.handle_raise()
                    except Exception:
                        _LOG.warning("Single-instance window activation failed")
                        valid = False
                finish(valid)
            socket.readyRead.connect(read)
            socket.disconnected.connect(lambda socket=socket: self._clients.discard(socket))
            socket.disconnected.connect(socket.deleteLater)
            timer.timeout.connect(lambda finish=finish: finish(False))
            timer.start(self.timeout_ms)
            read()

    def handle_raise(self):
        if self.on_raise is not None:
            self.on_raise()

    def close(self):
        for socket in list(self._clients):
            socket.abort()
        self._clients.clear()
        if self.server is not None:
            self.server.close()
            self.server.deleteLater()
            self.server = None
        if self._lease is not None:
            startup = InstanceLifetimeLease(self._endpoint, 'vidlift-gui-v3.start')
            try:
                if startup.acquire(self.timeout_ms / 1000):
                    rootfd = _open_root(self._endpoint)
                    try:
                        try:
                            st = os.stat(self._endpoint.socket.name, dir_fd=rootfd, follow_symlinks=False)
                            if (stat.S_ISSOCK(st.st_mode) and st.st_uid == self._endpoint.uid and
                                    (st.st_dev, st.st_ino) == self._socket_identity):
                                startup.verify()
                                self._lease.verify()
                                os.unlink(self._endpoint.socket.name, dir_fd=rootfd)
                        except FileNotFoundError:
                            pass
                    finally:
                        os.close(rootfd)
            except OSError:
                _LOG.warning("Instance endpoint cleanup skipped", exc_info=True)
            finally:
                startup.release()
                self._lease.release()
                self._lease = None
                if self._socket_fd is not None:
                    os.close(self._socket_fd)
                    self._socket_fd = None
