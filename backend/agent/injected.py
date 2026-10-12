"""How a message that is not the operator's reaches the model.

A sub-agent's report is delivered into the launching thread the same way an operator's
mid-turn message is — queued on the Run, handed over at the next model-request boundary.
That road is the right one and was chosen deliberately: the injection point amends the
*next, not-yet-sent* request, so nothing queued can interrupt a model that is mid-stream.

What it cannot do is let the two arrive looking the same. Pydantic AI has one shape for a
message from outside the model — ``UserPromptPart`` — so a report rides in as one, and
without a marker the transcript would claim the operator typed it, the model would answer
as though they had, and every later turn would replay it that way. So the text is wrapped,
once, here.

A direction travelling the other way — the launching agent redirecting a sub-agent that is
still working — has the mirror of that problem and takes the mirror of this road: same
queue, same boundary, a different envelope. Which one a message gets is decided by its
``source``, never by looking at its text.

The envelopes themselves are ``services/subagents/report.py``, not here: the transcript
reads them back on the way out and sits below this layer, so one home for the markers is
the only way their writers and their readers cannot drift.
"""

from __future__ import annotations

from runs import QueuedMessage
from services.subagents.report import message_envelope, report_envelope


def injected_text(message: QueuedMessage) -> str:
    """One queued message as the model should read it.

    The operator's own words are handed over untouched: they are what a user message is,
    and wrapping them would put framing between the operator and the model that neither
    asked for. Everything else came down a link between a thread and a sub-agent, and is
    framed as whichever direction it travelled.
    """
    if message.source == "subagent":
        return report_envelope(message.text)
    if message.source == "agent":
        return message_envelope(message.sender or "an agent", message.text)
    return message.text
