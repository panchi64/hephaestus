"""How long to wait before retry number *n*.

Two retriers in the backend independently spelled out ``base * 2 ** (attempt - 1)``: the
write-behind worker draining a queue, and the model downloader re-running a failed pull.
The formula is short enough to retype, which is exactly why it drifted out of one place —
and it is the natural home for anything that later applies to *all* retries here (a
ceiling, jitter, a different curve), so it should only exist once.

No jitter, deliberately. Jitter exists to decorrelate many clients retrying against one
service; this is a single-operator host where each retrier is the only one on its queue,
so a random delay would buy nothing and make a failing path harder to reason about.
"""

from __future__ import annotations


def backoff_delay(base_s: float, attempt: int, *, cap_s: float | None = None) -> float:
    """Seconds to wait before ``attempt``, counting from 1 (so the first wait is ``base_s``).

    ``cap_s`` bounds the doubling; without it a generous attempt budget eventually sleeps
    for a very long time on a permanently broken dependency.
    """
    delay = base_s * 2.0 ** (max(1, attempt) - 1)
    return delay if cap_s is None else min(delay, cap_s)
