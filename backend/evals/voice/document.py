"""The transcript document a grading session reads: one run, every scenario, blind."""

from __future__ import annotations

from collections.abc import Iterable

from .lint import Flag, lint_reply
from .record import Approval, Error, Question, Run, Sample, Tasks, Text, Tool, Turn
from .spec import Scenario

_TASK_MARK = {"completed": "x", "in_progress": "~", "cancelled": "-"}


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


def _flags(flags: Iterable[Flag]) -> str:
    listed = [f"`{flag.rule}`: {flag.match}" for flag in flags]
    return f"*Lint:* {'; '.join(listed)}" if listed else "*Lint:* none"


def _tool_line(tool: Tool) -> str:
    indent = "  " if tool.nested else ""
    mark = {"ok": "ok", "failed": "FAILED", "running": "no result"}[tool.status]
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


def _turn(turn: Turn, index: int) -> list[str]:
    out = [f"#### Turn {index}", "", "**Operator:**", "", _quote(turn.prompt), ""]
    if turn.ended != "done":
        out += [f"*Turn ended `{turn.ended}`{f': {turn.detail}' if turn.detail else ''}.*", ""]
    final = turn.final_reply()
    work = turn.work()
    if work:
        out += ["**Work** (beats, tool cards, instruments, in order):", ""]
        for segment in work:
            match segment:
                case Text(text=text) if text.strip():
                    out += ["- **beat:**", "", _quote(text), "", f"  {_flags(lint_reply(text))}"]
                case Tool():
                    out.append(_tool_line(segment))
                case Tasks(items=items):
                    listed = ", ".join(f"[{_TASK_MARK.get(s, ' ')}] {c}" for s, c in items)
                    out.append(f"- **task list:** {listed}")
                case Question(questions=questions, options=options):
                    for question, choices in zip(questions, options, strict=True):
                        out.append(f"- **question:** {question} — options: {' | '.join(choices)}")
                case Approval(name=name, summary=summary, explanation=explanation):
                    reason = f" — {explanation}" if explanation else ""
                    out.append(f"- **approval** `{name}`: {summary}{reason}")
                case Error(message=message):
                    out.append(f"- **run error:** {message}")
                case _:
                    pass
        out.append("")
    out += ["**Talos (final reply):**", ""]
    out += [_quote(final), "", _flags(lint_reply(final))] if final else ["*(no reply text)*"]
    return [*out, ""]


def _sample(sample: Sample) -> list[str]:
    out = [f"### Sample {sample.sample}", ""]
    if sample.problem:
        out += [f"*Incomplete: {sample.problem}*", ""]
    for index, turn in enumerate(sample.turns, start=1):
        out += _turn(turn, index)
    out += ["**Helpers:**", ""]
    if sample.title:
        regenerated = (
            " *(regenerated: the live title timed out)*" if sample.title_regenerated else ""
        )
        out.append(f"- title: {sample.title!r}{regenerated}")
    else:
        out.append("- title: *(none produced)*")
    summary = sample.work_summary
    out.append(f"- work summary: {summary!r}" if summary else "- work summary: *(none produced)*")
    out.append("")
    for agent in sample.subagents:
        out += [
            f"**Sub-agent** `{agent.handle}` · {agent.callsign} · {agent.agent_class}"
            + f" — status `{agent.status}`",
            "",
            "Brief from Talos:",
            "",
            _quote(agent.task),
            "",
            "Report:",
            "",
            _quote(agent.report) if agent.report else "*(no report)*",
            "",
        ]
        if agent.report:
            out += [_flags(lint_reply(agent.report)), ""]
        if agent.error:
            out += [f"Error: {agent.error}", ""]
    return out


def render(run: Run, scenarios: list[Scenario]) -> str:
    by_id = {s.id: s for s in scenarios}
    out = [
        f"# Voice run {run.group} / {run.name}",
        "",
        f"- started: {run.started_at}",
        f"- prompt fingerprint: `{run.prompt_fingerprint}`",
        f"- samples per scenario: {max((s.sample for s in run.samples), default=0)}",
    ]
    if run.stub:
        out.append("- **model: the dev stub. Plumbing check only; do not grade for voice.**")
    out += ["", "Grade this document by `backend/evals/voice/GRADING.md`.", ""]
    seen: list[str] = []
    for sample in run.samples:
        if sample.scenario not in seen:
            seen.append(sample.scenario)
            scenario = by_id[sample.scenario]
            out += [
                f"## {scenario.id}",
                "",
                f"*{scenario.about}*",
                "",
                f"Dimensions: {', '.join(f'`{d}`' for d in scenario.dimensions)}, `helper`",
                "",
                "Expect:",
                "",
                *(f"{i}. {item}" for i, item in enumerate(scenario.expect, start=1)),
                "",
            ]
        out += _sample(sample)
    return "\n".join(out).rstrip() + "\n"
