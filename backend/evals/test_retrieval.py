"""Part A — retrieval-quality ablation for both embedding consumers.

The LLM-free answer to "are the search embeddings useful?". For each consumer
(``MemoryStore.recall`` and ``ConversationSearch.search``) we embed the corpus
once via the live :class:`EnvEmbedder`, store it through the real write path, and
then compute three rankings over the **same** embedded set, reusing the shared
``services.ranking`` primitives (``cosine`` / ``tokens`` / ``rrf`` / ``matched_by``
— the exact module both consumers import):

- **hybrid** — the production path (the real ``recall`` / ``search``),
- **dense-only** — cosine ranking over the embeddings alone,
- **sparse-only** — token-overlap ranking alone.

Metrics (recall@1, recall@5, MRR) are reported overall and per slice. The
assertions encode "embeddings earn their keep": on the *paraphrase* slice
dense/hybrid must beat sparse by a clear margin (sparse near-zero, since the query
shares no tokens with its gold item); on the *rare_token* slice hybrid must not
regress below sparse (the keyword path still carries exact ids/codes).
"""

from __future__ import annotations

import numpy as np

from evals.dataset import Corpus, RetrievalQuery, conversation_corpus, memory_corpus
from evals.live import EnvEmbedder
from services import ranking
from services.conversation_search import ConversationSearch
from services.conversations import ConversationStore
from services.memory import MemoryStore

OWNER = "operator"


# --- ranking variants (all over the one embedded set, via services.ranking) ----


def _dense_ranking(query_vec: np.ndarray, doc_vecs: dict[str, np.ndarray]) -> list[str]:
    scores = {doc_id: ranking.cosine(query_vec, vec) for doc_id, vec in doc_vecs.items()}
    scores = {doc_id: s for doc_id, s in scores.items() if s > 0}
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]


def _sparse_ranking(query: str, doc_texts: dict[str, str]) -> list[str]:
    q = ranking.tokens(query)
    scores = {doc_id: float(len(q & ranking.tokens(text))) for doc_id, text in doc_texts.items()}
    scores = {doc_id: s for doc_id, s in scores.items() if s > 0}
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]


# --- metrics ------------------------------------------------------------------


def _rank_of_gold(ranked: list[str], gold: set[str]) -> int | None:
    for i, doc_id in enumerate(ranked, start=1):
        if doc_id in gold:
            return i
    return None


def metrics(rankings: list[tuple[list[str], set[str]]]) -> dict[str, float]:
    """recall@1, recall@5, MRR over a list of (ranked_ids, gold_ids)."""
    if not rankings:
        return {"recall@1": 0.0, "recall@5": 0.0, "mrr": 0.0, "n": 0}
    r1 = r5 = mrr = 0.0
    for ranked, gold in rankings:
        pos = _rank_of_gold(ranked, gold)
        if pos is not None:
            r1 += 1.0 if pos == 1 else 0.0
            r5 += 1.0 if pos <= 5 else 0.0
            mrr += 1.0 / pos
    n = len(rankings)
    return {"recall@1": r1 / n, "recall@5": r5 / n, "mrr": mrr / n, "n": n}


# --- embedding the corpus once ------------------------------------------------


async def _embed_docs(embedder: EnvEmbedder, doc_texts: dict[str, str]) -> dict[str, np.ndarray]:
    ids = list(doc_texts)
    batch = await embedder.embed(OWNER, [doc_texts[i] for i in ids])
    return {i: np.asarray(v, dtype=np.float64) for i, v in zip(ids, batch.vectors, strict=True)}


async def run_ablation(
    embedder: EnvEmbedder,
    doc_texts: dict[str, str],
    queries: list[RetrievalQuery],
    hybrid_ids: dict[str, list[str]],
) -> dict[str, dict[str, dict[str, float]]]:
    """Compute hybrid/dense/sparse metrics, overall and per slice.

    ``hybrid_ids`` maps each query to the production system's ranked gold-space ids
    (from the real ``recall``/``search``); dense/sparse are computed here over the
    same embedded corpus. Returns ``{variant: {slice_or_overall: metrics}}``.
    """
    doc_vecs = await _embed_docs(embedder, doc_texts)
    query_vecs = await _embed_docs(embedder, {q.query: q.query for q in queries})

    rows: dict[str, list[tuple[str, list[str], set[str]]]] = {
        "hybrid": [],
        "dense": [],
        "sparse": [],
    }
    for q in queries:
        gold = set(q.gold_ids)
        qvec = query_vecs[q.query]
        rows["hybrid"].append((q.slice, hybrid_ids[q.query], gold))
        rows["dense"].append((q.slice, _dense_ranking(qvec, doc_vecs), gold))
        rows["sparse"].append((q.slice, _sparse_ranking(q.query, doc_texts), gold))

    slices = sorted({q.slice for q in queries})
    out: dict[str, dict[str, dict[str, float]]] = {}
    for variant, entries in rows.items():
        out[variant] = {
            "overall": metrics([(r, g) for _s, r, g in entries]),
        }
        for sl in slices:
            out[variant][sl] = metrics([(r, g) for s, r, g in entries if s == sl])
    return out


