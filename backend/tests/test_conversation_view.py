"""Projecting a settled history into turns — the tool/operator boundary.

The case that pins this down is a tool that hands back pixels. ``browse_screenshot``
returns ``ToolReturn(text, content=[BinaryContent(png)])``, and Pydantic AI moves that
content into a ``UserPromptPart`` appended to the *same* ``ModelRequest`` as the tool
return, because no provider accepts an image inside a tool result. Read naively that is
indistinguishable from the operator speaking — and reading it that way costs three
things at once, all asserted below.
"""

from __future__ import annotations

from pydantic_ai import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from services.conversation_view import MAX_TOOL_IMAGE_BYTES, project_tree, tool_images

PNG = BinaryContent(data=b"\x89PNG-pretend", media_type="image/png")


def _screenshot_turn() -> list[tuple[str, ModelMessage]]:
    """One turn: the operator asks, the agent screenshots, then answers."""
    return [
        ("n0", ModelRequest(parts=[UserPromptPart(content="look at the page")])),
        (
            "n1",
            ModelResponse(
                parts=[ToolCallPart(tool_name="browse_screenshot", args={}, tool_call_id="c1")]
            ),
        ),
        (
            "n2",
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="browse_screenshot",
                        content="Took a screenshot.",
                        tool_call_id="c1",
                    ),
                    # Pydantic AI appends the image here — the shape under test.
                    UserPromptPart(content=[PNG]),
                ]
            ),
        ),
        ("n3", ModelResponse(parts=[TextPart(content="The page shows a login form.")])),
    ]


def test_screenshot_does_not_become_an_operator_turn():
    views = project_tree(_screenshot_turn())
    # Two turns, not three: the operator asked once and the agent answered once. A third
    # would be a turn nobody took, reading "[attachment]".
    assert [v.role for v in views] == ["user", "assistant"]
    assert all("[attachment]" not in v.content for v in views)


def test_the_assistant_turn_stays_whole_across_a_screenshot():
    views = project_tree(_screenshot_turn())
    # The answer belongs to the same bubble as the tool call that preceded it. Opening a
    # user turn on the image closed this one and started a second assistant turn, so the
    # work log and the answer it produced ended up in different bubbles.
    assistant = views[1]
    assert assistant.content == "The page shows a login form."
    assert [t.name for t in assistant.tools] == ["browse_screenshot"]


def test_the_tool_result_is_still_stitched():
    views = project_tree(_screenshot_turn())
    tool = views[1].tools[0]
    # The user-turn branch returned early, skipping the stitching below it — so the call
    # that produced the image was the one call left reading "running" forever.
    assert tool.status == "ok"
    assert tool.result == "Took a screenshot."


def test_the_image_lands_on_the_call_that_produced_it():
    views = project_tree(_screenshot_turn())
    images = views[1].tools[0].images
    assert [i.media_type for i in images] == ["image/png"]
    assert images[0].data == PNG.base64


def test_an_oversized_image_is_left_behind():
    """A picture too big to ship costs the transcript more than it is worth.

    The browser harness lets a capture reach 5 MB of PNG, which is ~6.7 MB of base64 on
    one stream frame and again in every read of the conversation after it. The call still
    says what it did in words — that is what the model read, and what the operator needs
    when the picture cannot come."""
    huge = BinaryContent(data=b"\x00" * (MAX_TOOL_IMAGE_BYTES + 1), media_type="image/png")
    assert tool_images([huge]) == []
    # The boundary is inclusive, so a capture right at the limit still arrives.
    at_limit = BinaryContent(data=b"\x00" * MAX_TOOL_IMAGE_BYTES, media_type="image/png")
    assert len(tool_images([at_limit])) == 1


def test_only_images_ride_along():
    """A tool may hand the model a PDF or an audio clip; a work-log card has nothing to
    do with either, and rendering one as an `<img>` would show a broken picture."""
    assert tool_images([BinaryContent(data=b"%PDF-1.7", media_type="application/pdf")]) == []
    assert tool_images("Screenshot captured.") == []


def test_an_operator_image_turn_is_still_an_operator_turn():
    """The rule is 'a request carrying tool returns', not 'a request carrying an image'.

    An operator attaching a picture to a vision model sends exactly the part shape the
    screenshot case does — minus the tool return. That one still has to read as a turn
    they took, or the fix would swallow the operator's own attachments."""
    views = project_tree(
        [("n0", ModelRequest(parts=[UserPromptPart(content=[PNG])]))],
    )
    assert [v.role for v in views] == ["user"]
    assert views[0].content == "[attachment]"


def test_the_turn_keeps_the_order_it_was_emitted_in():
    """``content``/``reasoning``/``tools`` flatten a turn into one lane per kind; the
    segments keep the think → call → text → call → text sequence the stream drew, so a
    reload does not regroup a turn the operator watched interleave."""
    views = project_tree(
        [
            ("n0", ModelRequest(parts=[UserPromptPart(content="go")])),
            (
                "n1",
                ModelResponse(
                    parts=[
                        ThinkingPart(content="plan "),
                        ThinkingPart(content="more"),
                        TextPart(content="Checking."),
                        ToolCallPart(tool_name="read", args={}, tool_call_id="c1"),
                    ]
                ),
            ),
            (
                "n2",
                ModelRequest(
                    parts=[ToolReturnPart(tool_name="read", content="x", tool_call_id="c1")]
                ),
            ),
            (
                "n3",
                ModelResponse(
                    parts=[
                        ThinkingPart(content="again"),
                        ToolCallPart(tool_name="read", args={}, tool_call_id="c2"),
                    ]
                ),
            ),
            (
                "n4",
                ModelRequest(
                    parts=[ToolReturnPart(tool_name="read", content="y", tool_call_id="c2")]
                ),
            ),
            ("n5", ModelResponse(parts=[TextPart(content="Done.")])),
        ]
    )
    segments = views[1].segments
    assert [(s.kind, s.text, s.tool_call_id) for s in segments] == [
        ("thinking", "plan more", None),
        ("text", "Checking.", None),
        ("tool", "", "c1"),
        ("thinking", "again", None),
        ("tool", "", "c2"),
        ("text", "Done.", None),
    ]
    # The flat answer lane is unchanged for search, which still reads it.
    assert views[1].content == "Checking.Done."
    assert views[0].segments == []


def test_an_empty_passage_does_not_split_its_neighbours():
    """The live translator emits no delta for an empty part, so the stream draws the two
    reasoning passages either side of one as a single block — the segments must too."""
    views = project_tree(
        [
            ("n0", ModelRequest(parts=[UserPromptPart(content="go")])),
            (
                "n1",
                ModelResponse(
                    parts=[
                        ThinkingPart(content="a"),
                        TextPart(content=""),
                        ThinkingPart(content=""),
                        ThinkingPart(content="b"),
                    ]
                ),
            ),
        ]
    )
    assert [(s.kind, s.text) for s in views[1].segments] == [("thinking", "ab")]
