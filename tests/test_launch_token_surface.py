"""The launcher must surface the approval token on the operator's terminal.

The kernel prints the token to its stdout, which the launcher redirects to
.llmos_kernel.log — so without this, a user running scripts/launch.py never
sees the token they are then asked for. The launcher lifts it back out of the
log and prints it.
"""

from scripts.launch import _approval_token_from_log


def test_token_is_lifted_from_the_log(tmp_path):
    log = tmp_path / "kernel.log"
    log.write_text(
        "starting…\n"
        "  🔑 Approval token for this session: 4cf55a15\n"
        "     Enter it in the console…\n"
    )
    assert _approval_token_from_log(log) == "4cf55a15"


def test_no_token_line_returns_empty(tmp_path):
    log = tmp_path / "kernel.log"
    log.write_text("starting…\nkernel ok\n")
    assert _approval_token_from_log(log) == ""


def test_missing_log_returns_empty(tmp_path):
    assert _approval_token_from_log(tmp_path / "nope.log") == ""
