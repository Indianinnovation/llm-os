#!/usr/bin/env python3
"""Routing eval harness: measures how accurately a model routes intents.

Runs the golden prompt set through the real kernel (in-process, real
tools, isolated scratchpad/audit/memory) and scores three things:

- tool selection: did the model pick the expected tool — or correctly
  pick NO tool for plain-chat prompts (the set includes trap prompts
  that mention calculators, disks and writing but need no tool)?
- execution: did every emitted tool call validate and run successfully?
- exactness: for math items, did the pipeline produce the right number?

Usage:
    python scripts/run_evals.py                        # default model
    python scripts/run_evals.py --models llama3.2,qwen2.5-coder
    python scripts/run_evals.py --category chat --limit 5

Results are printed as a scoreboard and saved to evals/results/ as JSON
so runs across models and quantizations stay comparable.
"""

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from ollama import Client  # noqa: E402

from llm_os import config  # noqa: E402
from llm_os.audit import AuditLog  # noqa: E402
from llm_os.kernel import Kernel  # noqa: E402
from llm_os.mcp_client import MCPManager  # noqa: E402
from llm_os.memory import create_memory  # noqa: E402
from llm_os.tools import default_registry  # noqa: E402
from llm_os.tools.memory_tools import memory_tools  # noqa: E402

