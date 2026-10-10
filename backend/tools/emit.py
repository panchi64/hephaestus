"""How a tool says something on the operator's stream.

A tool sometimes has news that is not its return value: a live view came up, a sandbox
image is still downloading, this turn opened a research thread. The model does not need
any of it — it is for the person watching — so it does not belong in the tool's result.

The old way was to reach through ``ctx.deps`` for the ``Run`` and call ``run.emit``.
Two things were wrong with that. A tool needed our deps object to do something Pydantic
AI was already carrying, and the emit landed *outside* the stream's ordering: a value
written straight to the run is visible the instant it is written, while everything around
it — the ``tool.started`` frame for this very call — is delivered when the translator
reaches it. A progress line could therefore arrive before the call it belonged to.

Now a tool awaits ``ctx.emit`` and the event takes its place in the stream like any
other. ``agent/translate.py`` unwraps it and puts the body on the run, in the one place
that already translates the engine's events into our protocol.

Two consequences worth knowing:

- **Only async tools can emit.** A sync tool has no event stream to emit into; the
  library raises rather than silently dropping it.
- **The library stamps the call.** When a tool emits, ``tool_call_id`` and ``tool_name``
  land on the *event* automatically, so the translator can always attribute it — the
  bodies that carry a ``tool_call_id`` of their own still set it themselves, because that
  field is part of the wire contract in ``runs/events/`` and not ours to make optional.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel
from pydantic_ai import CustomEvent


@dataclass(kw_only=True)
class RunEventEmitted(CustomEvent):
    """A ``runs.events`` body a tool put on the stream.

    One wrapper rather than an event class per message: the bodies are already defined —
    ``runs/events/`` *is* the wire contract — and restating them as event subclasses
    would only give the translator the same set to unwrap again.
    """

    body: BaseModel
