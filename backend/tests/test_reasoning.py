"""Per-provider thinking-off strategies and the extensible registry."""

from __future__ import annotations

from pydantic_ai.settings import ModelSettings

from services import reasoning
from services.reasoning import ModelDescriptor, disable_thinking


def test_qwen_family_disables_via_chat_template_kwargs():
    settings = disable_thinking(ModelDescriptor(model_id="qwen3:8b"))
    assert settings == {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}


def test_qwen_match_is_case_insensitive_and_substring():
    assert disable_thinking(ModelDescriptor(model_id="Qwen2.5-72B-Instruct"))


def test_openai_reasoning_models_use_unified_thinking_field():
    # We ride Pydantic AI's provider-agnostic `thinking` setting (it translates to
    # OpenAI's reasoning_effort) rather than hard-coding the provider key.
    for model_id in ("gpt-5.1", "o1-mini", "o3", "o4-mini", "openai/gpt-5"):
        settings = disable_thinking(ModelDescriptor(model_id=model_id))
        assert settings == {"thinking": "low"}, model_id


def test_non_reasoning_openai_models_get_no_settings():
    # gpt-4o is not a reasoning model — sending it a reasoning arg would 400, so
    # the strategy must NOT match. An unknown model is left to reason normally.
    assert disable_thinking(ModelDescriptor(model_id="gpt-4o")) == {}
    assert disable_thinking(ModelDescriptor(model_id="llama-3.1-70b")) == {}


def test_unknown_model_returns_empty_settings():
    assert disable_thinking(ModelDescriptor(model_id="some-future-model")) == {}


def test_register_strategy_extends_the_registry():
    sentinel: ModelSettings = {"extra_body": {"disable_reasoning": True}}

    def mylab(d: ModelDescriptor) -> ModelSettings | None:
        return sentinel if d.model_id.startswith("mylab-") else None

    original = list(reasoning.strategies)
    try:
        reasoning.register_strategy(mylab)
        assert disable_thinking(ModelDescriptor(model_id="mylab-7b")) == sentinel
        # Unrelated models still fall through to the built-ins / empty default.
        assert disable_thinking(ModelDescriptor(model_id="gpt-4o")) == {}
    finally:
        reasoning.strategies[:] = original


def test_register_first_takes_priority_over_builtins():
    override: ModelSettings = {"extra_body": {"custom": True}}

    def qwen_override(d: ModelDescriptor) -> ModelSettings | None:
        return override if "qwen" in d.model_id.lower() else None

    original = list(reasoning.strategies)
    try:
        reasoning.register_strategy(qwen_override, first=True)
        assert disable_thinking(ModelDescriptor(model_id="qwen3")) == override
    finally:
        reasoning.strategies[:] = original
