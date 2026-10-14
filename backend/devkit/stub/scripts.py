"""What the stub says, and how a test tells it what to say.

Scenarios are matched on **the last user message**, not on how many requests have been
made. That distinction is the whole design. An agent turn is a loop — the model answers,
a tool runs, the model is asked again with the result appended — so a script keyed by
call index says something different depending on how many tools the agent decided to
call, which is precisely the thing under test. Keyed by what the user actually asked,
the same script produces the same conversation however the loop unfolds.

A scenario answers every matching request unless it is given a ``uses`` count, and a
counted one is consumed — so a two-step exchange is scripted by pushing two scenarios
with the same match, the first with ``uses=1``: its reply asks for a tool, and the second
answers with the tool's result. Once the counted ones are spent the default reply takes
over, which is what keeps an agent that decided to loop once more from hanging.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from pydantic import JsonValue

from devkit.stub.wire import Reply, ToolCall

#: How many request bodies the stub keeps. Comfortably more than any one exchange needs
#: to assert against, far short of what an afternoon of chatting would accumulate.
REQUEST_LOG_LIMIT = 200


@dataclass
class Scenario:
    """One scripted reply, and the conditions for delivering it."""

    #: Case-insensitive substring of the last user message. ``None`` matches anything.
    match: str | None = None
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: Whether this reply is for a request that offered tools. ``None`` — the default —
    #: does not care. It exists because the prompt alone does not identify a request:
    #: the agent's turn and the utility calls beside it (the auto-title, a compaction)
    #: carry the same last user message and are told apart only by this. A scenario with
    #: ``tool_calls`` requires it whatever this says, since a model cannot call a tool
    #: that was not offered.
    needs_tools: bool | None = None
    #: Seconds to stall before replying — for watching a spinner, or tripping a timeout.
    latency_s: float = 0.0
    #: An HTTP status to fail with instead of replying. The error paths are as much a
    #: part of the product as the happy one, and far harder to provoke against a real
    #: endpoint that insists on working.
    status: int = 200
    error: str = "scripted failure"
    #: How many times this scenario may be used, ``None`` for without limit — which is
    #: the default, and deliberately so. The OpenAI client retries a 5xx of its own
    #: accord, so a failure scripted to happen once is consumed by the retry and the
    #: caller sees success: the one case where the wrong default produces a *silently*
    #: wrong test. Scripting a sequence is the case that says ``uses``, and saying it
    #: there reads naturally — this reply once, then the next one.
    uses: int | None = None

    def reply(self) -> Reply:
        return Reply(text=self.text, tool_calls=list(self.tool_calls))


#: What an unscripted request gets. Deliberately echoes the prompt rather than saying
#: something generic: a seeded conversation full of "OK" is indistinguishable from a
#: broken one, while an echo shows at a glance that the round trip worked.
DEFAULT_TEXT = "Stub model here. You said: {prompt}"


class ScriptBook:
    """The pushed scenarios, and the rule for choosing between them."""

    def __init__(self) -> None:
        self._scenarios: list[Scenario] = []
        #: The most recent request bodies, oldest first — so a test can assert what the
        #: agent actually sent (its tool schemas, its instructions) rather than only what
        #: came back. The stub is a listening post as much as a mouth.
        #:
        #: Bounded, because this process outlives any one test: a request carries the
        #: whole replayed transcript plus every tool schema, and an afternoon's chatting
        #: against an unbounded log would grow without limit and eventually make
        #: ``GET /_stub/requests`` too large to read.
        self.requests: deque[dict[str, JsonValue]] = deque(maxlen=REQUEST_LOG_LIMIT)

    def push(self, scenarios: list[Scenario]) -> None:
        self._scenarios.extend(scenarios)

    def reset(self) -> None:
        self._scenarios.clear()
        self.requests.clear()

    @property
    def pending(self) -> int:
        return len(self._scenarios)

    def next_for(self, prompt: str, *, has_tools: bool = True) -> Scenario:
        """The first unconsumed scenario matching this request, else the default reply.

        ``has_tools`` is not a refinement of the match — it is what makes matching by
        prompt work at all. One user message produces several requests: the chat turn
        that offers the agent's tools, and the utility calls beside it (the auto-title,
        a compaction) that offer none and carry the very same last user message. Without
        this a scripted tool call is handed to the titler, consumed there, and the turn
        it was written for answers in plain text — which looks like the agent deciding
        not to call the tool, and is the most misleading failure this stub can produce.

        Requiring tools of a tool-call scenario is also just true: a model cannot call a
        tool that was not offered to it.
        """
        for scenario in self._scenarios:
            wants = True if scenario.tool_calls else scenario.needs_tools
            if wants is not None and wants != has_tools:
                continue
            if scenario.match is None or scenario.match.lower() in prompt.lower():
                if scenario.uses is not None:
                    scenario.uses -= 1
                    if scenario.uses <= 0:
                        self._scenarios.remove(scenario)
                return scenario
        return Scenario(text=DEFAULT_TEXT.format(prompt=prompt or "(nothing)"))


def last_user_prompt(messages: list[JsonValue]) -> str:
    """The text of the last user message, flattened.

    Content arrives either as a plain string or as the list-of-parts form a multimodal
    request uses, and a stub that understood only the first would break the moment an
    attachment entered the conversation.
    """
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                text
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
                if isinstance(text := part.get("text", ""), str)
            )
    return ""
