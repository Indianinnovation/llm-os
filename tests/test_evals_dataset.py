"""Golden dataset sanity: well-formed, unique ids, known tools/categories."""

import json
from pathlib import Path

DATASET = Path(__file__).resolve().parent.parent / "evals" / "golden_prompts.jsonl"
VALID_CATEGORIES = {"calculator", "write_markdown", "mcp", "memory", "chat", "telecom"}
VALID_TOOLS = {
    "calculator",
    "write_markdown",
    "get_local_time",
    "get_disk_usage",
    "get_system_info",
    "remember",
    "search_memory",
    "network_health_check",
    "diagnose_cell",
    "get_cell_kpis",
    "get_active_alarms",
    "get_recent_logs",
    "search_specs",
    "get_kpi_trend",
    "analyze_cluster_impact",
    "monitor_sweep",
    "get_status_history",
    "generate_incident_report",
    "audit_cell_config",
    None,
}


def load_items():
    return [
        json.loads(line)
        for line in DATASET.read_text().splitlines()
        if line.strip()
    ]


def test_dataset_is_well_formed():
    items = load_items()
    assert len(items) >= 30

    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids)), "duplicate ids"

    for item in items:
        assert item["category"] in VALID_CATEGORIES, item["id"]
        assert item["prompt"].strip(), item["id"]
        expected = item["expect"].get("tool")
        tools = expected if isinstance(expected, list) else [expected]
        for tool in tools:
            assert tool in VALID_TOOLS, f"{item['id']}: unknown tool {tool}"


def test_chat_items_expect_no_tool():
    for item in load_items():
        if item["category"] == "chat":
            assert item["expect"]["tool"] is None, item["id"]


def test_calculator_items_have_verifiable_answers():
    for item in load_items():
        if item["category"] == "calculator":
            assert "result_approx" in item["expect"], item["id"]
