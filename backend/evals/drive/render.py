"""Markdown pieces every suite's transcript is built from."""

from __future__ import annotations

from .timeline import Tool

TASK_MARK = {"completed": "x", "in_progress": "~", "cancelled": "-"}


def quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


def tool_line(tool: Tool) -> str:
    indent = "  " if tool.nested else ""
    mark = {"ok": "ok", "failed": "FAILED", "running": "no result"}[tool.status]
    if tool.platform:
        mark += ", platform fault"
    lines = [f"{indent}- **tool** `{tool.name}` ({mark})"]
    if tool.narration:
        lines.append(f"{indent}  - narration: {tool.narration}")
    if tool.args:
        lines.append(f"{indent}  - args: `{tool.args.replace('`', "'")}`")
    if tool.outcome:
        lines.append(
            f"{indent}  - {'error' if tool.status == 'failed' else 'result'}: {tool.outcome}"
        )
    return "\n".join(lines)
