"""Part B — end-to-end agent benefit, capability-on vs -off, for both consumers.

The truest "does the LLM actually benefit?" signal. For each consumer we seed its
corpus into a live-embedder store, then run every question through a real agent
turn twice:

- **capability-on** — ``build_chat_orchestrator(q, model=chat, categories={the
  consumer's toolset}, capabilities=ServiceContainer.of(...))``,
- **capability-off** — the same turn with the consumer omitted (the baseline).

Each turn is submitted via ``RunRegistry().submit(...)`` + ``await run.wait()``;
its event stream is read with ``run.stream.replay()`` to (a) detect whether the
retrieval tool fired (``memory_recall`` / ``platform_search``, from the
``tool.*`` events) and (b) capture the final answer text. Scored three ways per
consumer:

- **trigger precision/recall** — fired on should-trigger Qs, silent on controls,
- **grounding (deterministic)** — the on-answer contains the gold fact, the off
  one does not (the anchor),
- **grounding (judged)** — a small-rubric judge on the utility model (or the live
  chat model) scores the on/off pair; the deterministic check anchors the judge.

The assertions: high trigger precision/recall, and capability-on materially
outscores capability-off on should-trigger questions.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset

from agent import build_chat_orchestrator
from agent.meta import make_utility_agent
from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from evals.dataset import (
    Corpus,
    EndToEndQuestion,
    conversation_corpus,
    memory_corpus,
)
from runs import RunRegistry, RunStatus
from runs.events import AnswerDelta, ToolStarted
from services.conversation_search import ConversationSearch
from services.corpus import ConversationAdapter, CorpusChunkStore, CorpusIndex, FolderAdapter
from services.embeddings import Embedder
from services.registry import ModelRegistry
from tools.deps import RunDeps
from tools.memory import memory_toolset
from tools.platform import platform_toolset

OWNER = "operator"

# The namespaced retrieval tool each consumer's category exposes (the category name
# prefixes the tool, per tools/toolsets.py).
MEMORY_RETRIEVAL_TOOL = "memory_recall"
CONVERSATION_RETRIEVAL_TOOL = "platform_search"


@dataclass(frozen=True)
class TurnResult:
    answer: str
    tools_fired: set[str]

    def fired(self, tool: str) -> bool:
        return tool in self.tools_fired


async def _run_turn(
    question: str,
    *,
    model: Model,
    categories: Mapping[str, AbstractToolset[RunDeps]] | None,
    capabilities: ServiceContainer,
) -> TurnResult:
    """One real agent turn; reads the event stream for tool firings + answer text."""
    orch = build_chat_orchestrator(
        question,
        model=model,
        categories=categories,
        capabilities=capabilities,
    )
    run = RunRegistry().submit(kind="chat", owner_id=OWNER, orchestrator=orch)
    await run.wait()
    assert run.status is RunStatus.done, f"turn did not complete: {run.status}"

    answer_parts: list[str] = []
    tools_fired: set[str] = set()
    for event in run.stream.replay():
        body = event.body
        if isinstance(body, ToolStarted):
            tools_fired.add(body.name)
        elif isinstance(body, AnswerDelta):
            answer_parts.append(body.text)
    return TurnResult(answer="".join(answer_parts), tools_fired=tools_fired)


# --- scoring ------------------------------------------------------------------


class _JudgeScore(BaseModel):
    """A 0–2 score for how well an answer used the operator's own context."""

    score: int  # 0 = no use / wrong, 1 = partial, 2 = clearly used the stored fact
    reason: str = ""


_JUDGE_INSTRUCTIONS = (
    "You grade whether an assistant answer used the operator's own stored context to "
    "answer a personal question. Score 0 if it gave a generic answer, refused, or got "
    "the fact wrong; 1 if it partially used the stored fact; 2 if it clearly and "
    "correctly used it. Judge only against the rubric you are given."
)


@dataclass
class ConsumerScores:
    consumer: str
    # trigger
    trigger_true_positives: int = 0  # fired on a should-trigger Q
    trigger_false_negatives: int = 0  # silent on a should-trigger Q
    trigger_false_positives: int = 0  # fired on a control
    trigger_true_negatives: int = 0  # silent on a control
    # grounding (deterministic)
    grounded_on: int = 0  # on-answer contained the gold fact
    grounded_off: int = 0  # off-answer contained the gold fact
    triggerable: int = 0  # number of should-trigger questions
    # grounding (judged) — summed score deltas over should-trigger Qs
    judged_on_total: int = 0
    judged_off_total: int = 0

    @property
    def trigger_precision(self) -> float:
        fired = self.trigger_true_positives + self.trigger_false_positives
        return self.trigger_true_positives / fired if fired else 1.0

    @property
    def trigger_recall(self) -> float:
        want = self.trigger_true_positives + self.trigger_false_negatives
        return self.trigger_true_positives / want if want else 1.0

    @property
    def grounding_delta(self) -> float:
        """Fraction of should-trigger Qs grounded with the capability minus without."""
        if not self.triggerable:
            return 0.0
        return (self.grounded_on - self.grounded_off) / self.triggerable

    @property
    def judged_delta(self) -> float:
        if not self.triggerable:
            return 0.0
        return (self.judged_on_total - self.judged_off_total) / self.triggerable


