"""One definition of 'is this observed TCP peer a loopback address?'.

The zero-egress checks (sentinel + preflight) each carried their own copy of
this predicate; tightening one silently left the other permissive. They now
share sentinel.is_loopback_host. (api._is_local is deliberately separate — it
guards request Host/Origin with stricter, exact-hostname semantics; the
standalone verify_airplane_mode script keeps its own copy on purpose.)
"""

import pytest

from llm_os.sentinel import is_loopback_host


@pytest.mark.parametrize("host", ["127.0.0.1", "127.5.5.5", "::1", "localhost"])
def test_loopback_addresses_are_local(host):
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["8.8.8.8", "10.0.0.1", "evil.com", "", "0.0.0.0"])
def test_non_loopback_addresses_are_not_local(host):
    assert is_loopback_host(host) is False
