"""What a run cost, broken down by kind of call and by scenario, as ``cost.md``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from .usage import Kind, Usage, total

COST_FILE = "cost.md"


class _CostedSample(Protocol):
    @property
    def scenario(self) -> str: ...

    @property
    def usage(self) -> Mapping[Kind, Usage]: ...


class Costed(Protocol):
    """The parts of a suite's run record a cost breakdown reads."""

    @property
    def group(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def samples(self) -> Sequence[_CostedSample]: ...

    @property
    def helper_usage(self) -> Mapping[Kind, Usage]: ...

    @property
    def peak_tokens_per_minute(self) -> int | None: ...


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


def render_cost(run: Costed) -> str:
    parts = [u for s in run.samples for u in s.usage.values()]
    spent = total([*parts, *run.helper_usage.values()])
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
