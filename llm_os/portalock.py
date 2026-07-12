"""Portable exclusive file locking — the audit chain's multi-process
safety rests on this, so the same contract must hold on every OS:
one writer per file at a time, blocking until the lock is granted.

POSIX gets flock (whole-file, blocks indefinitely). Windows has no
flock; msvcrt.locking takes a byte range, so both writers lock the
same first byte — equivalent serialization as long as every writer
goes through this module. msvcrt's blocking mode gives up after ~10
seconds, so it is retried until acquired.
"""

try:
    import fcntl

    def lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

except ImportError:  # pragma: no cover — Windows only
    import msvcrt

    def lock(handle) -> None:
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue  # LK_LOCK times out after ~10s of contention

    def unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
