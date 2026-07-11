"""Egress sentinel tests with an injected connection sampler."""

from llm_os.audit import AuditLog
from llm_os.sentinel import EgressSentinel


def make_sentinel(tmp_path, batches):
    calls = iter(batches)
    audit = AuditLog(tmp_path)
    sentinel = EgressSentinel(audit, sampler=lambda: next(calls, []))
    return sentinel, audit


def test_clean_samples_record_nothing(tmp_path):
    sentinel, audit = make_sentinel(tmp_path, [[], []])
    assert sentinel.check_once() == []
    assert sentinel.check_once() == []
    assert sentinel.violations == {}
    assert all(r["event"] != "egress_violation" for r in audit.tail(10))


def test_violation_is_audited_once(tmp_path):
    sentinel, audit = make_sentinel(
        tmp_path,
        [["34.36.133.15:443"], ["34.36.133.15:443"], []],
    )
    assert sentinel.check_once() == ["34.36.133.15:443"]  # new -> audited
    assert sentinel.check_once() == []                     # repeat -> not duplicated
    sentinel.check_once()

    events = [r for r in audit.tail(10) if r["event"] == "egress_violation"]
    assert len(events) == 1
    assert events[0]["remote"] == "34.36.133.15:443"
    assert audit.verify_chain()

    status = sentinel.status()
    assert status["samples"] == 3
    assert len(status["violations"]) == 1


def test_sampler_failure_never_crashes(tmp_path):
    audit = AuditLog(tmp_path)

    def broken():
        raise OSError("lsof unavailable")

    sentinel = EgressSentinel(audit, sampler=broken)
    assert sentinel.check_once() == []
    assert sentinel.violations == {}
