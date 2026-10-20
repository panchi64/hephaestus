"""Goal-aware distillation of an oversized fetched page.

When a page's body exceeds the fetch output cap, blind truncation keeps only the head — but
the model usually knows what it's looking for. Given a ``goal``, :class:`WebDistiller` walks
the page in windows and asks the utility model to keep only the goal-relevant passages
(verbatim figures/tables preserved), so the capped result is the *relevant* part of a long
page rather than its first N characters. Truncation + offset paging remains the fallback:
any timeout, model error, or all-empty result returns ``None`` and the caller truncates.

The distiller builds a bare ``pydantic_ai.Agent`` directly — services must not import the
engine layer — over a model resolved by an injected callable (a closure over the registry's
background-model rule, wired in ``app.py``). Its output is still web-derived, so the caller
wraps it untrusted exactly like raw content.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from core.text import tokens_to_chars, truncate_on_boundary

logger = logging.getLogger(__name__)

# A resolver that yields the utility model + its (reasoning-off) settings, fresh per call.
ResolveModel = Callable[[], Awaitable[tuple[Model, ModelSettings | None]]]

_NO_CONTENT = "NO RELEVANT CONTENT"


class WebDistiller:
    """Distill an oversized page body down to what's relevant to a stated goal."""

    def __init__(
        self,
        *,
        resolve_model: ResolveModel,
        instructions: str,
        window_tokens: int,
        max_windows: int,
        timeout_s: float,
    ) -> None:
        self._resolve_model: ResolveModel = resolve_model
        self._instructions: str = instructions
        self._window_tokens: int = window_tokens
        self._max_windows: int = max_windows
        self._timeout_s: float = timeout_s

    async def distill(self, body: str, *, goal: str, url: str) -> str | None:
        """Return the goal-relevant distillation of ``body``, or ``None`` to signal the
        caller to fall back to truncation. Never raises — a timeout, model error, or
        all-irrelevant result is a ``None``, logged once at warning."""
        try:
            return await asyncio.wait_for(self._distill(body, goal=goal), self._timeout_s)
        except Exception:  # timeout/model error ⇒ degrade, never raise
            logger.warning(
                "web distillation failed for %r; falling back to truncation", url, exc_info=True
            )
            return None

    async def _distill(self, body: str, *, goal: str) -> str | None:
        model, settings = await self._resolve_model()
        agent = Agent(model, output_type=str, instructions=self._instructions)
        windows, covered = self._windows(body)
        pieces: list[str] = []
        for window in windows:  # sequential: a local utility model serves one request well
            result = await agent.run(f"GOAL: {goal}\n\nEXCERPT:\n{window}", model_settings=settings)
            text = (result.output or "").strip()
            if not text or text.rstrip(".").strip().upper() == _NO_CONTENT:
                continue
            pieces.append(text)
        if not pieces:
            return None
        digest = "\n\n".join(pieces)
        if covered < len(body):
            digest += (
                f"\n\n[Distillation covered the first {covered} of {len(body)} "
                "characters of the page.]"
            )
        return digest

    def _windows(self, body: str) -> tuple[list[str], int]:
        """Split ``body`` into up to ``max_windows`` consecutive windows on clean word
        boundaries. Returns the windows and the number of characters actually covered."""
        window_chars = tokens_to_chars(self._window_tokens)
        windows: list[str] = []
        pos = 0
        while pos < len(body) and len(windows) < self._max_windows:
            chunk = truncate_on_boundary(body[pos:], window_chars)
            if not chunk:
                break
            windows.append(chunk)
            pos += len(chunk)
        return windows, pos
