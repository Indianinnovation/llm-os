"""Portable locking: the audit chain must serialize writers on every OS.
POSIX flock is exercised for real by the multi-process audit tests; here
we additionally prove the Windows fallback wires up correctly by
simulating a platform without fcntl."""

import importlib
import sys
import types

import llm_os.portalock as portalock
from llm_os.audit import AuditLog


def test_posix_lock_roundtrip(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("")
    with path.open("r+") as handle:
        portalock.lock(handle)
        portalock.unlock(handle)  # no deadlock, no error


def test_audit_append_goes_through_portalock(tmp_path, monkeypatch):
    order = []
    real_lock, real_unlock = portalock.lock, portalock.unlock
    monkeypatch.setattr(
        portalock, "lock", lambda h: (order.append("lock"), real_lock(h))
    )
    monkeypatch.setattr(
        portalock, "unlock", lambda h: (order.append("unlock"), real_unlock(h))
    )
    log = AuditLog(tmp_path)
    log.append("test_event", {"k": "v"})
    assert order == ["lock", "unlock"]


def test_windows_fallback_locks_first_byte(tmp_path, monkeypatch):
    calls = []
    fake_msvcrt = types.SimpleNamespace(
        LK_LOCK="LK_LOCK",
        LK_UNLCK="LK_UNLCK",
        locking=lambda fd, mode, nbytes: calls.append((mode, nbytes)),
    )
    # None in sys.modules makes `import fcntl` raise ImportError, which
    # is exactly what a real Windows interpreter does.
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    windows_lock = importlib.reload(portalock)
    try:
        path = tmp_path / "f.txt"
        path.write_text("x")
        with path.open("r+") as handle:
            windows_lock.lock(handle)
            handle.seek(0, 2)
            handle.write("y")
            windows_lock.unlock(handle)
        assert calls == [("LK_LOCK", 1), ("LK_UNLCK", 1)]
        assert path.read_text() == "xy"
    finally:
        monkeypatch.undo()
        importlib.reload(portalock)  # restore the real POSIX branch
