"""Read endpoints must clamp their count params.

/audit?n= and /memory?limit= were unbounded and accepted negatives. A
negative slices the wrong window, and an unbounded n makes a page render
read and re-hash the whole (unrotated, growing) audit file — a local DoS.
"""

from tests.test_console_api import client  # noqa: F401  (fixture)


def test_audit_rejects_negative_n(client):
    assert client.get("/audit?n=-5").status_code == 422


def test_audit_rejects_absurd_n(client):
    assert client.get("/audit?n=100000").status_code == 422


def test_audit_accepts_a_sane_n(client):
    assert client.get("/audit?n=10").status_code == 200


def test_memory_rejects_negative_limit(client):
    assert client.get("/memory?limit=-1").status_code == 422


def test_memory_accepts_a_sane_limit(client):
    assert client.get("/memory?limit=25").status_code == 200
