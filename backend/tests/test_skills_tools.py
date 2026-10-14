"""The skills toolset and the per-turn catalog the engine injects (`SKILL-2`, `SKILL-3`)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from runs import Run, RunStream
from services.sandbox import SandboxError, SandboxSessionManager
from services.skills import SkillStore
from services.skills.store import SkillCatalogEntry
from tools.deps import RunDeps
from tools.skills import _skill_catalog_block, skills_toolset

from ._helpers import client_app
from .test_skills_bundle import SKILL_MD, _zip

OWNER = "operator"


class FakeSession:
    """A sandbox session that keeps its workspace in a dict — the same `read_file` /
    `write_file` contract the real one exposes, including raising on a missing path."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.writes = 0

    def read_file(self, relpath: str) -> bytes:
        if relpath not in self.files:
            raise SandboxError(f"no such file: {relpath}")
        return self.files[relpath]

    def write_file(self, relpath: str, content: bytes) -> None:
        self.files[relpath] = content
        self.writes += 1

    def ensure_workspace(self) -> Path:
        """The seam `services/workspace.py` binds to — the file tools and the code
        runner reach the workspace as a directory, not through the container."""
        return Path("/fake-host-workspace")


class FakeSessions:
    def __init__(self, session: FakeSession | None = None) -> None:
        self.session = session or FakeSession()

    async def acquire(self, _key: str, *, holder: object = None) -> FakeSession:
        return self.session


async def _store() -> SkillStore:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return SkillStore(engine, vault)


def _tool(name: str):
    toolset = skills_toolset()
    return toolset.tools[name].function


def _ctx(deps: RunDeps) -> RunContext[RunDeps]:
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


def _deps(skills: SkillStore | None = None, sandbox_sessions: object = None) -> RunDeps:
    # The bag keys by concrete type; fakes register under the class the tools resolve.
    caps = ServiceContainer()
    if skills is not None:
        caps.add(skills, as_type=SkillStore)
    if sandbox_sessions is not None:
        caps.add(sandbox_sessions, as_type=SandboxSessionManager)
    run = Run(id="run-1", kind="chat", owner_id=OWNER, stream=RunStream())
    return RunDeps(run=run, owner_id=OWNER, conversation_id="conv-1", caps=caps)


async def _published(store: SkillStore, *, body: str = "# Release notes\n\nCollect the merged PRs."):
    view = await store.create(
        OWNER,
        name="release-notes",
        description="Draft release notes from a changelog.",
        body=body,
    )
    return await store.set_published(OWNER, view.id, True)


# ── open ─────────────────────────────────────────────────────────────────────────────────


async def test_open_returns_the_instructions_and_stages_the_bundle():
    store = await _store()
    imported, _ = await store.import_bundle(
        OWNER,
        _zip(
            {
                "pdf-processing/SKILL.md": SKILL_MD.encode(),
                "pdf-processing/scripts/fill.py": b"print('filling')\n",
            }
        ),
        "pdf-processing.zip",
    )
    await store.set_published(OWNER, imported.id, True)
    sessions = FakeSessions()

    result = await _tool("open")(
        _ctx(_deps(skills=store, sandbox_sessions=sessions)), "pdf-processing"
    )

    assert result["ok"] is True
    assert "PDF processing" in result["instructions"]
    assert result["allowed_tools"] == ["Read", "Bash"]
    assert result["files"] == [
        "/work/skills/pdf-processing/SKILL.md",
        "/work/skills/pdf-processing/scripts/fill.py",
    ]
    assert sessions.session.files["skills/pdf-processing/scripts/fill.py"] == b"print('filling')\n"
    # The staged SKILL.md is the rendered standard artifact, not just the body.
    assert sessions.session.files["skills/pdf-processing/SKILL.md"].startswith(b"---\n")


async def test_reopening_a_skill_does_not_rewrite_identical_files():
    store = await _store()
    await _published(store)
    sessions = FakeSessions()
    deps = _deps(skills=store, sandbox_sessions=sessions)

    await _tool("open")(_ctx(deps), "release-notes")
    after_first = sessions.session.writes
    await _tool("open")(_ctx(deps), "release-notes")

    assert sessions.session.writes == after_first


