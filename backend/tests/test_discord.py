"""The Discord plugin: message chunking, button ids, its settings route and its switch.

Nothing here connects to Discord; with no token the channel idles.
"""

from __future__ import annotations

import pytest

from plugins.discord.approvals import APPROVE, DENY, approval_text, custom_id, parse_custom_id
from plugins.discord.chunking import chunk
from services.channels import ApprovalRequest, PendingCall

from ._helpers import client_app
from ._json import json_body, json_list, obj, s


def _fence_balanced(piece: str) -> bool:
    return sum(1 for line in piece.split("\n") if line.strip().startswith("```")) % 2 == 0


def test_short_text_is_one_piece():
    assert chunk("hello") == ["hello"]
    assert chunk("") == []
    assert chunk("  \n ") == []


def test_every_piece_fits_and_none_is_empty():
    text = "\n\n".join(f"paragraph {i} " + "word " * 60 for i in range(40))
    pieces = chunk(text, 500)
    assert len(pieces) > 1
    assert all(0 < len(p) <= 500 for p in pieces)


def test_cuts_prefer_paragraph_breaks_then_lines():
    a, b = "a" * 30, "b" * 30
    assert chunk(f"{a}\n\n{b}", 70) == [f"{a}\n\n{b}"]
    assert chunk(f"{a}\n\n{b}", 50) == [a, b]
    assert chunk(f"{a}\n{b}", 50) == [a, b]


def test_a_single_very_long_line_is_hard_cut_and_loses_nothing():
    text = "x" * 5000
    pieces = chunk(text, 2000)
    assert all(len(p) <= 2000 for p in pieces)
    assert "".join(pieces) == text


def test_a_fence_cut_in_the_middle_is_closed_and_reopened_with_its_language():
    code = "\n".join(f"line_{i} = {i}" for i in range(200))
    pieces = chunk(f"intro\n\n```python\n{code}\n```\n\noutro", 300)
    assert len(pieces) > 2
    assert all(len(p) <= 300 for p in pieces)
    assert all(_fence_balanced(p) for p in pieces)
    assert all(p.startswith("```python\n") for p in pieces[1:-1])
    assert pieces[-1].endswith("outro")


def test_an_untagged_fence_is_reopened_untagged():
    code = "\n".join("y" * 40 for _ in range(30))
    pieces = chunk(f"```\n{code}\n```", 200)
    assert all(_fence_balanced(p) for p in pieces)
    assert all(p.startswith("```\n") for p in pieces[1:])


def test_a_fence_that_closes_inside_a_piece_is_not_reopened():
    text = "```\nshort\n```\n\n" + "tail " * 200
    pieces = chunk(text, 300)
    assert pieces[1:] and not any(p.startswith("```") for p in pieces[1:])


def test_a_limit_too_small_for_a_fence_is_refused():
    with pytest.raises(ValueError):
        _ = chunk("text", 10)


def test_a_button_id_round_trips():
    press = parse_custom_id(custom_id(APPROVE, "run-1", "abc123"))
    assert press is not None
    assert (press.approve, press.run_id, press.token) == (True, "run-1", "abc123")
    deny = parse_custom_id(custom_id(DENY, "run-1", "abc123"))
    assert deny is not None and deny.approve is False


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "heph",
        "heph:approve",
        "heph:approve:run",
        "heph:maybe:run:tok",
        "x:approve:run:tok",
        "heph:approve::tok",
        "heph:approve:run:",
        "heph:approve:run:tok:extra",
    ],
)
def test_a_foreign_or_malformed_button_id_is_ignored(raw: str):
    assert parse_custom_id(raw) is None


def test_the_approval_message_lists_each_call_and_flags_questions():
    request = ApprovalRequest(
        run_id="r",
        conversation_id="c",
        calls=(
            PendingCall("1", "shell", "run a command", "rm -rf ```x```"),
            PendingCall("2", "mail_send", "send an email", None),
        ),
        token="t",
        questions=1,
    )
    text = approval_text(request)
    assert "run a command" in text and "send an email" in text
    assert "rm -rf" in text and "```x```" not in text
    assert "answered in the app" in text
    assert len(text) <= 2000


async def test_switching_discord_on_exposes_its_routes_and_off_withdraws_them():
    async with client_app() as (client, _app):
        assert (await client.get("/discord/status")).status_code == 404

        on = await client.put("/plugins/builtin/discord", json={"enabled": True})
        assert on.status_code == 200
        status = await client.get("/discord/status")
        assert status.status_code == 200
        body = json_body(status)
        assert body["configured"] is False
        assert body["connected"] is False
        assert body["bot_user"] is None

        off = await client.put("/plugins/builtin/discord", json={"enabled": False})
        assert off.status_code == 200
        assert (await client.get("/discord/status")).status_code == 404


async def test_the_config_round_trips_and_rejects_non_numeric_ids():
    async with client_app() as (client, _app):
        _ = await client.put("/plugins/builtin/discord", json={"enabled": True})
        saved = await client.put(
            "/discord/config",
            json={
                "allowed_users": ["222", " 111 ", "222"],
                "allowed_channels": ["333"],
                "approvals_to_dm": True,
            },
        )
        assert saved.status_code == 200
        body = json_body(await client.get("/discord/status"))
        assert body["allowed_users"] == ["111", "222"]
        assert body["allowed_channels"] == ["333"]
        assert body["approvals_to_dm"] is True

        for bad_users, bad_channels in ((["not-an-id"], []), ([], ["12x"])):
            payload = {
                "allowed_users": bad_users,
                "allowed_channels": bad_channels,
                "approvals_to_dm": False,
            }
            assert (await client.put("/discord/config", json=payload)).status_code == 422


async def test_the_bot_token_is_a_declared_service_key():
    async with client_app() as (client, _app):
        listed = await client.get("/credentials")
        assert "discord_bot" in {s(obj(row)["service"]) for row in json_list(listed)}
