"""The launcher surfaces the approval token on the operator's terminal.

The kernel writes the token to a 0600 file (not the world-readable log, where
a local process could read it and self-approve). The launcher reads that file
and prints the token to the operator's own terminal, so a user running
scripts/launch.py sees the token they are then asked for.
"""

from scripts.launch import _approval_token_from_file


def test_token_is_read_from_the_file(tmp_path):
    token_file = tmp_path / ".approval_token"
    token_file.write_text("4cf55a15deadbeef", encoding="utf-8")
    assert _approval_token_from_file(token_file) == "4cf55a15deadbeef"


def test_missing_file_returns_empty(tmp_path):
    assert _approval_token_from_file(tmp_path / "nope") == ""


def test_the_token_is_written_to_an_owner_only_file(tmp_path, monkeypatch):
    # The security property: the token lands in a dedicated file, never in the
    # kernel log a second user could read. The 0600 bit is a POSIX concept
    # (Windows uses ACLs), so the mode assertion is POSIX-only.
    import os
    import stat

    from llm_os import api, config

    monkeypatch.setattr(config, "AUDIT_DIR", tmp_path)
    monkeypatch.setattr(api, "APPROVAL_TOKEN_FILE", tmp_path / ".approval_token")
    api._write_approval_token("s3cr3t-token-value")

    written = tmp_path / ".approval_token"
    assert written.read_text() == "s3cr3t-token-value"
    if os.name == "posix":
        mode = stat.S_IMODE(written.stat().st_mode)
        assert mode == 0o600, f"token file is {oct(mode)}, expected 0o600"