GREEN, RED, YELLOW, BOLD, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[2m", "\033[0m",
)

CATEGORIES = ["calculator", "write_markdown", "mcp", "memory", "chat"]


class StubMemory:
    """Schema-complete stand-in so memory tools stay routable even when
    the embedding model is unavailable (routing is what's measured)."""

    def archive(self, text, kind="fact"):
        return "stub"

    def recall(self, query, k=6, max_distance=0.7):
        return []

    def count(self):
        return 0


def load_dataset(path: Path, category: str, limit: int) -> list:
    items = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if category:
        items = [i for i in items if i["category"] == category]
    return items[:limit] if limit else items


def build_registry(workdir: Path):
    """Real tools, isolated side effects. Returns (registry, mcp_manager)."""
    config.SCRATCHPAD_DIR = workdir / "scratchpad"
    registry = default_registry()

    memory = create_memory(
        workdir / "memory", config.OLLAMA_HOST, config.EMBED_MODEL
    ) or StubMemory()
    for tool in memory_tools(memory):
        registry.register(tool)

    mcp = MCPManager(PROJECT_ROOT / "mcp_servers.json")
    mcp.start()
    mcp.register_tools(registry)
    return registry, mcp


def score_item(item: dict, result: dict) -> dict:
    expect = item["expect"]
    trace = result.get("trace", [])
    tools_called = [t["tool"] for t in trace]

    expected_tool = expect.get("tool")
    if expected_tool is None:
        routed_ok = not tools_called
    else:
        allowed = expected_tool if isinstance(expected_tool, list) else [expected_tool]
        routed_ok = any(t in allowed for t in tools_called)

    exec_ok = all(t["status"] == "success" for t in trace)

    exact_ok = None
    if "result_approx" in expect:
        expected_value = float(expect["result_approx"])
        tolerance = float(
            expect.get("tolerance", max(1e-6, 0.005 * abs(expected_value)))
        )
        exact_ok = any(
            t["tool"] == "calculator"
            and t["status"] == "success"
            and isinstance(t.get("result"), dict)
            and isinstance(t["result"].get("result"), (int, float))
            and abs(t["result"]["result"] - expected_value) <= tolerance
            for t in trace
        )

    passed = routed_ok and exec_ok and (exact_ok is not False)
    return {
        "id": item["id"],
        "category": item["category"],
        "prompt": item["prompt"],
        "tools_called": tools_called,
        "routed_ok": routed_ok,
        "exec_ok": exec_ok,
        "exact_ok": exact_ok,
        "passed": passed,
        "duration_ms": result.get("duration_ms", 0),
        "reply_preview": (result.get("reply") or "")[:120],
    }


def run_model(model: str, items: list, registry) -> dict:
    print(f"\n{BOLD}▶ Evaluating {model} on {len(items)} prompts{RESET}")
    with tempfile.TemporaryDirectory() as tmp:
        kernel = Kernel(
            registry=registry,
            client=Client(host=config.OLLAMA_HOST),
            model=model,
            audit=AuditLog(Path(tmp)),
            memory=None,  # no auto-paging: evals measure routing in isolation
        )
        scores = []
        for index, item in enumerate(items, 1):
            try:
                result = kernel.handle(item["prompt"])
            except Exception as exc:
                result = {"reply": f"KERNEL ERROR: {exc}", "trace": []}
            score = score_item(item, result)
            scores.append(score)
            mark = f"{GREEN}✓{RESET}" if score["passed"] else f"{RED}✗{RESET}"
            called = ",".join(score["tools_called"]) or "no tool"
            print(
                f"  {mark} [{index:>2}/{len(items)}] {score['id']:<10} "
                f"{DIM}→ {called} ({score['duration_ms'] / 1000:.1f}s){RESET}"
            )
    return summarize(model, scores)


def summarize(model: str, scores: list) -> dict:
    def rate(subset, key):
        relevant = [s for s in subset if s[key] is not None]
        if not relevant:
            return None
        return sum(1 for s in relevant if s[key]) / len(relevant)

    categories = {}
    for category in CATEGORIES:
        subset = [s for s in scores if s["category"] == category]
        if not subset:
            continue
        categories[category] = {
            "n": len(subset),
            "pass_rate": rate(subset, "passed"),
            "routing_rate": rate(subset, "routed_ok"),
            "exec_rate": rate(subset, "exec_ok"),
            "exact_rate": rate(subset, "exact_ok"),
            "avg_latency_ms": sum(s["duration_ms"] for s in subset) / len(subset),
        }
    return {
        "model": model,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n": len(scores),
        "overall_pass_rate": rate(scores, "passed"),
        "overall_routing_rate": rate(scores, "routed_ok"),
        "categories": categories,
        "items": scores,
    }


def percent(value) -> str:
    return "  n/a" if value is None else f"{value * 100:>4.0f}%"


def print_scoreboard(summary: dict) -> None:
    print(f"\n{BOLD}Scoreboard — {summary['model']}{RESET}")
    header = f"  {'category':<16}{'n':>4}  {'pass':>5}  {'route':>6}  {'exec':>5}  {'exact':>6}  {'avg lat':>8}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for name, c in summary["categories"].items():
        print(
            f"  {name:<16}{c['n']:>4}  {percent(c['pass_rate'])}  "
            f"{percent(c['routing_rate']):>6}  {percent(c['exec_rate'])}  "
            f"{percent(c['exact_rate']):>6}  {c['avg_latency_ms'] / 1000:>6.1f}s"
        )
    print("  " + "-" * (len(header) - 2))
    print(
        f"  {'OVERALL':<16}{summary['n']:>4}  "
        f"{percent(summary['overall_pass_rate'])}  "
        f"{percent(summary['overall_routing_rate']):>6}"
    )
    failures = [s for s in summary["items"] if not s["passed"]]
    if failures:
        print(f"\n{BOLD}Failures{RESET}")
        for s in failures:
            reason = (
                "misrouted" if not s["routed_ok"]
                else "tool error" if not s["exec_ok"]
                else "wrong result"
            )
            called = ",".join(s["tools_called"]) or "no tool"
            print(f"  {RED}✗{RESET} {s['id']:<10} {reason:<12} called: {called:<24} {DIM}{s['prompt'][:60]}{RESET}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default=config.MODEL_NAME,
                        help="Comma-separated model names to compare")
    parser.add_argument("--dataset", default=str(PROJECT_ROOT / "evals" / "golden_prompts.jsonl"))
    parser.add_argument("--category", choices=CATEGORIES, default=None)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    items = load_dataset(Path(args.dataset), args.category, args.limit)
    if not items:
        print("No dataset items selected.")
        return 1

    print(f"{BOLD}🎯 LLM OS Routing Evals{RESET} — {len(items)} prompts")
    with tempfile.TemporaryDirectory() as workdir:
        registry, mcp = build_registry(Path(workdir))
        tool_names = [t["name"] for t in registry.describe()]
        print(f"{DIM}Registry: {tool_names}{RESET}")

        summaries = []
        try:
            for model in [m.strip() for m in args.models.split(",") if m.strip()]:
                summary = run_model(model, items, registry)
                print_scoreboard(summary)
                summaries.append(summary)
        finally:
            mcp.shutdown()

    results_dir = PROJECT_ROOT / "evals" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    for summary in summaries:
        safe_model = summary["model"].replace("/", "_").replace(":", "_")
        out = results_dir / f"{safe_model}_{time.strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(summary, indent=2))
        print(f"\n{DIM}Saved: {out}{RESET}")

    if len(summaries) > 1:
        print(f"\n{BOLD}Model comparison (overall pass rate){RESET}")
        for summary in sorted(summaries, key=lambda s: -(s["overall_pass_rate"] or 0)):
            print(f"  {summary['model']:<24} {percent(summary['overall_pass_rate'])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
