"""Scheduled agents: cadence, due-ness, runs, and the safety property."""

from datetime import datetime

from llm_os.approvals import ApprovalStore
from llm_os.audit import AuditLog
from llm_os.kernel import Kernel
from llm_os.scheduler import Scheduler, ScheduleStore, compute_next_run
from llm_os.tools import default_registry
from tests.test_kernel import FakeClient, text_response, tool_call_response

# ── cadence ─────────────────────────────────────────────────────────────────

def test_every_minutes_next_run():
    after = datetime(2026, 7, 12, 9, 0, 0)
    nxt = compute_next_run({"every_minutes": 30}, after)
    assert nxt == "2026-07-12 09:30:00"


def test_daily_at_rolls_to_tomorrow_once_past():
    after = datetime(2026, 7, 12, 9, 0, 0)
    assert compute_next_run({"daily_at": "18:00"}, after) == "2026-07-12 18:00:00"
    assert compute_next_run({"daily_at": "08:00"}, after) == "2026-07-13 08:00:00"


def test_no_cadence_never_self_runs():
    assert compute_next_run({}, datetime(2026, 7, 12, 9, 0)) == ""


# ── store ───────────────────────────────────────────────────────────────────

def test_due_only_returns_enabled_and_ripe_jobs(tmp_path):
    store = ScheduleStore(tmp_path / "schedules.json")
    ripe = store.create("ripe", "do it", every_minutes=1)
    store.update(ripe["id"], next_run="2000-01-01 00:00:00")

    future = store.create("future", "later", every_minutes=60)
    paused = store.create("paused", "never", every_minutes=1)
    store.update(paused["id"], next_run="2000-01-01 00:00:00")
    store.update(paused["id"], enabled=False)

    due_ids = {s["id"] for s in store.due()}
    assert ripe["id"] in due_ids
    assert future["id"] not in due_ids
    assert paused["id"] not in due_ids       # disabled jobs never fire


def test_schedules_persist_across_restart(tmp_path):
    path = tmp_path / "schedules.json"
    job = ScheduleStore(path).create("nightly", "report", daily_at="08:00")
    reloaded = ScheduleStore(path).get(job["id"])   # a fresh process
    assert reloaded["name"] == "nightly"
    assert reloaded["daily_at"] == "08:00"


# ── running ─────────────────────────────────────────────────────────────────

def _scheduler(tmp_path, responses, gated=()):
    registry = default_registry()
    registry.require_approval(*gated)
    audit = AuditLog(tmp_path)
    kernel = Kernel(
        registry=registry,
        client=FakeClient(responses),
        model="fake",
        audit=audit,
        approvals=ApprovalStore(tmp_path / "approvals.json"),
    )
    store = ScheduleStore(tmp_path / "schedules.json")
    return Scheduler(store, kernel, audit), store, kernel


def test_a_run_goes_through_the_kernel_and_is_audited(tmp_path):
    scheduler, store, kernel = _scheduler(
        tmp_path,
        [tool_call_response("calculator", {"expression": "2+2"}), text_response("4.")],
    )
    job = store.create("math", "What is 2+2?", every_minutes=60)

    run = scheduler.run_job(job["id"], trigger="schedule")
    assert run["ok"] and run["tools"] == ["calculator"]
    assert run["reply"] == "4."

    events = [r["event"] for r in kernel.audit.tail(20)]
    assert "scheduled_run_started" in events
    assert "scheduled_run_finished" in events

    saved = store.get(job["id"])
    assert len(saved["runs"]) == 1
    assert saved["last_run"] == run["ts"]
    assert saved["next_run"] > run["ts"]      # rescheduled for the next cycle


def test_unattended_job_cannot_self_approve_a_gated_tool(tmp_path, monkeypatch):
    """The safety property: a 3am job that wants to write leaves a request
    for a human — it does not authorize itself."""
    from llm_os import config

    monkeypatch.setattr(config, "SCRATCHPAD_DIR", tmp_path / "scratch")
    scheduler, store, kernel = _scheduler(
        tmp_path,
        [
            tool_call_response("write_markdown",
                               {"filename": "nightly", "title": "T", "content": "c"}),
            text_response("It needs approval."),
        ],
        gated=("write_markdown",),
    )
    job = store.create("nightly", "Write the nightly report", daily_at="03:00")

    run = scheduler.run_job(job["id"], trigger="schedule")
    assert run["awaiting_approval"], "the job must leave a pending approval"
    assert not (tmp_path / "scratch" / "nightly.md").exists(), "it must NOT have written"
    assert len(kernel.approvals.pending()) == 1


def test_a_failing_job_is_recorded_not_fatal(tmp_path):
    class Boom(FakeClient):
        def chat(self, *a, **k):
            raise RuntimeError("engine down")

    audit = AuditLog(tmp_path)
    kernel = Kernel(registry=default_registry(), client=Boom([]), model="fake",
                    audit=audit)
    store = ScheduleStore(tmp_path / "schedules.json")
    scheduler = Scheduler(store, kernel, audit)
    job = store.create("flaky", "do something", every_minutes=5)

    run = scheduler.run_job(job["id"])
    assert run["ok"] is False and "engine down" in run["error"]
    assert store.get(job["id"])["next_run"], "a failure must not stop future runs"
