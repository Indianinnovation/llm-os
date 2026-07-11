"""Markdown document generator, confined to the scratchpad sandbox."""

import re
import time
from pathlib import Path

from pydantic import BaseModel, Field

from .. import config
from ..registry import Tool, ToolError

_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-]{0,80}$")


class MarkdownParams(BaseModel):
    filename: str = Field(
        ...,
        description="Base file name without extension, e.g. 'meeting-notes'. "
        "Letters, digits, spaces, hyphens and underscores only.",
    )
    title: str = Field(..., description="Document title (H1 heading).")
    content: str = Field(..., description="Markdown body of the document.")


def write_markdown(filename: str, title: str, content: str) -> dict:
    name = filename.strip().removesuffix(".md").strip()
    if not _FILENAME_RE.match(name):
        raise ToolError(
            "Invalid filename: use only letters, digits, spaces, hyphens, "
            "underscores (max 80 chars)."
        )

    sandbox = Path(config.SCRATCHPAD_DIR).resolve()
    sandbox.mkdir(parents=True, exist_ok=True)
    target = (sandbox / f"{name}.md").resolve()
    # Belt-and-braces: even with the regex, never write outside the sandbox.
    if sandbox not in target.parents:
        raise ToolError("Refused: path escapes the sandbox directory.")

    title = title.strip()
    body = content.strip()
    # Small models often repeat the title as a leading H1 in the body.
    if body.startswith(f"# {title}"):
        body = body[len(f"# {title}"):].lstrip()

    document = f"# {title}\n\n{body}\n\n---\n*Generated locally by LLM OS on {time.strftime('%Y-%m-%d %H:%M:%S')}*\n"
    target.write_text(document, encoding="utf-8")
    return {"file": target.name, "bytes": len(document.encode("utf-8"))}


TOOL = Tool(
    name="write_markdown",
    description=(
        "Create or overwrite a Markdown document in the local sandbox "
        "directory. Use this whenever the user asks to write, save, or "
        "generate a document, note, or report as a file."
    ),
    parameters=MarkdownParams,
    handler=write_markdown,
)
