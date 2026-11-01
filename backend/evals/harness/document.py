"""The transcript a grading session reads: one run, every scenario, every sub-agent."""

from __future__ import annotations

from collections import Counter

from evals.drive.render import TASK_MARK, quote, tool_line
from evals.drive.timeline import (
    Approval,
    Error,
    Injected,
    Limit,
    Permission,
    Plan,
    Question,
    SubagentRecord,
    Tasks,
    Text,
    Tool,
    Turn,
)

from .record import Run, Sample
from .spec import Scenario


def _segment(segment: object) -> list[str]:
    match segment:
        case Text(text=text) if text.strip():
            return ["- **beat:**", "", quote(text), ""]
        case Tool():
            return [tool_line(segment)]
        case Tasks(items=items):
            listed = ", ".join(f"[{TASK_MARK.get(s, ' ')}] {c}" for s, c in items)
            return [f"- **task list:** {listed}"]
        case Question(questions=questions, options=options):
            return [
                f"- **question:** {q} — options: {' | '.join(o)}"
                for q, o in zip(questions, options, strict=True)
            ]
        case Approval(name=name, summary=summary, refused=refused):
            decision = f"refused by the eval: {refused}" if refused else "approved"
            return [f"- **approval** `{name}` ({decision}): {summary}"]
        case Injected(source=source):
            return [f"- **steer reached the turn** (from {source})"]
        case Plan(title=title, status=status, steps=steps):
            return [f"- **plan** {title!r} ({status}): {' / '.join(steps)}"]
        case Permission(level=level, reason=reason):
            return [f"- **permission → {level}**: {reason}"]
        case Limit(limit=limit, message=message):
            return [f"- **limit `{limit}`:** {message}"]
        case Error(message=message):
            return [f"- **run error:** {message}"]
        case _:
            return []


def _turn(turn: Turn, title: str, speaker: str) -> list[str]:
    out = [f"#### {title}", ""]
    if turn.delivery:
        out += ["*A sub-agent's report reached the parent.*", ""]
    elif turn.prompt:
        out += ["**Operator:**", "", quote(turn.prompt), ""]
    ended = f"*Ended `{turn.ended}`" + (f": {turn.detail}" if turn.detail else "")
    out += [f"{ended} · {turn.steps} requests · {turn.wall_s:.0f}s*", ""]
    if work := turn.work():
        out += ["**Work:**", ""]
        for segment in work:
            out += _segment(segment)
        out.append("")
    final = turn.final_reply()
    out += [f"**{speaker} (final reply):**", "", quote(final) if final else "*(no reply text)*", ""]
    return out


def _subagent(agent: SubagentRecord) -> list[str]:
    model = f" · on `{agent.model}` ({agent.model_source})" if agent.model else ""
    out = [
        f"#### Sub-agent `{agent.handle}` · {agent.name} · {agent.agent_class} · depth "
        + f"{agent.depth} — `{agent.status}`{model}",
        "",
        "Brief:",
        "",
        quote(agent.task),
        "",
        "Report:",
        "",
        quote(agent.report) if agent.report else "*(no report)*",
        "",
    ]
    if agent.error:
        out += [f"Error: {agent.error}", ""]
    for index, turn in enumerate(agent.turns, start=1):
        out += _turn(turn, f"`{agent.handle}` run {index}", agent.handle)
    return out


def _requests(sample: Sample) -> list[str]:
    counts = Counter((r.endpoint, r.model) for r in sample.requests if r.tools)
    if not counts:
        return []
    listed = ", ".join(f"`{e}/{m}` ×{n}" for (e, m), n in counts.most_common())
    return [f"**Agent requests by endpoint:** {listed}", ""]


def _checks(sample: Sample) -> list[str]:
    out = ["**Checks:**", "", "| | Check | Detail |", "|---|---|---|"]
    for check in sample.checks:
        mark = "pass" if check.passed else f"**FAIL** ({check.kind})"
        implicit = " *(implicit)*" if check.implicit else ""
        out.append(f"| {mark} | {check.label}{implicit} | {check.detail} |")
    return [*out, ""]


def _sample(sample: Sample) -> list[str]:
    out = [f"### Sample {sample.sample}", ""]
    if sample.problem:
        out += [f"*Incomplete: {sample.problem}*", ""]
    if sample.actions:
        out += ["**Mid-run actions:**", "", *(f"- {a}" for a in sample.actions), ""]
    for index, turn in enumerate(sample.turns, start=1):
        out += _turn(turn, f"Turn {index}", "Talos")
    if sample.later:
        out += ["*A new thread:*", ""]
        for index, turn in enumerate(sample.later, start=1):
            out += _turn(turn, f"New-thread turn {index}", "Talos")
    out.append(f"- title: {sample.title!r}" if sample.title else "- title: *(none produced)*")
    if sample.tasks:
        listed = ", ".join(f"[{TASK_MARK.get(s, ' ')}] {c}" for s, c in sample.tasks)
        out.append(f"- final task list: {listed}")
    out.append("")
    for agent in sample.subagents:
        out += _subagent(agent)
    return [*out, *_requests(sample), *_checks(sample)]


def render(run: Run, scenarios: list[Scenario]) -> str:
    by_id = {s.id: s for s in scenarios}
    out = [
        f"# Harness run {run.group} / {run.name}",
        "",
        f"- started: {run.started_at}",
        f"- prompt fingerprint: `{run.prompt_fingerprint}`",
        f"- samples per scenario: {max((s.sample for s in run.samples), default=0)}",
    ]
    if run.routing.local or run.routing.hosted:
        out.append(f"- routing models: local `{run.routing.local}`, hosted `{run.routing.hosted}`")
    if run.stub:
        out.append("- **model: the dev stub. Plumbing check only; do not grade.**")
    if run.skipped:
        out.append(f"- skipped: {', '.join(f'`{k}` ({v})' for k, v in run.skipped.items())}")
    out += ["", "Grade this document by `backend/evals/harness/GRADING.md`.", ""]
    seen: set[str] = set()
    for sample in run.samples:
        if sample.scenario not in seen:
            seen.add(sample.scenario)
            scenario = by_id[sample.scenario]
            out += [
                f"## {scenario.id} ({scenario.area})",
                "",
                f"*{scenario.about}*",
                "",
                f"Dimensions: {', '.join(f'`{d}`' for d in scenario.graded)}",
                "",
                "Expect:",
                "",
                *(f"{i}. {item}" for i, item in enumerate(scenario.expect, start=1)),
                "",
            ]
        out += _sample(sample)
    return "\n".join(out).rstrip() + "\n"
