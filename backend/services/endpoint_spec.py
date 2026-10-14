"""The resolved endpoint a model is built from — a leaf shared by `llm` and the provider
adapters, which would otherwise import each other."""

from __future__ import annotations

from dataclasses import dataclass

from runs import Endpoint


@dataclass(frozen=True)
class EndpointSpec:
    """A resolved, decrypted endpoint — everything needed to build a model."""

    base_url: str
    model: str
    # Which adapter builds the model (`services/providers`). Every pre-provider
    # endpoint is OpenAI-compatible, hence the default.
    provider: str = "openai-compatible"
    # None ⇒ the server doesn't authenticate (a local engine). Never a sentinel.
    api_key: str | None = None
    context_window: int | None = None
    # The most output tokens one response may ask for, as the provider advertises it —
    # discovered, never operator-set. None ⇒ unknown, and the model library's own
    # default stands (`Provider.model_limits` has why that matters for Anthropic).
    max_output_tokens: int | None = None
    native_tools: bool = True
    vision: bool = False
    thinking: bool = False
    # The stored endpoint this spec was resolved from; None for a spec built by hand.
    endpoint_id: str | None = None
    name: str = ""
    # The operator's cap on concurrent runs, else the slot count the server reports.
    max_concurrent_runs: int | None = None
    parallel_slots: int | None = None

    def gate_endpoint(self) -> Endpoint | None:
        """This endpoint as the run gate sees it, or None when it is not a stored one."""
        if self.endpoint_id is None:
            return None
        return Endpoint(
            id=self.endpoint_id,
            name=self.name,
            capacity=self.max_concurrent_runs or self.parallel_slots,
        )
