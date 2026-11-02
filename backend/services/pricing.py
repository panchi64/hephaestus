"""List-price lookup from the bundled genai-prices snapshot, matched by provider URL only."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from genai_prices import calc_price
from pydantic_ai.usage import RequestUsage


@dataclass(frozen=True, slots=True)
class Cost:
    """What one response cost, and what caching saved against sending its prompt uncached
    (negative when the cache writes cost more than the reads saved)."""

    total_usd: float
    saved_usd: float


def price_usage(
    usage: RequestUsage,
    model_name: str | None,
    provider_url: str | None,
    timestamp: datetime | None,
) -> Cost | None:
    """The price of one request, or None when it can't be priced: no usage, or a provider
    URL the snapshot doesn't know.

    Matched by URL only, never by provider name: an OpenAI-compatible local server reports
    the provider ``openai``, and a model it serves under a hosted model's name would
    otherwise be billed at that model's price."""
    if not model_name or not provider_url or not usage.input_tokens:
        return None
    if usage.unmeasured_requests:
        return None
    uncached = replace(usage, cache_read_tokens=0, cache_write_tokens=0, cache_write_1h_tokens=0)
    try:
        actual, baseline = (
            calc_price(
                each,
                model_name,
                provider_api_url=provider_url,
                genai_request_timestamp=timestamp,
            )
            for each in (usage, uncached)
        )
    except LookupError, ValueError:
        return None
    return Cost(
        total_usd=float(actual.total_price),
        saved_usd=float(baseline.total_price - actual.total_price),
    )
