"""A single audit record larger than the tail-read window must not fork the
chain. `_last_hash_of` reads only the last few KB to find the previous
record's hash; a record bigger than that window was read as a truncated,
unparseable line, silently fell back to GENESIS, and forked the chain so
verification failed forever. Reachable in the DEFAULT redacted mode via any
large non-content tool result (a disk-inspector file list, a big MCP result).
"""

from llm_os import redact
from llm_os.audit import AuditLog


def test_record_over_the_tail_window_does_not_fork_the_chain(tmp_path):
    log = AuditLog(tmp_path, content_mode=redact.PLAINTEXT)
    log.append("tool_execution", {"tool": "a", "status": "success"})
    log.append("tool_execution", {"tool": "big", "status": "success",
                                   "result": {"blob": "X" * 12000}})  # > 8192
    log.append("tool_execution", {"tool": "c", "status": "success"})
    assert log.verify_chain() is True


def test_large_non_content_result_forks_nothing_in_default_mode(tmp_path):
    # Redaction shrinks CONTENT_KEYS, but a file list is not content — it
    # stays verbatim, so the fork is reachable without plaintext mode.
    log = AuditLog(tmp_path)  # default: redacted
    log.append("tool_execution", {
        "tool": "disk", "status": "success",
        "result": {"groups": [f"/very/long/path/name/file-{i:06d}.bin" for i in range(300)]},
    })
    log.append("tool_execution", {"tool": "next", "status": "success"})
    assert log.verify_chain() is True


def test_many_large_records_in_a_row_stay_linked(tmp_path):
    log = AuditLog(tmp_path, content_mode=redact.PLAINTEXT)
    for i in range(6):
        log.append("tool_execution", {"tool": f"t{i}", "status": "success",
                                       "result": {"blob": "Y" * 10000}})
    assert log.verify_chain() is True
