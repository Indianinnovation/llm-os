"""The audit chain must stay linear when several writers share a log.

This is the bug that bit us in production: an overlapping kernel restart
meant two processes appended against their own in-memory idea of the
chain tip, forking it — and a forked chain reports as TAMPERED forever.
"""

import multiprocessing
import threading

from llm_os.audit import AuditLog


def _writer(directory, count, tag):
    log = AuditLog(directory)
    for i in range(count):
        log.append("tool_execution", {"tool": tag, "i": i})


def test_two_processes_cannot_fork_the_chain(tmp_path):
    procs = [
        multiprocessing.Process(target=_writer, args=(tmp_path, 15, f"proc{n}"))
        for n in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)

    log = AuditLog(tmp_path)
    records = log.tail(1000)
    assert len(records) == 30, "every append must survive"
    assert log.verify_chain(), "chain must stay linear across processes"

    # And the links really are sequential, not just individually valid.
    for previous, current in zip(records, records[1:]):
        assert current["prev_hash"] == previous["hash"]


def test_threads_cannot_fork_the_chain(tmp_path):
    log = AuditLog(tmp_path)
    threads = [
        threading.Thread(target=lambda t=t: [log.append("chat", {"t": t, "i": i})
                                             for i in range(10)])
        for t in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)

    assert len(log.tail(1000)) == 40
    assert log.verify_chain()


def test_a_second_instance_continues_the_same_chain(tmp_path):
    """A restart must extend the existing chain, not start a new fork."""
    first = AuditLog(tmp_path)
    first.append("chat", {"n": 1})

    second = AuditLog(tmp_path)          # a fresh process/instance
    second.append("chat", {"n": 2})

    records = second.tail(10)
    assert records[1]["prev_hash"] == records[0]["hash"]
    assert second.verify_chain()
