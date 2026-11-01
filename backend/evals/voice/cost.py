"""Rewrite a voice run's ``cost.md`` from its ``run.json``; ``collect`` writes it first.

uv run python -m evals.voice.cost <group>/<letter> [...]
"""

from __future__ import annotations

import sys

from evals.drive.cost import COST_FILE, render_cost

from .record import load_run
from .spec import run_path

__all__ = ["COST_FILE", "main", "render_cost"]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for raw in argv:
        run_dir = run_path(raw)
        _ = (run_dir / COST_FILE).write_text(render_cost(load_run(run_dir)))
        print(f"wrote {run_dir / COST_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
