"""Preflight gate tests: config-drift detection with injected state."""


import pytest

from llm_os.preflight import (
    FAIL,
    PASS,
    WARN,
    PreflightReport,
    check_memory_telemetry,
    check_models,
    check_streamlit_telemetry,
)


def by_name(report, name):
    return next(c for c in report.checks if c.name == name)


def test_streamlit_telemetry_pass(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[browser]\ngatherUsageStats = false\n")
    report = PreflightReport()
    check_streamlit_telemetry(report, cfg)
    assert by_name(report, "UI telemetry disabled").status == PASS


@pytest.mark.parametrize("content", ["", "[theme]\nbase='light'\n",
                                     "[browser]\ngatherUsageStats = true\n"])
def test_streamlit_telemetry_fails_when_not_disabled(tmp_path, content):
    cfg = tmp_path / "config.toml"
    cfg.write_text(content)
    report = PreflightReport()
    check_streamlit_telemetry(report, cfg)
    check = by_name(report, "UI telemetry disabled")
    assert check.status == FAIL
    assert "gatherUsageStats" in check.hint


def test_memory_telemetry_guard_passes_on_current_source():
    report = PreflightReport()
    check_memory_telemetry(report)
    assert by_name(report, "Vector-store telemetry disabled").status == PASS


def test_model_checks(tmp_path):
    report = PreflightReport()
    check_models(report, ["llama3.2:latest", "all-minilm:latest"])
    assert by_name(report, "Routing model").status == PASS
    assert by_name(report, "Embedding model (memory)").status == PASS

    report = PreflightReport()
    check_models(report, ["something-else:latest"])
    assert by_name(report, "Routing model").status == FAIL
    assert by_name(report, "Embedding model (memory)").status == WARN


def test_report_ok_requires_no_fail():
    report = PreflightReport()
    report.add("a", PASS, "")
    report.add("b", WARN, "")
    assert report.ok
    report.add("c", FAIL, "")
    assert not report.ok
