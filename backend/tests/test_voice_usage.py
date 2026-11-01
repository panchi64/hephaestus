"""Usage read off relayed Anthropic responses, priced like the platform, and the rate window."""

from __future__ import annotations

import json

from evals.voice.cost import render_cost
from evals.voice.record import Run, Sample
from evals.voice.usage import ResponseUsage, TokenWindow, Usage

URL = "https://api.anthropic.com"


def _sse(*events: dict[str, object]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


MESSAGE: dict[str, object] = {
    "id": "m1",
    "type": "message",
    "role": "assistant",
    "content": [],
    "model": "claude-sonnet-5-5",
    "stop_reason": None,
    "stop_sequence": None,
    "usage": {
        "input_tokens": 100,
        "cache_creation_input_tokens": 1_000,
        "cache_read_input_tokens": 9_000,
        "output_tokens": 1,
    },
}
START: dict[str, object] = {"type": "message_start", "message": MESSAGE}
DELTA: dict[str, object] = {
    "type": "message_delta",
    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
    "usage": {"output_tokens": 250},
}


def test_a_stream_split_anywhere_yields_its_usage_and_price() -> None:
    started: list[int] = []
    reader = ResponseUsage(URL, stream=True, on_start=started.append)
    body = _sse(START, {"type": "ping"}, DELTA, {"type": "message_stop"})
    for i in range(0, len(body), 7):
        reader.feed(body[i : i + 7])
    read = reader.finish()
    assert read is not None
    usage, cost = read
    assert started == [10_100]
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (
        10_100,
        9_000,
        1_000,
    )
    assert usage.output_tokens == 250
    assert cost is not None and cost.total_usd > 0 and cost.saved_usd > 0


def test_a_plain_message_body_is_read_too() -> None:
    reader = ResponseUsage(URL, stream=False, on_start=lambda _: None)
    reader.feed(json.dumps({**MESSAGE, "usage": {"input_tokens": 5, "output_tokens": 2}}).encode())
    read = reader.finish()
    assert read is not None and read[0].output_tokens == 2


def test_a_response_without_anthropic_usage_counts_nothing() -> None:
    reader = ResponseUsage(URL, stream=True, on_start=lambda _: None)
    reader.feed(b'data: {"choices": []}\n\ndata: [DONE]\n\n')
    assert reader.finish() is None


def test_the_window_waits_until_the_oldest_tokens_age_out() -> None:
    now = [0.0]
    window = TokenWindow(1_000, clock=lambda: now[0])
    window.add(600)
    assert window.wait_s() == 0
    now[0] = 20.0
    window.add(500)
    assert window.wait_s() == 40.0
    now[0] = 61.0
    assert window.wait_s() == 0
    assert window.peak == 1_100


def test_the_cost_report_breaks_the_run_down() -> None:
    agent = Usage(requests=3, input_tokens=30_000, cache_read_tokens=24_000, cost_usd=0.05)
    title = Usage(requests=1, input_tokens=500, cost_usd=0.001)
    run = Run(
        group="g",
        name="A",
        started_at="now",
        prompt_fingerprint="f",
        stub=False,
        samples=[Sample(scenario="banter", sample=1, usage={"agent": agent, "utility": title})],
        helper_usage={"utility": Usage(requests=1, input_tokens=400, cost_usd=0.002)},
        peak_tokens_per_minute=30_000,
    )
    text = render_cost(run)
    assert "**$0.05** over 5 requests" in text
    assert "| `banter` | 4 | 30,500 | 79% |" in text
    assert "after the scenarios | 1 | 400 |" in text
