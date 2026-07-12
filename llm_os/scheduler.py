"""Scheduled agents — work that happens while you sleep.

    "Every morning at 08:00, check disk usage and write me a report."

A schedule is a prompt plus a cadence. The scheduler runs it through the
same kernel as a human would: the same tools, the same memory, the same
hash-chained audit trail — and, crucially, **the same approval gates**.
An unattended job that wants to touch something gated does not get to
authorize itself; it leaves a pending request for a human. That is the
difference between an OS doing work for you and an agent running loose.

Cadence is deliberately small and legible:
    every_minutes: 30        → every half hour
    daily_at: "08:00"        → once a day, local time
"""

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("llm_os.scheduler")

TICK_SECONDS = 20
MAX_RUNS_KEPT = 20


def _now() -> datetime:
    return datetime.now()


def compute_next_run(schedule: dict, after: Optional[datetime] = None) -> str:
    """When should this run next? Returns an ISO timestamp."""
    after = after or _now()
    if schedule.get("every_minutes"):
        minutes = max(1, int(schedule["every_minutes"]))
        return (after + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")

    daily = schedule.get("daily_at")
    if daily:
        hour, minute = (int(x) for x in daily.split(":"))
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate.strftime("%Y-%m-%d %H:%M:%S")

    # No cadence: never runs on its own (manual "run now" only).
    return ""


class ScheduleStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2, default=str))

    def create(self, name: str, prompt: str, every_minutes: int = 0,
               daily_at: str = "") -> dict:
        with self._lock:
            data = self._load()
            schedule = {
                "id": f"job-{uuid.uuid4().hex[:8]}",
                "name": name.strip() or "Unnamed job",
                "prompt": prompt.strip(),
                "every_minutes": int(every_minutes or 0),
                "daily_at": daily_at.strip(),
                "enabled": True,
                "created": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "last_run": "",
                "runs": [],
            }
            schedule["next_run"] = compute_next_run(schedule)
            data[schedule["id"]] = schedule
            self._save(data)
            return schedule

    def get(self, job_id: str) -> Optional[dict]:
        return self._load().get(job_id)

    def list(self) -> List[dict]:
        return sorted(self._load().values(), key=lambda s: s["created"], reverse=True)

    def update(self, job_id: str, **fields) -> Optional[dict]:
        with self._lock:
            data = self._load()
            schedule = data.get(job_id)
            if schedule is None:
                return None
            schedule.update(fields)
            if any(k in fields for k in ("every_minutes", "daily_at", "enabled")):
                schedule["next_run"] = (
                    compute_next_run(schedule) if schedule["enabled"] else ""
                )
            self._save(data)
            return schedule

    def delete(self, job_id: str) -> bool:
        with self._lock:
            data = self._load()
            if job_id not in data:
                return False
            del data[job_id]
            self._save(data)
            return True

    def record_run(self, job_id: str, run: dict) -> None:
        with self._lock:
            data = self._load()
            schedule = data.get(job_id)
            if schedule is None:
                return
            schedule["runs"] = ([run] + schedule.get("runs", []))[:MAX_RUNS_KEPT]
            schedule["last_run"] = run["ts"]
            schedule["next_run"] = (
                compute_next_run(schedule) if schedule["enabled"] else ""
            )
            self._save(data)

    def due(self, at: Optional[datetime] = None) -> List[dict]:
        at = at or _now()
        stamp = at.strftime("%Y-%m-%d %H:%M:%S")
        return [
            s for s in self._load().values()
            if s.get("enabled") and s.get("next_run") and s["next_run"] <= stamp
        ]


class Scheduler(threading.Thread):
    """Runs due jobs through the kernel. Daemon thread; never blocks shutdown."""

    def __init__(self, store: ScheduleStore, kernel, audit, tick: float = TICK_SECONDS):
        super().__init__(name="llm-os-scheduler", daemon=True)
        self.store = store
        self.kernel = kernel
        self.audit = audit
        self.tick = tick
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                for schedule in self.store.due():
                    self.run_job(schedule["id"], trigger="schedule")
            except Exception as exc:  # a bad job must not kill the scheduler
                logger.warning("scheduler tick failed: %s", exc)
            self._halt.wait(self.tick)

    def stop(self) -> None:
        self._halt.set()

    def run_job(self, job_id: str, trigger: str = "manual") -> dict:
        """Execute one job through the kernel — same tools, same audit,
        same approval gates. A gated tool leaves a pending request."""
        schedule = self.store.get(job_id)
        if schedule is None:
            return {"error": f"No schedule '{job_id}'."}

        started = time.time()
        audit_id = self.audit.append(
            "scheduled_run_started",
            {"job_id": job_id, "name": schedule["name"], "trigger": trigger},
        )
        try:
            result = self.kernel.handle(schedule["prompt"])
            tools = [t["tool"] for t in result.get("trace", [])]
            awaiting = [
                t["approval_id"] for t in result.get("trace", [])
                if t.get("status") == "awaiting_approval"
            ]
            run = {
                "ts": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "trigger": trigger,
                "ok": True,
                "reply": result.get("reply", "")[:1500],
                "tools": tools,
                "awaiting_approval": awaiting,
                "audit_ids": [t.get("audit_id") for t in result.get("trace", [])],
                "duration_ms": round((time.time() - started) * 1000, 1),
            }
        except Exception as exc:
            run = {
                "ts": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "trigger": trigger,
                "ok": False,
                "error": str(exc)[:300],
                "duration_ms": round((time.time() - started) * 1000, 1),
            }

        self.store.record_run(job_id, run)
        self.audit.append(
            "scheduled_run_finished",
            {
                "job_id": job_id,
                "started_audit_id": audit_id,
                "ok": run["ok"],
                "tools": run.get("tools", []),
                "awaiting_approval": run.get("awaiting_approval", []),
            },
        )
        return run
