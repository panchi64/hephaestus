"""``platform_search`` / ``platform_read``: one retrieval surface over the whole platform."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from sqlmodel import Session

from core.container import ServiceContainer
from models.conversation import Conversation
from models.subagent import SubagentRecord
from models.task import ScheduledTask, TaskRun
from runs import Run, RunStream
from services.corpus import (
    ConversationAdapter,
    CorpusChunkStore,
    CorpusIndex,
    FolderAdapter,
    MemoryAdapter,
    record_adapters,
)
from services.corpus.chunk_store import _stitch
from services.memory import MemoryStore
from services.plugins import PluginStore
from services.registry import ModelRegistry
from services.skills import SkillStore
from tools.deps import RunDeps
from tools.platform import platform_toolset

from ._plugin_helpers import bundle
from .test_conversation_search import DegradedEmbedder, _record, _setup

OWNER = "operator"


class Platform:
    def __init__(self, engine, vault, store, index, memory, skills, plugins) -> None:
        self.engine = engine
        self.vault = vault
        self.store = store
        self.index = index
        self.memory = memory
        self.skills = skills
        self.plugins = plugins
        self.tools = platform_toolset().tools

    def ctx(self, conversation_id: str | None = "current") -> RunContext[RunDeps]:
        run = Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
        deps = RunDeps(
            run=run,
            owner_id=OWNER,
            conversation_id=conversation_id,
            caps=ServiceContainer.of(self.index, self.skills, self.plugins),
        )
        return RunContext(deps=deps, model=TestModel(), usage=RunUsage(), tool_call_approved=True)

    async def search(self, query: str, **kwargs):
        return await self.tools["search"].function(self.ctx(), query, **kwargs)

    async def read(self, ref: str):
        return await self.tools["read"].function(self.ctx(), ref)


@pytest.fixture
async def platform():
    engine, vault, store, search = await _setup(DegradedEmbedder())
    embedder = DegradedEmbedder()
    memory = MemoryStore(engine, vault, embedder)
    chunks = CorpusChunkStore(engine, vault, embedder)
    folder = FolderAdapter(engine, chunks, vault.unlocked_event)
    index = CorpusIndex(embedder, ModelRegistry.__new__(ModelRegistry), chunks, folder)
    index.register(folder)
    index.register(MemoryAdapter(memory))
    index.register(ConversationAdapter(search))
    for adapter in record_adapters(engine, vault):
        index.register(adapter)
    yield Platform(
        engine, vault, store, index, memory, SkillStore(engine, vault), PluginStore(engine, vault)
    )
    await store.stop()


def _refs(results) -> list[str]:
    return [hit.ref for hit in results.hits]


async def test_a_fact_from_another_conversation_is_found_and_read_back(platform):
    older = await _record(
        platform.store, OWNER, ("which port does the staging proxy use", "port 8443")
    )
    results = await platform.search("staging proxy port")
    assert _refs(results) == [f"surf-conversations:{older}"]
    assert results.instruction
    assert "staging proxy" in results.hits[0].excerpt

    document = await platform.read(f"surf-conversations:{older}")
    assert "port 8443" in document.text
    assert document.citations()[0].engagement == "read"


async def test_the_callers_own_conversation_is_never_a_hit(platform):
    current = await _record(platform.store, OWNER, ("the staging proxy", "uses 8443"))
    ctx = platform.ctx(conversation_id=current)
    results = await platform.tools["search"].function(ctx, "staging proxy")
    assert results.hits == []


async def test_reports_summaries_and_routine_outputs_are_searchable(platform):
    vault = platform.vault
    conversation = await _record(platform.store, OWNER, ("hello", "hi"))
    now = datetime.now(UTC)
    with Session(platform.engine) as session:
        convo = session.get(Conversation, conversation)
        assert convo is not None
        convo.work_summary_enc = vault.encrypt_str("Migrated the billing service to Postgres.")
        convo.work_summary_at = now
        session.add(convo)
        session.add(
            SubagentRecord(
                owner_id=OWNER,
                parent_conversation_id=conversation,
                child_conversation_id="child",
                run_id="run-x",
                spec_name="explorer",
                handle="flaky-test-hunter",
                task_enc=vault.encrypt_str("Find the flaky integration test"),
                summary_enc=vault.encrypt_str("test_checkout_timeout races the clock"),
                status="done",
                ended_at=now,
            )
        )
        task = ScheduledTask(
            owner_id=OWNER,
            kind="agent",
            title_enc=vault.encrypt_str("Nightly dependency audit"),
            prompt_enc=vault.encrypt_str("audit"),
            schedule_type="once",
            output="chat",
            pre_authorized=[],
        )
        session.add(task)
        session.flush()
        session.add(
            TaskRun(
                task_id=task.id,
                finished_at=now,
                outcome="ok",
                summary_enc=vault.encrypt_str("Three advisories in transitive deps."),
            )
        )
        session.commit()

    summary = await platform.search("billing postgres", sources=["summaries"])
    assert summary.hits[0].source == "summaries"
    report = await platform.search("flaky integration test", sources=["reports"])
    assert report.hits[0].title.startswith("flaky-test-hunter")
    routine = await platform.search("advisories", sources=["routines"])
    assert routine.hits[0].title == "Nightly dependency audit"

    document = await platform.read(report.hits[0].ref)
    assert "races the clock" in document.text and "Find the flaky" in document.text


async def test_since_drops_older_material(platform):
    memory = await platform.memory.remember(OWNER, "the boiler code is 4471")
    later = (memory.created_at + timedelta(days=1)).isoformat()
    assert (await platform.search("boiler", sources=["memory"])).hits
    assert not (await platform.search("boiler", sources=["memory"], since=later)).hits
    with pytest.raises(ModelRetry):
        await platform.search("boiler", since="last tuesday")


async def test_sources_narrow_the_search(platform):
    await platform.memory.remember(OWNER, "the boiler code is 4471")
    await _record(platform.store, OWNER, ("the boiler", "is in the cellar"))
    results = await platform.search("boiler", sources=["memory"])
    assert {hit.source for hit in results.hits} == {"memory"}


async def test_the_catalogs_are_searched_by_description(platform):
    skill = await platform.skills.create(
        OWNER,
        name="release-notes",
        description="Draft release notes from a changelog.",
        body="Collect the merged PRs.",
    )
    await platform.skills.set_published(OWNER, skill.id, True)
    results = await platform.search("changelog", sources=["skills"])
    assert _refs(results) == ["skill:release-notes"]
    assert results.instruction == ""
    assert "skills_open" in await platform.read("skill:release-notes")

    agents = await platform.search("researcher", sources=["agents"])
    assert "agent:researcher" in _refs(agents)
    document = await platform.read("agent:researcher")
    assert document.title == "researcher"


async def test_published_plugins_are_searched_by_their_tools(platform):
    view = await platform.plugins.write_draft(OWNER, "weather", bundle(), source="agent")
    assert _refs(await platform.search("forecast", sources=["plugins"])) == []

    await platform.plugins.publish(OWNER, view.id)
    results = await platform.search("forecast", sources=["plugins"])
    assert _refs(results) == ["plugin:weather"]
    assert "plugins_weather_forecast" in results.hits[0].excerpt
    assert "search_tools" in await platform.read("plugin:weather")


async def test_a_file_reads_back_whole(platform, tmp_path: Path):
    (tmp_path / "notes.txt").write_text(" ".join(f"word{i}" for i in range(1200)))
    folder = platform.index._folder
    await folder.start()
    source = await platform.index.add_folder(OWNER, str(tmp_path))
    await folder._worker.join()
    await folder.stop()
    results = await platform.search("word1100", sources=["files"])
    assert results.hits and results.hits[0].source == "files"
    document = await platform.read(f"{source.id}:{results.hits[0].ref.partition(':')[2]}")
    assert document.text.count("word1100") == 1
    assert "word0" in document.text and "word1199" in document.text


async def test_an_unknown_ref_says_so(platform):
    assert "Nothing found" in await platform.read("surf-memory:missing")
    assert "Nothing found" in await platform.read("nonsense")


def test_stitching_drops_the_overlap_between_chunks():
    assert _stitch(["a b c d", "c d e f", "f g"]) == "a b c d e f g"