# --- consumer seeding + hybrid capture ----------------------------------------


async def memory_hybrid(store: MemoryStore, corpus: Corpus) -> dict[str, list[str]]:
    """Seed the memory corpus through ``remember`` and capture the real ``recall``
    ranking (mapped back to memory ids) for every query."""
    for m in corpus.memories:
        _ = await store.remember(OWNER, m.content)
    # Recover content→id so a recall hit (which carries decrypted content) maps back
    # to the gold id without reaching into the store internals.
    id_by_content = {m.content: m.id for m in corpus.memories}
    hybrid: dict[str, list[str]] = {}
    for q in corpus.queries:
        hits = await store.recall(OWNER, q.query, limit=len(corpus.memories))
        hybrid[q.query] = [
            id_by_content[h.memory.content] for h in hits if h.memory.content in id_by_content
        ]
    return hybrid


async def conversation_hybrid(
    store: ConversationStore, search: ConversationSearch, corpus: Corpus
) -> dict[str, list[str]]:
    """Seed the conversation corpus through the persistence path (so the drainer
    embeds each turn) and capture the real ``search`` ranking by conversation id."""
    from pydantic_ai import ModelRequest, ModelResponse, TextPart, UserPromptPart

    title_to_gold = {c.title: c.id for c in corpus.conversations}
    real_to_gold: dict[str, str] = {}
    for c in corpus.conversations:
        cid = await store.create_conversation(OWNER, title=c.title)
        real_to_gold[cid] = c.id
        for prompt, answer in c.turns:
            _ = store.record(
                cid,
                [
                    ModelRequest(parts=[UserPromptPart(content=prompt)]),
                    ModelResponse(parts=[TextPart(content=answer)], model_name="m"),
                ],
            )
    await store._worker.join()  # flush the drainer so rows + vectors land
    assert title_to_gold  # titles are unique gold anchors

    hybrid: dict[str, list[str]] = {}
    for q in corpus.queries:
        hits = await search.search(OWNER, q.query, limit=len(corpus.conversations))
        hybrid[q.query] = [
            real_to_gold[h.conversation_id] for h in hits if h.conversation_id in real_to_gold
        ]
    return hybrid


def memory_texts(corpus: Corpus) -> dict[str, str]:
    return {m.id: m.content for m in corpus.memories}


def conversation_texts(corpus: Corpus) -> dict[str, str]:
    # The per-message vectors the drainer stores are one per turn-text; for the
    # offline ablation we score against the joined conversation text (the unit the
    # search collapses to). Faithful for recall@k at the conversation grain.
    return {c.id: " ".join(f"{p} {a}" for p, a in c.turns) for c in corpus.conversations}


# --- assertions on the per-consumer ablation ----------------------------------


def _assert_embeddings_earn_keep(table: dict[str, dict[str, dict[str, float]]]) -> None:
    para_sparse = table["sparse"].get("paraphrase", {})
    para_dense = table["dense"].get("paraphrase", {})
    para_hybrid = table["hybrid"].get("paraphrase", {})
    if para_sparse.get("n"):
        # The paraphrase slice shares no tokens with its gold item: sparse is blind
        # there, dense/hybrid must see it.
        assert para_sparse["recall@5"] <= 0.1, (
            f"sparse should be near-blind on paraphrases, got {para_sparse['recall@5']:.2f}"
        )
        assert para_dense["recall@5"] >= para_sparse["recall@5"] + 0.5, (
            "dense must clearly beat sparse on the paraphrase slice "
            f"({para_dense['recall@5']:.2f} vs {para_sparse['recall@5']:.2f})"
        )
        assert para_hybrid["recall@5"] >= para_sparse["recall@5"] + 0.5, (
            "hybrid must clearly beat sparse on the paraphrase slice "
            f"({para_hybrid['recall@5']:.2f} vs {para_sparse['recall@5']:.2f})"
        )

    rare_sparse = table["sparse"].get("rare_token", {})
    rare_hybrid = table["hybrid"].get("rare_token", {})
    if rare_sparse.get("n"):
        # Exact ids/codes: hybrid must not lose the items the keyword path catches.
        assert rare_hybrid["recall@5"] >= rare_sparse["recall@5"] - 1e-9, (
            "hybrid regressed below sparse on the rare-token slice "
            f"({rare_hybrid['recall@5']:.2f} vs {rare_sparse['recall@5']:.2f})"
        )


# --- tests --------------------------------------------------------------------


async def test_memory_retrieval_ablation(embedder: EnvEmbedder, memory_store: MemoryStore) -> None:
    corpus = memory_corpus()
    hybrid = await memory_hybrid(memory_store, corpus)
    table = await run_ablation(embedder, memory_texts(corpus), corpus.queries, hybrid)
    _assert_embeddings_earn_keep(table)


async def test_conversation_retrieval_ablation(
    embedder: EnvEmbedder, conversation_search: tuple[ConversationStore, ConversationSearch]
) -> None:
    store, search = conversation_search
    corpus = conversation_corpus()
    hybrid = await conversation_hybrid(store, search, corpus)
    table = await run_ablation(embedder, conversation_texts(corpus), corpus.queries, hybrid)
    _assert_embeddings_earn_keep(table)