async def _judge(model: Model, q: EndToEndQuestion, answer: str) -> int:
    agent = make_utility_agent(model, output_type=_JudgeScore, instructions=_JUDGE_INSTRUCTIONS)
    result = await agent.run(f"Question: {q.question}\nRubric: {q.rubric}\nAnswer:\n{answer}")
    return max(0, min(2, result.output.score))


async def score_consumer(
    *,
    consumer: str,
    retrieval_tool: str,
    model: Model,
    categories: Mapping[str, AbstractToolset[RunDeps]],
    capabilities_on: ServiceContainer,
    questions: list[EndToEndQuestion],
) -> ConsumerScores:
    """Run every question on/off and tally trigger + grounding for one consumer."""
    scores = ConsumerScores(consumer=consumer)
    for q in questions:
        on = await _run_turn(
            q.question, model=model, categories=categories, capabilities=capabilities_on
        )
        # capability-off: omit the consumer's category + capability entirely.
        off = await _run_turn(
            q.question, model=model, categories={}, capabilities=ServiceContainer()
        )

        fired = on.fired(retrieval_tool)
        if q.should_trigger:
            scores.triggerable += 1
            scores.trigger_true_positives += int(fired)
            scores.trigger_false_negatives += int(not fired)
            scores.grounded_on += int(q.gold_fact.lower() in on.answer.lower())
            scores.grounded_off += int(q.gold_fact.lower() in off.answer.lower())
            scores.judged_on_total += await _judge(model, q, on.answer)
            scores.judged_off_total += await _judge(model, q, off.answer)
        else:
            scores.trigger_false_positives += int(fired)
            scores.trigger_true_negatives += int(not fired)
    return scores


# --- seeding ------------------------------------------------------------------


async def _seed_memory(store, corpus: Corpus) -> None:
    for m in corpus.memories:
        await store.remember(OWNER, m.content)


async def _seed_conversations(store, corpus: Corpus) -> None:
    for c in corpus.conversations:
        cid = await store.create_conversation(OWNER, title=c.title)
        for prompt, answer in c.turns:
            store.record(
                cid,
                [
                    ModelRequest(parts=[UserPromptPart(content=prompt)]),
                    ModelResponse(parts=[TextPart(content=answer)], model_name="m"),
                ],
            )
    await store._worker.join()


# --- assertions ---------------------------------------------------------------


def _assert_agent_benefits(scores: ConsumerScores) -> None:
    # The agent must call retrieval when the question needs it and stay quiet on
    # generic world-knowledge controls.
    assert scores.trigger_recall >= 0.6, (
        f"{scores.consumer}: agent under-triggers retrieval (recall {scores.trigger_recall:.2f})"
    )
    assert scores.trigger_precision >= 0.6, (
        f"{scores.consumer}: agent over-triggers retrieval on controls "
        f"(precision {scores.trigger_precision:.2f})"
    )
    # Capability-on must materially out-ground capability-off on the answerable set.
    assert scores.grounding_delta > 0.0, (
        f"{scores.consumer}: capability-on did not ground better than off "
        f"(delta {scores.grounding_delta:.2f})"
    )
    assert scores.judged_delta > 0.0, (
        f"{scores.consumer}: judged on/off delta not positive ({scores.judged_delta:.2f})"
    )


# --- tests --------------------------------------------------------------------


async def test_memory_end_to_end_benefit(chat_model, memory_store):
    corpus = memory_corpus()
    await _seed_memory(memory_store, corpus)
    scores = await score_consumer(
        consumer="memory",
        retrieval_tool=MEMORY_RETRIEVAL_TOOL,
        model=chat_model,
        categories={"memory": memory_toolset()},
        capabilities_on=ServiceContainer.of(memory_store),
        questions=corpus.questions,
    )
    _assert_agent_benefits(scores)


def platform_over(search: ConversationSearch, embedder: Embedder) -> CorpusIndex:
    """A corpus index whose one source is ``search``, for scoring ``platform_search``."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    chunks = CorpusChunkStore(engine, vault, embedder)
    folder = FolderAdapter(engine, chunks, vault.unlocked_event)
    index = CorpusIndex(embedder, ModelRegistry.__new__(ModelRegistry), chunks, folder)
    index.register(ConversationAdapter(search))
    return index


async def test_conversation_end_to_end_benefit(chat_model, embedder, conversation_search):
    store, search = conversation_search
    corpus = conversation_corpus()
    await _seed_conversations(store, corpus)
    scores = await score_consumer(
        consumer="conversations",
        retrieval_tool=CONVERSATION_RETRIEVAL_TOOL,
        model=chat_model,
        categories={"platform": platform_toolset()},
        capabilities_on=ServiceContainer.of(platform_over(search, embedder)),
        questions=corpus.questions,
    )
    _assert_agent_benefits(scores)
