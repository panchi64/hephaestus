"""No tool hands the model a bare exception string as the whole of a failure.

``ModelRetry(str(exc))`` tells the model what broke and nothing about what to do next, so a
small model retries the same call. Messages go through ``core.diagnostics`` or say the
next step in their own words.
"""

from __future__ import annotations

import ast
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
_RAISES = {"ModelRetry", "ToolFailed"}


def _bare(arg: ast.expr) -> bool:
    """``str(x)``, ``f"{x}"`` or a bare name: the exception's text and nothing else."""
    if isinstance(arg, ast.Name):
        return True
    if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "str":
        return True
    return isinstance(arg, ast.JoinedStr) and all(
        isinstance(value, ast.FormattedValue) for value in arg.values
    )


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(TOOLS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _RAISES
                and node.args
                and _bare(node.args[0])
            ):
                found.append(f"{path.name}:{node.lineno}")
    return found


def test_no_tool_failure_is_only_the_exception_text() -> None:
    assert _offenders() == []
