"""Reading an ``ask_user`` call, writing the operator's reply into it, and reading it back.

One module owns every half on purpose. The questions the client renders, the prose the
model reads and the structure the transcript redraws are derived from the *same* place —
the arguments of the parked call — so a label the operator clicked cannot mean one thing
on screen and another in history. The client sends back which options it chose, never the
prose; this is where prose is made, and it is made from the parked arguments rather than
from anything the client said.

**Why the arguments are parsed defensively.** They are whatever the model produced. A
badly-shaped ``questions`` list must degrade to a thin question rather than raise: the
first caller is the park, which is a turn in the middle of stopping cleanly, and an
exception there strands the run instead of pausing it.

**Why it lives in ``services/`` rather than beside the park that first needed it.** Two
readers are below the agent layer: the answer is rendered into the tool's result by the
approve route, and read back out of that result by ``services/conversation_view.py`` so a
reloaded transcript draws the same card the live stream drew. The alternative to one home
is two copies of a format that only works while they agree — the same reasoning that put
``services/subagents/report.py`` where it is.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from runs.events import QuestionOption, QuestionSpec
from services.shapes import dict_list

#: The namespaced name of the tool this format belongs to. A literal rather than an import
#: because ``tools/`` sits above this layer; ``tests/test_ask_user.py`` pins it against the
#: name a real run resolves.
ASK_USER_TOOL = "builtin_ask_user"

#: The three markers :func:`render_answer` writes and :func:`parse_answer` reads. Named
#: once, because a round trip across two hand-written literals is a round trip that holds
#: until somebody improves the wording on one side only.
_QUESTION_PREFIX = "Q: "
_ANSWER_PREFIX = "A: "
_WROTE_PREFIX = "   They also wrote: "
_NO_OPTION_PREFIX = "(none of the options) "


class AnswerError(ValueError):
    """The reply doesn't fit the question it answers — an unknown option, or nothing at
    all. Raised for the route to render; never for the park."""


class AnsweredQuestion(BaseModel):
    """One question and what the operator said to it, as structure.

    What :func:`render_answer` writes is prose, because the model is its reader. This is
    the same answer for the readers that are not models — the live event and the cold
    transcript — so neither has to take the paragraph apart to draw a card from it."""

    question: str
    selections: list[str] = Field(default_factory=list)
    text: str | None = None


def questions_of(args: Mapping[str, object]) -> list[QuestionSpec]:
    """The questions carried by one ``ask_user`` call, as event bodies."""
    questions: list[QuestionSpec] = []
    for raw in dict_list(args.get("questions")):
        options = [
            QuestionOption(
                label=str(opt["label"]),
                description=(
                    str(opt["description"]) if opt.get("description") is not None else None
                ),
            )
            for opt in dict_list(raw.get("options"))
            if opt.get("label") is not None
        ]
        questions.append(
            QuestionSpec(
                question=str(raw.get("question", "")),
                options=options,
                multi_select=bool(raw.get("multi_select", False)),
            )
        )
    return questions


def render_answer(
    questions: list[QuestionSpec],
    replies: list[tuple[list[str], str | None]],
) -> str:
    """The operator's reply as the model will read it: each question restated, then what
    they said to it.

    The question is restated rather than referenced by index because this string is the
    tool's *result*, and a result that reads "1. Postgres" is only interpretable next to a
    call the model has to go back and re-read. Restating costs a line and removes the
    dependency — and it is also what makes the string readable back into structure long
    after the call's arguments have scrolled out of anyone's view (:func:`parse_answer`).

    Raises :class:`AnswerError` if the two don't line up, an option isn't one that was
    offered, or a question came back with nothing said to it at all.
    """
    if len(replies) != len(questions):
        raise AnswerError(f"expected one reply per question ({len(questions)}), got {len(replies)}")
    lines: list[str] = []
    for question, (selections, text) in zip(questions, replies, strict=True):
        offered = {opt.label for opt in question.options}
        unknown = [label for label in selections if label not in offered]
        if unknown:
            raise AnswerError(
                f"{unknown!r} was not offered for {question.question!r}; offered: {sorted(offered)}"
            )
        written = (text or "").strip()
        if not selections and not written:
            raise AnswerError(f"nothing was answered for {question.question!r}")
        lines.append(f"{_QUESTION_PREFIX}{question.question}")
        if selections:
            lines.append(f"{_ANSWER_PREFIX}{', '.join(selections)}")
            if written:
                # Both: they picked, and then said something about it. Kept distinct from
                # the choice so the model can tell an elaboration from a selection.
                lines.append(f"{_WROTE_PREFIX}{written}")
        else:
            # No option fit. Say so, so the model doesn't read the prose as a label it
            # should have offered and try to match it against its own list.
            lines.append(f"{_ANSWER_PREFIX}{_NO_OPTION_PREFIX}{written}")
    return "\n".join(lines)


def answered_questions(
    questions: list[QuestionSpec],
    replies: list[tuple[list[str], str | None]],
) -> list[AnsweredQuestion]:
    """The same reply as structure, from the same two inputs :func:`render_answer` takes.

    The live path has both in hand — the parked call's questions and the replies the route
    just validated — so it builds the structure directly rather than rendering prose and
    reading it back. Parsing exists for the *cold* path, where the arguments and the result
    are all that survived; using it here too would be a round trip taken for no reason and
    one more way the two paths could disagree."""
    return [
        AnsweredQuestion(
            question=question.question,
            selections=list(selections),
            text=(text or "").strip() or None,
        )
        # ``strict`` because the caller has *just* put the same two lists through
        # :func:`render_answer`, which refuses a mismatch — so a length difference here
        # could only mean the two were built from different sources, and silently
        # truncating the operator's answers is the worst way to find that out.
        for question, (selections, text) in zip(questions, replies, strict=True)
    ]


def parse_answer(questions: list[QuestionSpec], rendered: str) -> list[AnsweredQuestion]:
    """Read a rendered answer back into the structure it was written from.

    The exact inverse of :func:`render_answer`, and the two are pinned as a round trip by
    ``tests/test_ask_user.py``. It exists because a *settled* call carries only its
    arguments and its result: the replies the operator sent are long gone by the time a
    reload projects the transcript, so the result is where the answer has to come from.
    Asking the client to parse it instead would put the format in two codebases and break
    the day the wording improves.

    ``questions`` supplies the labels that were offered, which is what makes a selection
    containing ", " readable — the separator and the label are otherwise the same
    characters. Longest offered label wins; anything unrecognised is taken at the
    separator, which is what a history written before an option was renamed degrades to.

    **It degrades, never raises.** It also runs over history stored long before it existed,
    and a stored string it cannot read must cost the operator the card rather than the
    transcript: whatever it recovered is returned, and an unreadable string returns ``[]``.
    """
    offered_by_index = [[opt.label for opt in q.options] for q in questions]
    parsed: list[AnsweredQuestion] = []
    # Which field a line with no marker of its own continues — a question or an answer can
    # both carry newlines, and the model wrote the question.
    open_field: str | None = None
    for line in rendered.split("\n"):
        if line.startswith(_QUESTION_PREFIX):
            parsed.append(AnsweredQuestion(question=line[len(_QUESTION_PREFIX) :]))
            open_field = "question"
            continue
        if not parsed:
            # Prose before the first question — not something this format produces, so
            # there is nothing to hang it on.
            continue
        current = parsed[-1]
        if line.startswith(_ANSWER_PREFIX):
            payload = line[len(_ANSWER_PREFIX) :]
            if payload.startswith(_NO_OPTION_PREFIX):
                current.text = payload[len(_NO_OPTION_PREFIX) :] or None
            else:
                offered = offered_by_index[len(parsed) - 1] if len(parsed) <= len(questions) else []
                current.selections = _split_selections(payload, offered)
            open_field = "text"
        elif line.startswith(_WROTE_PREFIX):
            current.text = line[len(_WROTE_PREFIX) :]
            open_field = "text"
        elif open_field == "question":
            current.question += f"\n{line}"
        elif open_field == "text" and current.text is not None:
            current.text += f"\n{line}"
    return parsed


def _split_selections(payload: str, offered: list[str]) -> list[str]:
    """The labels in one ``A:`` line, split on ", " — but on the *right* ", ".

    A label may contain the separator ("Postgres, then MySQL" is a perfectly good option),
    so the split is checked against what the call actually offered, longest candidate
    first. With nothing offered to check against — an argument list too malformed to read —
    it falls back to the plain split, which is right far more often than treating the whole
    line as one label."""
    if not payload:
        return []
    pieces = payload.split(", ")
    if not offered:
        return [piece for piece in pieces if piece]
    labels: list[str] = []
    start = 0
    while start < len(pieces):
        for end in range(len(pieces), start, -1):
            candidate = ", ".join(pieces[start:end])
            if candidate in offered:
                labels.append(candidate)
                start = end
                break
        else:
            labels.append(pieces[start])
            start += 1
    return labels
