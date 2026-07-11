import pytest

from llm_os import config
from llm_os.registry import ToolError
from llm_os.tools import calculator, markdown_writer


def test_calculator_success():
    outcome = calculator.TOOL.run({"expression": "sqrt(3**2 + 4**2)"})
    assert outcome["result"] == 5.0


def test_calculator_rejects_code_injection():
    with pytest.raises(ToolError):
        calculator.TOOL.run({"expression": "__import__('os').system('id')"})


def test_calculator_validates_params():
    with pytest.raises(ToolError):
        calculator.TOOL.run({})  # missing expression
    with pytest.raises(ToolError):
        calculator.TOOL.run({"expression": 42})  # wrong type


def test_markdown_writer_creates_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCRATCHPAD_DIR", tmp_path)
    outcome = markdown_writer.TOOL.run(
        {"filename": "demo-note", "title": "Demo", "content": "Hello **world**."}
    )
    assert outcome["file"] == "demo-note.md"
    text = (tmp_path / "demo-note.md").read_text()
    assert text.startswith("# Demo")
    assert "Hello **world**." in text


@pytest.mark.parametrize(
    "bad_name",
    ["../escape", "/etc/passwd", "a/b", "..", ".hidden", "x" * 100],
)
def test_markdown_writer_blocks_bad_paths(tmp_path, monkeypatch, bad_name):
    monkeypatch.setattr(config, "SCRATCHPAD_DIR", tmp_path)
    with pytest.raises(ToolError):
        markdown_writer.TOOL.run(
            {"filename": bad_name, "title": "t", "content": "c"}
        )