async def test_open_refuses_a_draft_the_same_way_as_an_unknown_name():
    """A draft must be indistinguishable from nonexistent — the operator hasn't
    published it, so the agent should not learn it exists."""
    store = await _store()
    await store.create(OWNER, name="secret-draft", description="Not ready.", body="x")
    await _published(store)

    with pytest.raises(ModelRetry) as caught:
        await _tool("open")(_ctx(_deps(skills=store)), "secret-draft")
    assert "secret-draft" not in str(caught.value).split("Available skills:")[1]
    assert "release-notes" in str(caught.value)


async def test_open_without_a_sandbox_still_returns_instructions():
    store = await _store()
    await _published(store)
    result = await _tool("open")(_ctx(_deps(skills=store)), "release-notes")
    assert result["ok"] is True
    assert result["files"] == []
    assert "unavailable" in result["note"]


async def test_open_survives_a_sandbox_failure():
    class FailingSessions:
        async def acquire(self, _key, *, holder=None):
            raise SandboxError("no runtime")

    store = await _store()
    await _published(store)
    result = await _tool("open")(
        _ctx(_deps(skills=store, sandbox_sessions=FailingSessions())), "release-notes"
    )
    assert result["ok"] is True
    # A fail-closed sandbox is now indistinguishable from having no runtime at all —
    # both are "there is no workspace", answered once by the resolver. The skill still
    # opens; only its files are missing, and the note says so.
    assert "not staged" in result["note"]


async def test_tools_degrade_when_the_capability_is_absent():
    with pytest.raises(ModelRetry):
        await _tool("open")(_ctx(_deps()), "anything")
    for name, args in [
        ("create", ("a-skill", "desc", "body")),
        ("edit", ("a-skill", "old", "new", "why")),
    ]:
        result = await _tool(name)(_ctx(_deps()), *args)
        assert result["ok"] is False
        assert "unavailable" in result["error"]


# ── create / edit ────────────────────────────────────────────────────────────────────────


async def test_create_only_ever_writes_a_draft():
    store = await _store()
    result = await _tool("create")(
        _ctx(_deps(skills=store)), "deploy-checks", "Check a deploy is safe.", "1. Look."
    )
    assert result["ok"] is True
    assert result["status"] == "draft"

    saved = await store.get_by_name(OWNER, "deploy-checks")
    assert saved.published is False
    assert saved.source == "agent"
    assert await store.catalog(OWNER) == []


async def test_create_asks_the_model_to_retry_on_an_invalid_name():
    store = await _store()
    with pytest.raises(ModelRetry, match="name"):
        await _tool("create")(_ctx(_deps(skills=store)), "Not A Slug", "desc", "body")


async def test_edit_replaces_one_span():
    store = await _store()
    await _published(store)
    result = await _tool("edit")(
        _ctx(_deps(skills=store)),
        "release-notes",
        "Collect the merged PRs.",
        "Collect the tags.",
        "The repo tags releases now.",
    )
    assert result["ok"] is True
    assert "Collect the tags." in (await store.get_by_name(OWNER, "release-notes")).body


@pytest.mark.parametrize(
    ("body", "old", "expected"),
    [
        ("one two", "missing", "was not found"),
        ("dup dup", "dup", "matched 2 places"),
    ],
)
async def test_edit_retries_with_a_precise_reason(body, old, expected):
    store = await _store()
    await _published(store, body=body)
    with pytest.raises(ModelRetry, match=expected):
        await _tool("edit")(_ctx(_deps(skills=store)), "release-notes", old, "x", "why")


async def test_rewriting_a_published_skill_is_the_one_gated_skill_tool():
    """A published skill's body is loaded and *followed* on every later `skills_open`, in
    conversations unrelated to the one that changed it — so untrusted content the agent
    read this turn must not be able to poison a standing playbook without the operator
    seeing it. `create` stays ungated: it can only write a draft, and publishing (the act
    that makes a skill reachable at all) is already the operator's."""
    # A real RunContext here, not the module's duck-typed one: `get_tools` is the path
    # that stamps the approval kind onto each tool def, and it reads more of the context
    # than a direct function call does.
    ctx = RunContext(deps=_deps(skills=await _store()), model=TestModel(), usage=RunUsage())
    tools = await skills_toolset().get_tools(ctx)
    gated = {name for name, tool in tools.items() if tool.tool_def.kind == "unapproved"}
    assert gated == {"edit"}


