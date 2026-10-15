"""Pointing a dev instance at a real model instead of the stub.

The stub is right for almost everything — deterministic, free, scriptable, offline — and
wrong for the one question it cannot answer: whether a *model* handles something. A tool
description that reads ambiguously, a prompt that invites the wrong tool, an instruction
a small local model ignores; none of those show up against a fixture that says what it
was told to.

With the base URL and model set, the seeded roles point at that endpoint and the
launcher starts no stub. One of the two alone counts as neither, because a
half-configured endpoint that silently fell back to the stub would be read as the real
model behaving strangely. ``HEPH_DEV_EMBED_MODEL`` names an embedding model on the same
server; without it the embedding role is bound to the chat model, and memory and search
recall fall back to keywords.
"""

from __future__ import annotations

import os

BASE_URL = "HEPH_DEV_CHAT_BASE_URL"
MODEL = "HEPH_DEV_CHAT_MODEL"
KEY = "HEPH_DEV_CHAT_KEY"
EMBED_MODEL = "HEPH_DEV_EMBED_MODEL"

#: The key is genuinely optional — a local engine usually wants none — so it is read but
#: not required. The other two are what "an endpoint" means.
REQUIRED = (BASE_URL, MODEL)


def configured() -> bool:
    """Whether a real endpoint has been named for this instance."""
    return all(os.environ.get(name) for name in REQUIRED)


def missing() -> list[str]:
    """Which of the required variables are unset — for a message that says what to fix."""
    return [name for name in REQUIRED if not os.environ.get(name)]


def embed_model() -> str | None:
    """The embedding model on the live server, when one is named."""
    return os.environ.get(EMBED_MODEL) or None


def endpoint() -> dict[str, str | None]:
    """The endpoint to seed, assuming :func:`configured`."""
    return {
        "base_url": os.environ[BASE_URL].rstrip("/"),
        "model": os.environ[MODEL],
        "api_key": os.environ.get(KEY) or None,
    }
