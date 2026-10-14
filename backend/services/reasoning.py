"""Disabling model "thinking" across providers — the one place that knows how.

**Prefer Pydantic AI's own lever.** Its :class:`~pydantic_ai.settings.ModelSettings`
has a unified, provider-agnostic ``thinking`` field (``False`` to disable, or an
effort level), and each model class translates it to that provider's request shape
— OpenAI's ``reasoning_effort``, Anthropic's thinking budget, and so on. That is
the engine doing its job; we should ride it, not re-derive ``reasoning_effort``
ourselves.

But ``thinking`` is only safe to send to a model that actually reasons (sent to a
plain chat model it can become an unsupported ``reasoning_effort`` argument), and
it does **not** cover every lever — notably Qwen-family models served over
OpenAI-compatible runtimes (vLLM/SGLang/llama.cpp) gate the ``<think>`` block on a
``chat_template_kwargs.enable_thinking`` request field the library doesn't model.

So this module is a thin **strategy** layer that decides, per model family, *which*
settings actually turn reasoning off — preferring the unified ``thinking`` field
and dropping to a provider-specific lever only for the gaps. Each strategy
recognizes a family from a :class:`ModelDescriptor`; :func:`disable_thinking`
returns the first match, or **empty settings** ("let it think") for an unrecognized
model — a safe default that never sends an argument the model would reject.

Adding support for a new model family is adding a strategy:

    from services import reasoning

    def _my_lab(d: reasoning.ModelDescriptor) -> reasoning.ModelSettings | None:
        if d.model_id.startswith("mylab-"):
            # Prefer the unified field when the provider's model class maps it…
            return {"thinking": False}
            # …or a provider lever only for what `thinking` can't express:
            # return {"extra_body": {"disable_reasoning": True}}
        return None

    reasoning.register_strategy(_my_lab)

Match **narrowly** — return ``None`` unless you are sure the descriptor is your
family — so an unrecognized model is left alone rather than handed a field it will
400 on.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic_ai.settings import ModelSettings


@dataclass(frozen=True)
class ModelDescriptor:
    """What a strategy matches on. ``model_id`` is the provider-served id (e.g.
    ``"qwen3:8b"``, ``"gpt-5.1"``), ``base_url`` is the endpoint it lives on (some
    levers are runtime-specific), and ``thinking`` is the operator's declared hint
    that the endpoint is a reasoning model."""

    model_id: str
    base_url: str | None = None
    thinking: bool = False


# A strategy recognizes a family and returns the settings that disable its
# thinking, or ``None`` when the descriptor isn't its family.
ThinkingStrategy = Callable[[ModelDescriptor], "ModelSettings | None"]


def _qwen(descriptor: ModelDescriptor) -> ModelSettings | None:
    """Qwen3 and kin served over OpenAI-compatible runtimes (vLLM, SGLang,
    llama.cpp, LM Studio): the chat template gates the ``<think>`` block on an
    ``enable_thinking`` flag, surfaced to the request as a ``chat_template_kwargs``
    field. This is a genuine gap — Pydantic AI's unified ``thinking`` setting does
    not express it — so we reach for ``extra_body`` here (and only here)."""
    if "qwen" in descriptor.model_id.lower():
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    return None


# OpenAI reasoning families: the o-series and the GPT-5 line onwards. Plain GPT-4o/4.1
# etc. are NOT reasoning models and must not match — sending them a reasoning arg 400s.
# This is a list that goes stale silently: a family added after it was last touched gets
# no setting rather than an error, so a new GPT-6 endpoint would simply keep thinking on
# every background title and verification until someone noticed the bill.
_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5", "gpt-6")


def _openai_reasoning(descriptor: ModelDescriptor) -> ModelSettings | None:
    """OpenAI reasoning models (o-series, GPT-5). We set the **unified** ``thinking``
    field and let Pydantic AI's ``OpenAIChatModel`` translate it to
    ``reasoning_effort`` — riding the library rather than hard-coding the provider
    key. ``"low"`` floors the budget (these models always reason a little, and only
    GPT-5.1+ accepts ``"none"``), so it is the portable minimum the whole family
    honors. We gate on the model id so a non-reasoning model is never sent the
    setting at all."""
    # Take the last path segment so a gateway prefix like ``openai/gpt-5.1``
    # resolves; do NOT split on ``.`` — that would mangle the ``gpt-5.1`` version.
    name = descriptor.model_id.lower().rsplit("/", 1)[-1]
    if name.startswith(_OPENAI_REASONING_PREFIXES):
        return {"thinking": "low"}
    return None


_BUILTIN_STRATEGIES: tuple[ThinkingStrategy, ...] = (_qwen, _openai_reasoning)
strategies: list[ThinkingStrategy] = list(_BUILTIN_STRATEGIES)


def register_strategy(strategy: ThinkingStrategy, *, first: bool = False) -> None:
    """Register a thinking-off strategy. ``first=True`` gives it priority over the
    built-ins (useful to override a built-in for a specific deployment)."""
    if first:
        strategies.insert(0, strategy)
    else:
        strategies.append(strategy)


def disable_thinking(descriptor: ModelDescriptor) -> ModelSettings:
    """The settings that turn off ``descriptor``'s reasoning, or ``{}`` when no
    strategy recognizes it (left to think — the safe default that never sends a
    rejected argument)."""
    for strategy in strategies:
        settings = strategy(descriptor)
        if settings is not None:
            return settings
    return {}
