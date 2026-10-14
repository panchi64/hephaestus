"""``python -m evals.report`` — run both consumers' evals and write FINDINGS.md.

The written deliverable. Runs Part A (retrieval ablation) and Part B (end-to-end
agent benefit) for both embedding consumers against the live env models, prints
the metric tables, and writes ``backend/evals/FINDINGS.md`` with:

- retrieval recall@k / MRR per variant + slice, per consumer,
- trigger precision / recall,
- grounding on-vs-off deltas (deterministic + judged),
- a plain-language verdict on each of the three failure points (trigger /
  retrieval / grounding) for each consumer.

This entry point reuses the exact functions the tests assert on, so the report and
the test suite can never drift. It requires the six ``HEPH_EVAL_*`` env vars; run it
the same way as ``uv run pytest evals/ -m live_models``.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai.models import Model

from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from evals.dataset import conversation_corpus, memory_corpus
from evals.live import EnvEmbedder, build_chat_model, missing_env
from evals.test_endtoend import (
    CONVERSATION_RETRIEVAL_TOOL,
    MEMORY_RETRIEVAL_TOOL,
    ConsumerScores,
    platform_over,
    score_consumer,
    seed_conversations,
    seed_memory,
)
from evals.test_retrieval import (
    conversation_hybrid,
    conversation_texts,
    memory_hybrid,
    memory_texts,
    run_ablation,
)
from services.conversation_search import ConversationSearch
from services.conversations import ConversationStore
from services.memory import MemoryStore
from tools.memory import memory_toolset
from tools.platform import platform_toolset

OWNER = "operator"
FINDINGS = Path(__file__).with_name("FINDINGS.md")


async def _unlocked_vault() -> Vault:
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return vault


# --- per-consumer runs --------------------------------------------------------


type Ablation = dict[str, dict[str, dict[str, float]]]


async def _memory_run(embedder: EnvEmbedder, chat: Model) -> tuple[Ablation, ConsumerScores]:
    corpus = memory_corpus()
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = await _unlocked_vault()
    store = MemoryStore(engine, vault, embedder)
    hybrid = await memory_hybrid(store, corpus)
    ablation = await run_ablation(embedder, memory_texts(corpus), corpus.queries, hybrid)
    # A fresh store for the end-to-end seeding (independent of the ablation seeding).
    e2e_engine = make_engine("sqlite:///:memory:")
    init_db(e2e_engine)
    e2e_store = MemoryStore(e2e_engine, await _unlocked_vault(), embedder)
    await seed_memory(e2e_store, corpus)
    scores = await score_consumer(
        consumer="memory",
        retrieval_tool=MEMORY_RETRIEVAL_TOOL,
        model=chat,
        categories={"memory": memory_toolset()},
        capabilities_on=ServiceContainer.of(e2e_store),
        questions=corpus.questions,
    )
    return ablation, scores


async def _conversation_run(embedder: EnvEmbedder, chat: Model) -> tuple[Ablation, ConsumerScores]:
    corpus = conversation_corpus()
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = await _unlocked_vault()
    store = ConversationStore(engine, vault, embedder)
    await store.start()
    search = ConversationSearch(engine, vault, embedder, store)
    try:
        hybrid = await conversation_hybrid(store, search, corpus)
        ablation = await run_ablation(embedder, conversation_texts(corpus), corpus.queries, hybrid)
    finally:
        await store.stop()

    e2e_engine = make_engine("sqlite:///:memory:")
    init_db(e2e_engine)
    e2e_vault = await _unlocked_vault()
    e2e_store = ConversationStore(e2e_engine, e2e_vault, embedder)
    await e2e_store.start()
    e2e_search = ConversationSearch(e2e_engine, e2e_vault, embedder, e2e_store)
    try:
        await seed_conversations(e2e_store, corpus)
        scores = await score_consumer(
            consumer="conversations",
            retrieval_tool=CONVERSATION_RETRIEVAL_TOOL,
            model=chat,
            categories={"platform": platform_toolset()},
            capabilities_on=ServiceContainer.of(platform_over(e2e_search, embedder)),
            questions=corpus.questions,
        )
    finally:
        await e2e_store.stop()
    return ablation, scores


# --- rendering ----------------------------------------------------------------


def _ablation_table(ablation: Ablation) -> list[str]:
    lines = ["| variant | slice | recall@1 | recall@5 | MRR | n |", "|---|---|---|---|---|---|"]
    for variant in ("hybrid", "dense", "sparse"):
        for slice_name, m in sorted(ablation[variant].items()):
            lines.append(
                f"| {variant} | {slice_name} | {m['recall@1']:.2f} | "
                + f"{m['recall@5']:.2f} | {m['mrr']:.2f} | {m['n']} |"
            )
    return lines


def _trigger_verdict(s: ConsumerScores) -> str:
    if s.trigger_recall >= 0.8 and s.trigger_precision >= 0.8:
        return "PASS — the agent reliably calls retrieval when needed and stays quiet otherwise."
    if s.trigger_recall < 0.6:
        return "FAIL — the agent under-triggers: it answers personal questions without recalling."
    if s.trigger_precision < 0.6:
        return "FAIL — the agent over-triggers: it wastes recall calls on generic questions."
    return "WEAK — triggering works but is not crisp; see the precision/recall numbers."


def _retrieval_verdict(ablation: Ablation) -> str:
    para_dense = ablation["dense"].get("paraphrase", {}).get("recall@5", 0.0)
    para_sparse = ablation["sparse"].get("paraphrase", {}).get("recall@5", 0.0)
    rare_hybrid = ablation["hybrid"].get("rare_token", {}).get("recall@5", 0.0)
    rare_sparse = ablation["sparse"].get("rare_token", {}).get("recall@5", 0.0)
    parts: list[str] = []
    if para_dense >= para_sparse + 0.5:
        parts.append(
            f"PASS on paraphrases — dense recall@5 {para_dense:.2f} clearly beats "
            + f"sparse {para_sparse:.2f} (embeddings earn their keep)."
        )
    else:
        parts.append(
            f"WEAK on paraphrases — dense {para_dense:.2f} did not clearly beat sparse "
            + f"{para_sparse:.2f}; the embedding model may be weak or degraded."
        )
    if rare_hybrid >= rare_sparse - 1e-9:
        parts.append(
            f"PASS on rare tokens — hybrid {rare_hybrid:.2f} holds the keyword path "
            + f"{rare_sparse:.2f}."
        )
    else:
        parts.append(
            f"FAIL on rare tokens — hybrid {rare_hybrid:.2f} regressed below sparse "
            + f"{rare_sparse:.2f}; fusion is dropping exact-id hits."
        )
    return " ".join(parts)


def _grounding_verdict(s: ConsumerScores) -> str:
    if s.grounding_delta > 0 and s.judged_delta > 0:
        return (
            "PASS — capability-on grounds better than off "
            + f"(deterministic +{s.grounding_delta:.2f}, judged +{s.judged_delta:.2f})."
        )
    return (
        "FAIL — the recalled content did not improve the answer "
        + f"(deterministic +{s.grounding_delta:.2f}, judged +{s.judged_delta:.2f}); "
        + "the model is retrieving but not using it."
    )


def _consumer_section(name: str, ablation: Ablation, scores: ConsumerScores) -> list[str]:
    lines = [f"## {name}", ""]
    lines.append("### Retrieval ablation (recall@k / MRR)")
    lines += _ablation_table(ablation)
    lines.append("")
    lines.append("### End-to-end (capability on vs off)")
    lines.append("")
    lines.append(f"- Trigger precision: {scores.trigger_precision:.2f}")
    lines.append(f"- Trigger recall: {scores.trigger_recall:.2f}")
    lines.append(
        f"- Grounding (deterministic) on/off: {scores.grounded_on}/{scores.grounded_off} "
        + f"of {scores.triggerable} → delta +{scores.grounding_delta:.2f}"
    )
    lines.append(
        f"- Grounding (judged 0–2) on/off totals: {scores.judged_on_total}/"
        + f"{scores.judged_off_total} → delta +{scores.judged_delta:.2f}"
    )
    lines.append("")
    lines.append("### Verdict")
    lines.append(f"- **Trigger:** {_trigger_verdict(scores)}")
    lines.append(f"- **Retrieval:** {_retrieval_verdict(ablation)}")
    lines.append(f"- **Grounding:** {_grounding_verdict(scores)}")
    lines.append("")
    return lines


def _render(results: dict[str, tuple[Ablation, ConsumerScores]]) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Embeddings eval findings",
        "",
        f"Generated {now} by `python -m evals.report` against the live `HEPH_EVAL_*` "
        + "models, over synthetic corpora (`evals/dataset.py`).",
        "",
        "Three failure points are scored per consumer: **trigger** (does the agent "
        + "call retrieval?), **retrieval** (do the embeddings beat keyword?), and "
        + "**grounding** (does the answer use what was recalled?).",
        "",
    ]
    for name, (ablation, scores) in results.items():
        lines += _consumer_section(name, ablation, scores)
    return "\n".join(lines) + "\n"


# --- entry point --------------------------------------------------------------


async def _main() -> int:
    absent = missing_env()
    if absent:
        print(f"Set the eval env vars to run: {', '.join(absent)}", file=sys.stderr)
        return 2
    embedder = EnvEmbedder.from_env()
    # Preflight: fail loudly rather than measuring a silently-degraded endpoint.
    probe = await embedder.embed(OWNER, ["probe"])
    if not probe.vectors or probe.dim <= 0:
        print("Preflight failed: the embedding endpoint returned no vectors.", file=sys.stderr)
        return 3
    chat = build_chat_model()

    results = {
        "Long-term memory": await _memory_run(embedder, chat),
        "Cross-chat search": await _conversation_run(embedder, chat),
    }

    report = _render(results)
    _ = FINDINGS.write_text(report)
    print(report)
    print(f"\nWrote {FINDINGS}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
