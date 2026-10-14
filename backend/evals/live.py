"""Env-backed live handles — a real embedder and a real chat model, no registry/DB.

The model registry stores endpoints encrypted under ``data/`` (off limits to this
suite), so the evals do **not** read the operator's registry. Instead they build
their handles directly from eval-only env vars, fully decoupled from ``data/``:

- ``HEPH_EVAL_EMBED_BASE_URL`` / ``HEPH_EVAL_EMBED_MODEL`` / ``HEPH_EVAL_EMBED_KEY``
- ``HEPH_EVAL_CHAT_BASE_URL`` / ``HEPH_EVAL_CHAT_MODEL`` / ``HEPH_EVAL_CHAT_KEY``

``EnvEmbedder`` satisfies the :class:`~services.embeddings.Embedder` protocol and
calls the exact same OpenAI-compatible ``/embeddings`` path as
``RegistryEmbedder.embed`` — the retrieval quality under test lives in
``memory._rank`` + ``services.ranking``, which is identical regardless of which
embedder object feeds it, so this thin adapter is faithful and avoids the DB.

``build_chat_model`` constructs a single Pydantic AI model from an env
:class:`~services.llm.EndpointSpec` via ``services.llm.build_model`` (the same
builder the registry uses), passed as the ``model=`` override to
``build_chat_orchestrator``.

Note: ``services/embeddings.py`` imports ``ModelRegistry`` at module top today.
We import only the protocol/dataclass (``Embedder``, ``EmbeddingBatch``) from it,
which resolves regardless of whether that import is later moved under
``TYPE_CHECKING``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import override

from openai import AsyncOpenAI
from pydantic_ai.models import Model

from services.embeddings import Embedder, EmbeddingBatch
from services.endpoint_spec import EndpointSpec
from services.llm import build_model

# The six env vars that gate the whole suite. All-present ⇒ the live tests run.
EMBED_ENV = ("HEPH_EVAL_EMBED_BASE_URL", "HEPH_EVAL_EMBED_MODEL", "HEPH_EVAL_EMBED_KEY")
CHAT_ENV = ("HEPH_EVAL_CHAT_BASE_URL", "HEPH_EVAL_CHAT_MODEL", "HEPH_EVAL_CHAT_KEY")
REQUIRED_ENV = (*EMBED_ENV, *CHAT_ENV)


def missing_env() -> list[str]:
    """The required env vars that are unset/empty — empty list ⇒ ready to run."""
    return [name for name in REQUIRED_ENV if not os.environ.get(name)]


@dataclass(frozen=True)
class EnvEmbedder(Embedder):
    """An :class:`~services.embeddings.Embedder` backed by an env endpoint.

    Mirrors ``RegistryEmbedder.embed`` exactly — same ``AsyncOpenAI`` client,
    same ``embeddings.create`` call — but resolves its spec from env instead of
    the encrypted registry. Always available (a live endpoint, no degrade seam):
    a real failure surfaces loudly rather than silently degrading, which the
    preflight fixture relies on.
    """

    base_url: str
    model: str
    api_key: str

    @classmethod
    def from_env(cls) -> EnvEmbedder:
        return cls(
            base_url=os.environ["HEPH_EVAL_EMBED_BASE_URL"],
            model=os.environ["HEPH_EVAL_EMBED_MODEL"],
            api_key=os.environ["HEPH_EVAL_EMBED_KEY"],
        )

    @override
    async def is_available(self, owner_id: str) -> bool:
        return True

    @override
    async def embed(self, owner_id: str, texts: list[str]) -> EmbeddingBatch:
        client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key or "not-needed")
        response = await client.embeddings.create(model=self.model, input=texts)
        vectors = [item.embedding for item in response.data]
        dim = len(vectors[0]) if vectors else 0
        return EmbeddingBatch(vectors=vectors, model=self.model, dim=dim)


def chat_spec_from_env() -> EndpointSpec:
    """The env chat endpoint as an :class:`~services.llm.EndpointSpec`."""
    return EndpointSpec(
        base_url=os.environ["HEPH_EVAL_CHAT_BASE_URL"],
        model=os.environ["HEPH_EVAL_CHAT_MODEL"],
        api_key=os.environ.get("HEPH_EVAL_CHAT_KEY") or "not-needed",
    )


def build_chat_model() -> Model:
    """Build a single Pydantic AI chat model from the env endpoint.

    Uses the same ``services.llm.build_model`` the registry uses; the registry
    additionally wraps chains in ``FallbackModel``, but the eval points at one
    endpoint so a plain model is the faithful single-endpoint case.
    """
    return build_model(chat_spec_from_env())
