"""The embedding reindex coordinator: heal counts, model passthrough, degrade paths."""

from __future__ import annotations

from core.exceptions import DegradedCapabilityError, NotFoundError
from services import llm
from services.reindex import EmbeddingReindexer

OWNER = "operator"


class _Registry:
    def __init__(
        self, *, spec: llm.EndpointSpec | None = None, exc: Exception | None = None
    ) -> None:
        self._spec = spec
        self._exc = exc

    async def resolve_embedding_spec(self, owner_id: str) -> llm.EndpointSpec:
        if self._exc is not None:
            raise self._exc
        assert self._spec is not None
        return self._spec


class _Memory:
    def __init__(self, n: int = 0) -> None:
        self._n = n
        self.seen_model: str | None = None

    async def reembed(
        self, owner_id: str, *, current_model: str | None = None, batch_size: int = 64
    ) -> int:
        self.seen_model = current_model
        return self._n


class _Convos:
    def __init__(self, n: int = 0) -> None:
        self._n = n
        self.seen_model: str | None = None

    async def reindex_embeddings(
        self, owner_id: str, *, current_model: str | None = None, batch_size: int = 64
    ) -> int:
        self.seen_model = current_model
        return self._n


class _Chunks:
    def __init__(self, n: int = 0) -> None:
        self._n = n
        self.seen_model: str | None = None

    async def reembed(
        self,
        owner_id: str,
        source_id: str | None = None,
        *,
        source_kind: str | None = None,
        current_model: str | None = None,
        batch_size: int = 64,
    ) -> int:
        self.seen_model = current_model
        return self._n


async def test_reindex_reports_counts_and_passes_current_model():
    mem, conv, chunks = _Memory(3), _Convos(5), _Chunks(7)
    spec = llm.EndpointSpec(base_url="http://x/v1", model="embed-m")
    reindexer = EmbeddingReindexer(_Registry(spec=spec), mem, conv, chunks)
    await reindexer._run(OWNER)

    status = reindexer.status()
    assert status.state == "done"
    assert status.memories == 3 and status.messages == 5 and status.chunks == 7
    # Every store is healed against the currently-resolved model (EMB-2).
    assert mem.seen_model == "embed-m" and conv.seen_model == "embed-m"
    assert chunks.seen_model == "embed-m"


async def test_reindex_degrades_when_no_embedder():
    reindexer = EmbeddingReindexer(
        _Registry(exc=DegradedCapabilityError("unset")), _Memory(), _Convos(), _Chunks()
    )
    await reindexer._run(OWNER)
    assert reindexer.status().state == "degraded"


async def test_reindex_degrades_when_endpoint_deleted():
    # A NotFoundError (the endpoint vanished mid-run) must reach a terminal state,
    # not leave the status stuck at 'running' so the UI spins forever.
    reindexer = EmbeddingReindexer(
        _Registry(exc=NotFoundError("gone")), _Memory(), _Convos(), _Chunks()
    )
    await reindexer._run(OWNER)
    assert reindexer.status().state == "degraded"
