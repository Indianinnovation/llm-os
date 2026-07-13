"""The egress sentinel must not report 'clean' when it cannot actually look.

On a host without pgrep/lsof (minimal Linux, Windows), the sampler raises
FileNotFoundError. Swallowing that as 'no remotes' means the flagship
zero-egress guarantee silently becomes a no-op: a leaking component would
show a clean /health and an empty audit chain. Missing tooling must read as
UNAVAILABLE, not as SAFE.
"""

from llm_os import sentinel
from llm_os.audit import AuditLog


def _sentinel(tmp_path, sampler):
    return sentinel.EgressSentinel(AuditLog(tmp_path), sampler=sampler)


def test_missing_tooling_reports_unavailable_not_clean(tmp_path):
    def no_lsof():
        raise FileNotFoundError("lsof")
    s = _sentinel(tmp_path, no_lsof)
    s.check_once()
    st = s.status()
    assert st["available"] is False
    assert st["violations"] == []           # but NOT reported as safe…
    assert "unavailable" in st.get("reason", "").lower()


def test_unavailability_is_written_to_the_audit_chain_once(tmp_path):
    def no_lsof():
        raise FileNotFoundError("lsof")
    audit = AuditLog(tmp_path)
    s = sentinel.EgressSentinel(audit, sampler=no_lsof)
    s.check_once(); s.check_once(); s.check_once()
    events = [r["event"] for r in audit.tail(20)]
    assert events.count("egress_monitoring_unavailable") == 1   # not once per sample


def test_working_sampler_is_available(tmp_path):
    s = _sentinel(tmp_path, lambda: [])
    s.check_once()
    assert s.status()["available"] is True


def test_a_real_violation_is_still_caught(tmp_path):
    s = _sentinel(tmp_path, lambda: ["8.8.8.8:443"])
    new = s.check_once()
    assert new == ["8.8.8.8:443"]
    assert s.status()["available"] is True


def test_monitoring_available_detects_missing_binary(monkeypatch):
    monkeypatch.setattr(sentinel.shutil, "which", lambda name: None)
    assert sentinel.monitoring_available() is False
    monkeypatch.setattr(sentinel.shutil, "which", lambda name: "/usr/bin/" + name)
    assert sentinel.monitoring_available() is True