async def test_the_edit_gate_reaches_the_approval_scope_vocabulary():
    """The static mark is what `tools/catalog` discovers by inspection, so `skills_edit`
    becomes a name a conversation grant or a task pre-authorization can name — without
    the skills manifest having to declare it in `gated_tools`."""
    async with client_app() as (client, _app):
        scopes = {row["name"] for row in (await client.get("/tools/approval-scopes")).json()}
    assert "skills_edit" in scopes
    assert "skills_create" not in scopes  # a draft needs no approval


# ── the injected catalog ─────────────────────────────────────────────────────────────────


def test_catalog_block_is_empty_with_no_published_skills():
    assert _skill_catalog_block([]) == ""


def test_catalog_block_counts_the_skills_and_lists_only_the_pinned():
    block = _skill_catalog_block(
        [
            SkillCatalogEntry(
                name="release-notes", description="Draft release notes.", pinned=True
            ),
            SkillCatalogEntry(name="triage-bugs", description="Triage a bug report."),
        ]
    )
    assert "2 saved skills" in block
    assert "- release-notes: Draft release notes." in block
    assert "triage-bugs" not in block
    assert "platform_search" in block and "skills_open" in block


def test_catalog_block_stays_one_line_however_many_skills_there_are():
    entries = [SkillCatalogEntry(name=f"skill-{i}", description="d" * 200) for i in range(100)]
    block = _skill_catalog_block(entries)
    assert "100 saved skills" in block
    assert "skill-" not in block
    assert len(block) < 400


def test_the_block_reads_the_same_however_recently_each_skill_was_touched():
    """The catalog renders at the head of every request, so touching one skill must not
    reorder the pinned list."""
    entries = [
        SkillCatalogEntry(name="release-notes", description="Draft release notes.", pinned=True),
        SkillCatalogEntry(name="triage-bugs", description="Triage a bug report.", pinned=True),
    ]
    assert _skill_catalog_block(entries) == _skill_catalog_block(list(reversed(entries)))


def test_past_the_pin_limit_the_most_recently_touched_pins_stay():
    entries = [
        SkillCatalogEntry(name=f"skill-{i:02d}", description="d", pinned=True) for i in range(10)
    ]
    block = _skill_catalog_block(list(reversed(entries)))
    assert "skill-09" in block
    assert "skill-00" not in block


async def test_catalog_reaches_the_block_with_published_skills_only():
    store = await _store()
    await store.create(OWNER, name="a-draft", description="Draft.", body="x")
    await _published(store)
    entries = await store.catalog(OWNER)
    assert [entry.name for entry in entries] == ["release-notes"]
    assert "1 saved skill," in _skill_catalog_block(entries)


async def test_a_pinned_skill_is_named_in_the_block():
    store = await _store()
    skill = await _published(store)
    await store.set_pinned(OWNER, skill.id, True)
    assert "- release-notes:" in _skill_catalog_block(await store.catalog(OWNER))


# ── Regressions ──────────────────────────────────────────────────────────────────────────


async def test_edit_refuses_a_draft_the_same_way_open_does():
    """A draft is the operator's unreviewed work: the agent must neither rewrite one nor
    learn it exists from a differing error."""
    store = await _store()
    draft = await store.create(
        OWNER, name="secret-draft", description="Not ready.", body="do the private thing"
    )
    await _published(store)

    with pytest.raises(ModelRetry) as caught:
        await _tool("edit")(_ctx(_deps(skills=store)), "secret-draft", "private", "public", "why")
    message = str(caught.value)
    assert "No published skill named" in message
    assert "secret-draft" not in message.split("Available skills:")[1]
    # …and the draft is untouched.
    assert (await store.get(OWNER, draft.id)).body == "do the private thing"
