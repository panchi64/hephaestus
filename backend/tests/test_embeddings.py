"""The embedding capability's client lifetime.

`RegistryEmbedder.embed` sits on the hottest path in the system — once per stored
conversation turn, once per memory write, once per recall — and it builds a transient
`AsyncOpenAI` per call. A client that isn't closed strands an httpx connection pool and
its sockets, so "was it closed on *this* path" is the property worth pinning, on the
failure paths above all: those are the ones that repeat while nothing works.

The role→spec resolution itself is covered in `test_registry.py`; here the registry is a
stub so the test is about the client and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import override

import httpx2
import pytest
from openai import APIConnectionError

from core.db import make_engine
from core.exceptions import DegradedCapabilityError
from core.vault import Vault
from services import embeddings, llm
from services.registry import ModelRegistry

OWNER = "operator"
SPEC = llm.EndpointSpec(base_url="http://127.0.0.1:9/v1", model="embed-model", api_key=None)


class _StubRegistry(ModelRegistry):
    """Resolves straight to `SPEC` — the registry's own resolution is tested elsewhere."""

    def __init__(self) -> None:
        super().__init__(make_engine("sqlite:///:memory:"), Vault(Path("unused-keyfile.json")))

    @override
    async def resolve_embedding_spec(self, owner_id: str) -> llm.EndpointSpec:
        return SPEC


class _RecordingClient:
    """Stands in for `AsyncOpenAI`, recording whether this instance was ever closed.

    Only the surface `embed` touches: the async-context protocol and
    ``client.embeddings.create``.
    """

    def __init__(self, *, base_url: str, api_key: str, fail: Exception | None) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.closed = False
        self.calls: list[tuple[str, list[str]]] = []
        self._fail = fail
        self.embeddings = _RecordingEmbeddings(self)

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.closed = True


class _RecordingEmbeddings:
    def __init__(self, client: _RecordingClient) -> None:
        self._client = client

    async def create(self, *, model: str, input: list[str]):  # noqa: A002 — the SDK's name
        self._client.calls.append((model, list(input)))
        if self._client._fail is not None:
            raise self._client._fail
        return _FakeResponse([[0.1, 0.2, 0.3] for _ in input])


class _FakeVector:
    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _FakeResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_FakeVector(vector) for vector in vectors]


def _install_client(monkeypatch, *, fail: Exception | None = None) -> list[_RecordingClient]:
    """Swap the module's `AsyncOpenAI` for the recorder, returning the list every
    constructed client lands in."""
    made: list[_RecordingClient] = []

    def factory(*, base_url: str, api_key: str) -> _RecordingClient:
        client = _RecordingClient(base_url=base_url, api_key=api_key, fail=fail)
        made.append(client)
        return client

    monkeypatch.setattr(embeddings, "AsyncOpenAI", factory)
    return made


async def test_the_client_is_closed_after_a_successful_embed(monkeypatch):
    made = _install_client(monkeypatch)
    embedder = embeddings.RegistryEmbedder(_StubRegistry())

    batch = await embedder.embed(OWNER, ["hello", "world"])

    assert (batch.model, batch.dim, len(batch.vectors)) == ("embed-model", 3, 2)
    assert [client.closed for client in made] == [True]
    # A keyless local server still gets a placeholder header it ignores — the OpenAI
    # client refuses a None key outright.
    assert made[0].api_key == "unused"


async def test_the_client_is_closed_when_the_endpoint_is_unreachable(monkeypatch):
    """The path that actually leaked: a local model server that isn't running is the
    ordinary case, and it repeats on every persisted turn. Leaking a connection pool per
    turn is how a workspace runs out of sockets while nothing appears to be happening."""
    request = httpx2.Request("POST", "http://127.0.0.1:9/v1/embeddings")
    made = _install_client(monkeypatch, fail=APIConnectionError(request=request))
    embedder = embeddings.RegistryEmbedder(_StubRegistry())

    with pytest.raises(DegradedCapabilityError, match="127.0.0.1:9"):
        await embedder.embed(OWNER, ["hello"])

    assert [client.closed for client in made] == [True]


async def test_the_client_is_closed_when_the_server_errors(monkeypatch):
    """A 5xx/timeout isn't caught here — it propagates to the caller, who degrades. The
    client still has to be closed on the way out."""
    made = _install_client(monkeypatch, fail=RuntimeError("the server fell over"))
    embedder = embeddings.RegistryEmbedder(_StubRegistry())

    with pytest.raises(RuntimeError):
        await embedder.embed(OWNER, ["hello"])

    assert [client.closed for client in made] == [True]


async def test_every_call_closes_its_own_client(monkeypatch):
    """The client is per-call by design — the spec is re-resolved each time and can change
    the moment the operator rebinds the embedding role. What must not vary is that each
    one is closed, so repeated embedding leaves nothing behind."""
    made = _install_client(monkeypatch)
    embedder = embeddings.RegistryEmbedder(_StubRegistry())

    for _ in range(3):
        await embedder.embed(OWNER, ["hello"])

    assert len(made) == 3
    assert all(client.closed for client in made)
