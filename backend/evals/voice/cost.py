"""What a run cost, broken down by kind of call and by scenario. ``collect`` writes it
to ``cost.md`` beside the transcript; this rewrites it from ``run.json``.

    uv run python -m evals.voice.cost <group>/<letter> [...]
"""

from __future__ import annotations

import sys

from .record import Run, load_run
from .spec import run_path
from .usage import Usage, total

COST_FILE = "cost.md"


def _row(name: str, usage: Usage) -> str:
    cells = [
        name,
        str(usage.requests),
        f"{usage.input_tokens:,}",
        f"{usage.cache_read_share:.0%}",
        f"{usage.cache_write_tokens:,}",
        f"{usage.output_tokens:,}",
        f"${usage.cost_usd:.2f}",
        f"${usage.saved_usd:.2f}",
    ]
    return f"| {' | '.join(cells)} |"


_HEAD = [
    "| | Requests | Prompt tokens | From cache | Cache writes | Output | Cost | Saved by cache |",
    "|---|---|---|---|---|---|---|---|",
]


def render_cost(run: Run) -> str:
    spent = run.usage()
    out = [f"# Cost: {run.group} / {run.name}", ""]
    if not spent.requests:
        return "\n".join([*out, "No usage recorded: only Anthropic responses are counted.", ""])
    out += [
        f"**${spent.cost_usd:.2f}** over {spent.requests} requests; caching saved "
        + f"${spent.saved_usd:.2f}. Peak: {run.peak_tokens_per_minute or 0:,} prompt "
        + "tokens in one minute.",
        "",
    ]
    if spent.unpriced:
        out += [f"{spent.unpriced} requests had no price and aren't in the cost.", ""]
    agent = total(u for s in run.samples for k, u in s.usage.items() if k == "agent")
    utility = total(u for s in run.samples for k, u in s.usage.items() if k == "utility")
    helpers = total(run.helper_usage.values())
    out += [
        *_HEAD,
        _row("Agent turns (Talos and sub-agents)", agent),
        _row("Titles and summaries, during samples", utility),
        _row("Titles and summaries, after the scenarios", helpers),
        _row("**Total**", spent),
        "",
        "## By scenario",
        "",
        *_HEAD,
    ]
    by_scenario: dict[str, Usage] = {}
    for sample in run.samples:
        by_scenario.setdefault(sample.scenario, Usage()).merge(total(sample.usage.values()))
    out += [_row(f"`{sid}`", usage) for sid, usage in by_scenario.items()]
    return "\n".join([*out, ""])


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
