"""Tool-call lifecycle events."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from core.envelopes import ContentPart

from .base import Body


class ToolStarted(Body):
    """A tool call began. ``parent_tool_call_id`` is set when a script made the call — the
    ``tool_call_id`` of the ``run_code`` call it was made from — and absent for a call the
    model made directly; the same field rides ``tool.completed`` and ``tool.failed``, so a
    client nests the whole lifecycle under the script that ran it. Additive to v1."""

    type: Literal["tool.started"] = "tool.started"
    tool_call_id: str
    name: str
    args: dict[str, object] = Field(default_factory=dict)
    parent_tool_call_id: str | None = None


class ToolPreparing(Body):
    """The model has named a tool and is still writing its arguments. Throttled, so
    ``args_chars`` rises in steps; ``tool.started`` with the same id supersedes it, and a
    call that never starts is dropped by the client at the next step."""

    type: Literal["tool.preparing"] = "tool.preparing"
    tool_call_id: str
    name: str
    args_chars: int = 0
    #: The id this call was announced under before the stream changed it.
    replaces: str | None = None


class ToolProgress(Body):
    type: Literal["tool.progress"] = "tool.progress"
    tool_call_id: str
    elapsed_s: float | None = None
    partial: str | None = None


class ToolImage(BaseModel):
    """An image a tool handed back for the model to look at — a screenshot, today.

    It rides the call's completion rather than arriving as a turn of its own: no
    provider accepts an image inside a tool result, so Pydantic AI moves it into a
    user-role part, and that wire detail must not reach the operator as a message they
    appear to have sent. The work log is where the call is, so it is where the picture
    of what the call saw belongs."""

    media_type: str
    data: str  # base64, no data-URI scheme — the renderer adds it


class ToolCompleted(Body):
    type: Literal["tool.completed"] = "tool.completed"
    tool_call_id: str
    name: str
    result: object = None
    #: ``result`` split into text and envelope parts, or None when it carries no envelope.
    result_parts: list[ContentPart] | None = None
    images: list[ToolImage] = Field(default_factory=list)
    parent_tool_call_id: str | None = None


class ToolFailed(Body):
    type: Literal["tool.failed"] = "tool.failed"
    tool_call_id: str
    name: str
    error: str
    parent_tool_call_id: str | None = None
    #: The failure is a fault in the platform rather than the call or the environment,
    #: so the operator's card marks it. Additive to v1.
    platform: bool = False
