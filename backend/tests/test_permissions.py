"""The permission level: the knobs behind the five names, and the two halves that
enforce them.

Two halves, because one alone is not enforcement. The **toolset** marks every tool that
reaches past the level's write scope as needing approval, so the model's request for one
comes back undone instead of running — that half is checked through the composed stack a
real run resolves, not through a re-derivation of the rule. The **decision** then rules on
the call that came back. Between them sits the invariant worth the most here: a level only
ever adds a gate, never removes one. A tool that pauses its own calls is still answered at
every level — by the operator, or by the review at the level whose meaning is that they
asked for their answers to be given for them — so raising a thread to Edit cannot quietly
delete a protection nobody re-examined.

**Yolo is the one exception, and it is tested as one rather than excluded from the
sweeps.** Every "at every level" assertion below either still holds there or names it and
says what it does instead, because a level that removes a gate is exactly the level whose
behaviour must be written down rather than left to a loop that skips it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from agent import build_chat_orchestrator
from core.db import init_db, make_engine
from core.vault import Vault
from routes.deps import app_state
from runs import Run, RunRegistry, RunStatus, RunStream
from services.conversations import ConversationBinding, ConversationStore
from services.permissions import (
    ACTING_PERMISSIONS,
    DEFAULT_PERMISSION,
    PERMISSION_LADDER,
    PERMISSION_LEVELS,
    PERMISSIONS,
    PLANNING_TOOLS,
    STRICTEST_PERMISSION,
    ApprovalPolicy,
    Decision,
    PermissionLevel,
    beyond_scope,
    blocked_message,
    decide,
    permission_level,
    permission_spec,
    stricter_permission,
    tools_beyond_scope,
)
from services.tool_policy import permission_disabled_tools
from services.tool_sensitivity import (
    SENSITIVITY_CLASSES,
    Sensitivity,
    declared_sensitivity,
    sensitivity_of,
)
from tools import RunDeps, build_agent_toolsets

from ._helpers import (
    client_app,
    collect_sse_events,
    full_tool_categories,
    patch_model_resolution,
    settle,
)
from ._json import json_body, parse_json, s
from .test_chat_concurrency_guards import patch_hanging_model

OWNER = "operator"

# One tool per class, by name, so the tests below read as the rule they check rather than
# as a list of strings. Each is pinned to its class by `test_tool_sensitivity.py`.
READ = "web_search"
WORKSPACE_WRITE = "files_write_file"
HOST_EXEC = "shell_run_command"
EXTERNAL_EFFECT = "mail_send"
SECRET = "vault_get_entry"
#: A name no literal can enumerate — an operator's own MCP tool. Classifies as an
#: external effect, which is the fail-closed reading and the honest one.
UNCLASSIFIED = "external_someserver_do_a_thing"
#: A name from nowhere at all: no class, and not the operator's either. The shape a tool
#: added to the catalog without a class takes, and the one nothing else answers for.
UNKNOWN = "someones_brand_new_tool"
#: A **read** that pauses its own calls — the global-recall gate. Within every level's
#: scope, so a deferral of one is never the level's doing.
SELF_GATED_READ = "corpus_retrieve"
#: A **workspace write** that pauses its own calls: `skills_edit` carries
#: `requires_approval=True`. Within Edit's and Auto's scope, so neither of those levels
#: marks it and its own marking is the only thing standing.
SELF_MARKED_WRITE = "skills_edit"


def _classified_names() -> set[str]:
    return {name for names in SENSITIVITY_CLASSES.values() for name in names}


class TestTheKnobs:
    """A level is a preset over a pair, not a fourth branch."""

    def test_every_level_has_a_preset(self):
        assert set(PERMISSIONS) == PERMISSION_LEVELS
        assert all(spec.level == level for level, spec in PERMISSIONS.items())

    def test_the_ceiling_is_what_the_two_read_only_levels_share(self):
        # Plan and Manual differ in what happens past the ceiling, not in the ceiling —
        # which is the point of storing the pair: the read-only-ness is one knob, and the
        # two levels are two answers to the *other* one.
        assert PERMISSIONS["plan"].ceiling is Sensitivity.READ
        assert PERMISSIONS["manual"].ceiling is Sensitivity.READ
        assert PERMISSIONS["plan"].approval_policy is ApprovalPolicy.WITHHOLD
        assert PERMISSIONS["manual"].approval_policy is ApprovalPolicy.ASK

    def test_edit_and_auto_reach_the_workspace_and_differ_in_who_answers(self):
        assert PERMISSIONS["edit"].ceiling is Sensitivity.WORKSPACE_WRITE
        assert PERMISSIONS["auto"].ceiling is Sensitivity.WORKSPACE_WRITE
        assert PERMISSIONS["edit"].approval_policy is ApprovalPolicy.ASK
        assert PERMISSIONS["auto"].approval_policy is ApprovalPolicy.REVIEW

    def test_an_unreadable_level_lands_on_the_one_that_does_least(self):
        # Off a database row and out of a parked run's payload, both of which outlive a
        # rename. The conservative direction is the level that can only look.
        assert permission_level("nonsense") == STRICTEST_PERMISSION
        assert permission_level("") == STRICTEST_PERMISSION
        assert permission_spec("nonsense") is PERMISSIONS["plan"]
        assert all(permission_level(level) == level for level in PERMISSION_LEVELS)

    def test_beyond_scope_is_the_one_question_both_halves_ask(self):
        assert not beyond_scope("edit", WORKSPACE_WRITE)
        assert beyond_scope("edit", HOST_EXEC)
        assert beyond_scope("manual", WORKSPACE_WRITE)
        assert not beyond_scope("manual", READ)

    def test_the_set_form_agrees_with_the_predicate(self):
        # The catalog-narrowing caller works in sets and the decision point works one call
        # at a time; a level that withheld a different set than it refuses would be two
        # rules wearing one name.
        for level in PERMISSION_LEVELS:
            assert tools_beyond_scope(level) == frozenset(
                name for name in _classified_names() if beyond_scope(level, name)
            )

    def test_an_unclassifiable_tool_is_elevated_only_where_the_guess_is_load_bearing(self):
        # The unclassifiable names are the operator's own MCP and connector tools, and
        # those already carry a per-tool decision of theirs — the trust list. A level that
        # permits changes leaves that alone rather than overruling an explicit answer with
        # an inferred one; a level that permits *nothing* has no other way to keep its
        # promise against a tool nothing here can bound, so it elevates.
        assert beyond_scope("plan", UNCLASSIFIED)
        assert beyond_scope("manual", UNCLASSIFIED)
        assert not beyond_scope("edit", UNCLASSIFIED)
        assert not beyond_scope("auto", UNCLASSIFIED)
        assert not beyond_scope("yolo", UNCLASSIFIED)
        # ...and a call that arrives anyway is still read as the class that reaches
        # furthest, which is what makes Plan's refusal reach it.
        assert decide("plan", UNCLASSIFIED) is Decision.BLOCK

    def test_a_name_from_nowhere_is_gated_at_every_level_that_gates_anything(self):
        # The exemption above is for the operator's *own* external tools, which carry the
        # prefix and have the trust list deciding them. A name that is neither classified
        # nor theirs has nothing to defer to, so it reads as the class that reaches
        # furthest and is elevated everywhere — including the levels that let the model
        # act, which is where a fail-open would have cost something.
        assert sensitivity_of(UNKNOWN) is Sensitivity.EXTERNAL_EFFECT
        for level in PERMISSION_LEVELS - {"yolo"}:
            assert beyond_scope(level, UNKNOWN), level
        # Yolo's ceiling is above every class, so the fail-closed reading is still applied
        # here and simply finds nothing left to elevate. Asserted rather than skipped:
        # "the conservative fallback stops mattering" is the level's meaning, not a hole.
        assert not beyond_scope("yolo", UNKNOWN)

    def test_a_toolset_may_state_its_own_class_and_is_believed(self):
        # The seam for tools this installation composes at run time rather than ships:
        # a declaration outranks both the registry and the unknown-name fallback, so a
        # composed read-only tool is not gated as though it sent mail.
        assert not beyond_scope("edit", UNKNOWN, declared=Sensitivity.READ)
        assert not beyond_scope("plan", UNKNOWN, declared=Sensitivity.READ)
        # ...and it cannot be used to *lower* a gate below what the level permits.
        assert beyond_scope("edit", UNKNOWN, declared=Sensitivity.HOST_EXEC)
        # A malformed declaration is no declaration, so the fallback still answers.
        assert declared_sensitivity({"sensitivity": "not-a-class"}) is None
        assert beyond_scope("edit", UNKNOWN, declared=None)

    def test_the_plan_writes_are_permitted_at_every_level(self):
        # A read-only turn ends by writing down what it would do; a level that made it ask
        # first would leave it no way to finish at all.
        for level in PERMISSION_LEVELS:
            for tool in PLANNING_TOOLS:
                assert not beyond_scope(level, tool)

    def test_a_fresh_thread_starts_at_auto(self):
        """The default reaches no further than Edit — the two share a ceiling — and moves
        only *who answers* at that boundary: a structural judge and a fence rule on the
        contained offline work, and everything past the worktree still reaches the
        operator. Pinned because it is the one value here an operator feels immediately."""
        assert DEFAULT_PERMISSION == "auto"
        spec = PERMISSIONS[DEFAULT_PERMISSION]
        assert spec.approval_policy is ApprovalPolicy.REVIEW
        assert spec.ceiling is PERMISSIONS["edit"].ceiling

    def test_the_levels_that_can_act_are_the_ones_that_reach_past_reading(self):
        # Derived from the ceiling rather than named, so a further preset is a row in the
        # registry and every caller that means "the levels that can act" follows it —
        # which is how `yolo` joined this set without anything being edited to admit it.
        assert ACTING_PERMISSIONS == {"edit", "auto", "yolo"}
        assert all(
            (level in ACTING_PERMISSIONS) is (spec.ceiling is not Sensitivity.READ)
            for level, spec in PERMISSIONS.items()
        )

    def test_the_stricter_of_two_levels_is_the_one_that_permits_less(self):
        # The order comes off the pair — how far the ceiling reaches, then what happens at
        # its edge — so the four sort themselves without a hand-written list.
        assert stricter_permission("plan", "auto") == "plan"
        assert stricter_permission("auto", "manual") == "manual"
        assert stricter_permission("edit", "auto") == "edit"
        assert stricter_permission("plan", "manual") == "plan"
        # Symmetric, idempotent, and total over the vocabulary.
        for a in PERMISSION_LEVELS:
            for b in PERMISSION_LEVELS:
                assert stricter_permission(a, b) == stricter_permission(b, a)
                assert stricter_permission(a, a) == a

    def test_an_unreadable_level_is_the_strictest_thing_to_take_the_stricter_of(self):
        # Same degrade as everywhere else: a value off a row an older build wrote must not
        # widen the thread it is compared against.
        assert stricter_permission("root", "auto") == STRICTEST_PERMISSION


class TestTheDecisionMatrix:
    """What each level does with a deferred call, by the class of what it would do."""

    def test_plan_refuses_rather_than_asks(self):
        # There is nothing to put in front of the operator: they answered by choosing the
        # level. Everything above a read is refused, including a tool no literal can name
        # — which is the one that gets past Plan's catalog narrowing.
        for tool in (WORKSPACE_WRITE, HOST_EXEC, EXTERNAL_EFFECT, SECRET, UNCLASSIFIED):
            assert decide("plan", tool) is Decision.BLOCK

    def test_manual_asks_about_everything_it_did_not_already_permit(self):
        for tool in (WORKSPACE_WRITE, HOST_EXEC, EXTERNAL_EFFECT, SECRET, UNCLASSIFIED):
            assert decide("manual", tool) is Decision.ASK

    def test_edit_asks_past_the_workspace_and_not_within_it(self):
        for tool in (HOST_EXEC, EXTERNAL_EFFECT, SECRET, UNCLASSIFIED):
            assert decide("edit", tool) is Decision.ASK
        # A workspace write does not reach this decision at Edit at all — the toolset
        # never marks it, so the model's call simply runs (see the elevation tests).
        assert not beyond_scope("edit", WORKSPACE_WRITE)

    @pytest.mark.parametrize("level", PERMISSION_LADDER)
    def test_a_plan_approval_is_put_to_the_operator_at_every_level(self, level: str):
        assert decide(level, "plan_submit") is Decision.ASK

    def test_auto_sends_the_same_calls_to_review_instead(self):
        for tool in (HOST_EXEC, EXTERNAL_EFFECT, SECRET, UNCLASSIFIED):
            assert decide("auto", tool) is Decision.REVIEW

    def test_a_tools_own_gate_survives_every_level_but_the_one_that_deletes_it(self):
        # The asymmetry the module exists to hold, asked of a tool that really has a gate
        # of its own: `corpus_retrieve` pauses a *global* recall from inside the call. It
        # classifies as a read, so it is within every level's scope and no level asked for
        # it to defer — and no level below the top may wave it through either.
        for level in PERMISSION_LEVELS:
            assert not beyond_scope(level, SELF_GATED_READ)
        for level in PERMISSION_LEVELS - {"auto", "yolo"}:
            assert decide(level, SELF_GATED_READ) is Decision.ASK
        # Auto answers it with something other than the operator, and only because
        # answering on their behalf is what choosing Auto means. It is still answered.
        assert decide("auto", SELF_GATED_READ) is Decision.REVIEW
        # Yolo does not answer it — it removes it, which is the one deliberate hole in the
        # invariant this file opens with. Pinned here, beside the rule it breaks, so the
        # exception cannot be widened by accident: any *other* level starting to return
        # ALLOW for a self-gated tool fails the loop three lines up.
        assert decide("yolo", SELF_GATED_READ) is Decision.ALLOW

    def test_yolo_allows_every_class_of_act(self):
        # The level's whole proposition in one assertion. Includes `SECRET` and
        # `UNCLASSIFIED` deliberately: the first is the class that survives every other
        # widening, and the second is the operator's own MCP surface, which is gated by a
        # guess everywhere else. Neither is an oversight here — an operator who has said
        # "never ask me" has answered for both.
        for tool in (WORKSPACE_WRITE, HOST_EXEC, EXTERNAL_EFFECT, SECRET, UNCLASSIFIED):
            assert decide("yolo", tool) is Decision.ALLOW
        # And the catalog is not narrowed to get there: the model is offered everything
        # and nothing it calls comes back deferred by the level.
        assert tools_beyond_scope("yolo") == frozenset()
        assert permission_disabled_tools("yolo") == frozenset()

    def test_yolo_is_reachable_only_by_being_named(self):
        # The one structural guarantee under a level that asks nobody anything: no
        # fallback, no default and no degrade can land a thread here. Every path that
        # resolves a level without the operator naming one goes somewhere else.
        assert DEFAULT_PERMISSION != "yolo"
        assert STRICTEST_PERMISSION != "yolo"
        assert permission_level("nonsense") != "yolo"
        # ...including the spawn path, where a thread opened from inside another one takes
        # the stricter of the two, so an approved spawn can never buy this level.
        for level in PERMISSION_LEVELS - {"yolo"}:
            assert stricter_permission(level, "yolo") == level

    def test_a_corrupt_level_decides_as_plan(self):
        assert decide("nonsense", HOST_EXEC) is Decision.BLOCK

    def test_the_refusal_names_the_level_and_the_tool(self):
        # The model has to be able to tell a refusal from a transient failure, or it
        # spends the rest of the turn retrying a call that will never be allowed.
        message = blocked_message("plan", HOST_EXEC)
        assert "plan" in message
        assert HOST_EXEC in message


async def _resolved_kinds(permission: PermissionLevel) -> dict[str, str]:
    """How a real run's composed toolset stack ends up defining every tool it offers —
    the ground truth for the elevation, resolved through the same stack the engine hands
    the Agent rather than through a re-reading of the rule."""
    run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
    deps = RunDeps(
        run=run,
        owner_id=OWNER,
        permission=permission,
        project_id="proj-1",
        # Plan's catalog narrowing is `tool_policy`'s half and is tested there; apply it
        # here too so this resolves the stack a Plan run really resolves.
        disabled_tools=permission_disabled_tools(permission),
    )
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
    tools = await build_agent_toolsets(full_tool_categories())[0].get_tools(ctx)
    return {name: tool.tool_def.kind for name, tool in tools.items()}


class TestTheToolsetElevation:
    """The half that makes the decision reachable: a tool past the level's scope is
    marked as needing approval, so the call comes back instead of running."""

    async def test_edit_marks_past_the_workspace_and_leaves_the_workspace_alone(self):
        kinds = await _resolved_kinds("edit")
        assert kinds[WORKSPACE_WRITE] == "function"
        assert kinds[READ] == "function"
        assert kinds[HOST_EXEC] == "unapproved"
        assert kinds[EXTERNAL_EFFECT] == "unapproved"
        assert kinds[SECRET] == "unapproved"

    async def test_manual_marks_the_workspace_writes_too(self):
        kinds = await _resolved_kinds("manual")
        assert kinds[WORKSPACE_WRITE] == "unapproved"
        assert kinds[HOST_EXEC] == "unapproved"
        # Reading still needs nothing: Manual is "ask before you *act*", not "ask before
        # you look", and a level that paused every read would be unusable.
        assert kinds[READ] == "function"

    async def test_plan_withholds_what_it_can_and_marks_what_it_cannot(self):
        kinds = await _resolved_kinds("plan")
        # Withheld outright — the model is never offered them, so a Plan turn's schemas
        # are a fraction of the others'.
        assert WORKSPACE_WRITE not in kinds
        assert HOST_EXEC not in kinds
        # ...except the task-list writes, which are the model's own scratchpad and still
        # run without a prompt, and `plan_enter`, which only ever narrows further.
        assert kinds["tasks_write"] == "function"
        assert kinds["plan_enter"] == "function"
        # `plan_submit` is the exception to the exception: exempt from the *level* so a
        # Plan turn has a way out at all, and still marked `unapproved` by its own
        # `ApprovalRequired`, because the way out is the operator's to open.
        assert kinds["plan_submit"] == "function"
        assert kinds[READ] == "function"

    async def test_a_tools_own_marking_is_never_taken_away(self):
        # `skills_edit` classifies as a workspace write and carries `requires_approval=True`
        # of its own. The witness has to be a tool the level would *not* have marked, or
        # the gate could be deleted outright and the assertion would still pass on the
        # level's own elevation.
        assert not beyond_scope("edit", SELF_MARKED_WRITE)
        assert not beyond_scope("auto", SELF_MARKED_WRITE)
        for level in ("edit", "auto"):
            assert (await _resolved_kinds(level))[SELF_MARKED_WRITE] == "unapproved"

    async def test_yolo_marks_nothing_of_its_own_and_leaves_every_other_marking_alone(
        self,
    ):
        # The two halves say different things at this level, and the split is the point.
        #
        # The *toolset* stops marking: nothing is beyond the ceiling, so a tool the level
        # elevated at Edit is an ordinary `function` the model calls with no round trip.
        kinds = await _resolved_kinds("yolo")
        assert (await _resolved_kinds("edit"))[HOST_EXEC] == "unapproved"
        assert kinds[HOST_EXEC] == "function"
        # Every marking that was never the level's is untouched, because this gate is the
        # toolset's and it never read the level at all. `mail_send` and `vault_get_entry`
        # carry `requires_approval=True` themselves; `skills_edit` is the witness that
        # nothing here is being deleted, since the level would not have marked it anyway.
        for tool in (EXTERNAL_EFFECT, SECRET, SELF_MARKED_WRITE):
            assert kinds[tool] == "unapproved", tool
            # ...and every one of them is cleared one layer later, by the *decision*,
            # which is the single place the exception is written down and can be read.
            assert decide("yolo", tool) is Decision.ALLOW, tool
        # Nothing is withheld either, so unlike Plan the model sees the whole catalog.
        assert WORKSPACE_WRITE in kinds
        assert kinds[READ] == "function"


class TestTheThreadRemembers:
    """The level lives on the conversation: set at creation, moved by the operator, and
    read back as one binding with the workspace kind and the project."""

    async def _store(self, tmp_path: Path) -> ConversationStore:
        engine = make_engine("sqlite:///:memory:")
        init_db(engine)
        vault = Vault(tmp_path / "keyfile.json")
        await vault.setup("pw")
        return ConversationStore(engine, vault)

    async def test_a_fresh_thread_starts_at_the_default_filed_or_not(self, tmp_path: Path):
        store = await self._store(tmp_path)
        for project_id in (None, "p1"):
            conversation_id = await store.create_conversation(OWNER, project_id=project_id)
            binding = await store.binding(conversation_id)
            assert binding.permission == DEFAULT_PERMISSION

    async def test_an_explicit_level_beats_the_default(self, tmp_path: Path):
        store = await self._store(tmp_path)
        conversation_id = await store.create_conversation(OWNER, permission="plan")
        assert (await store.binding(conversation_id)).permission == "plan"

    async def test_the_level_moves_and_the_rest_of_the_binding_does_not(self, tmp_path: Path):
        store = await self._store(tmp_path)
        conversation_id = await store.create_conversation(OWNER, project_id="p1")
        _ = await store.open_worktree(conversation_id)
        assert await store.set_permission_level(conversation_id, "manual") == "manual"
        binding = await store.binding(conversation_id)
        assert binding.permission == "manual"
        assert binding.worktree
        assert binding.project_id == "p1"

    async def test_a_corrupt_stored_level_reads_as_the_strictest(self, tmp_path: Path):
        store = await self._store(tmp_path)
        conversation_id = await store.create_conversation(OWNER)
        _ = await store.set_permission_level(conversation_id, "nonsense")
        assert (await store.binding(conversation_id)).permission == STRICTEST_PERMISSION

    async def test_a_missing_thread_reads_as_the_default(self, tmp_path: Path):
        store = await self._store(tmp_path)
        assert (await store.binding("no-such-thread")).permission == DEFAULT_PERMISSION


class TestTheLiveControl:
    """Switching mid-thread is a plain send, and a Plan turn ends by being accepted."""

    async def test_a_send_carries_the_level_and_it_persists(self, monkeypatch: pytest.MonkeyPatch):
        patch_model_resolution(monkeypatch, output_text="ok")
        async with client_app() as (client, _app):
            created = await client.post("/chat", json={"prompt": "hi", "permission_level": "plan"})
            conversation_id = s(json_body(created)["conversation_id"])
            _ = await collect_sse_events(client, s(json_body(created)["run_id"]))
            detail = await client.get(f"/conversations/{conversation_id}")
            assert json_body(detail)["permission_level"] == "plan"

            # ...and the next send moves it, without a separate call.
            again = await client.post(
                "/chat",
                json={
                    "prompt": "go on",
                    "conversation_id": conversation_id,
                    "permission_level": "edit",
                },
            )
            _ = await collect_sse_events(client, s(json_body(again)["run_id"]))
            detail = await client.get(f"/conversations/{conversation_id}")
            assert json_body(detail)["permission_level"] == "edit"

    async def test_a_send_without_a_level_leaves_the_thread_where_it_was(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        patch_model_resolution(monkeypatch, output_text="ok")
        async with client_app() as (client, _app):
            created = await client.post(
                "/chat", json={"prompt": "hi", "permission_level": "manual"}
            )
            conversation_id = s(json_body(created)["conversation_id"])
            _ = await collect_sse_events(client, s(json_body(created)["run_id"]))
            again = await client.post(
                "/chat", json={"prompt": "more", "conversation_id": conversation_id}
            )
            _ = await collect_sse_events(client, s(json_body(again)["run_id"]))
            detail = await client.get(f"/conversations/{conversation_id}")
            assert json_body(detail)["permission_level"] == "manual"

    async def test_a_send_that_is_steered_into_a_live_run_leaves_the_level_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The stored level is what the *next* turn runs at, and it is what the composer
        shows. A plain-text send onto a busy thread is queued into the run already going,
        which keeps running at the level it started at — so moving the stored one would
        leave the control reading a level nothing is running at."""
        hang, started = asyncio.Event(), asyncio.Event()
        patch_hanging_model(monkeypatch, hang, started)
        async with client_app() as (client, app):
            first = await client.post("/chat", json={"prompt": "hi", "permission_level": "manual"})
            conversation_id = s(json_body(first)["conversation_id"])
            _ = await started.wait()

            steered = await client.post(
                "/chat",
                json={
                    "prompt": "and this too",
                    "conversation_id": conversation_id,
                    "permission_level": "auto",
                },
            )
            assert steered.status_code == 202
            detail = await client.get(f"/conversations/{conversation_id}")
            assert json_body(detail)["permission_level"] == "manual"

            hang.set()
            first_run = app_state(app).runs.get(s(json_body(first)["run_id"]))
            assert first_run is not None
            await first_run.wait()

    async def test_a_queued_send_leaves_the_level_alone(self, monkeypatch: pytest.MonkeyPatch):
        """A send into a busy thread queues rather than running at the level it asked for,
        so the stored level stays what the running turn is running at."""
        hang, started = asyncio.Event(), asyncio.Event()
        patch_hanging_model(monkeypatch, hang, started)
        async with client_app() as (client, app):
            upload = await client.post(
                "/uploads", files={"file": ("note.txt", b"look at this", "text/plain")}
            )
            first = await client.post("/chat", json={"prompt": "hi", "permission_level": "manual"})
            conversation_id = s(json_body(first)["conversation_id"])
            _ = await started.wait()

            queued = await client.post(
                "/chat",
                json={
                    "prompt": "again",
                    "conversation_id": conversation_id,
                    "permission_level": "auto",
                    "attachment_ids": [json_body(upload)["id"]],
                },
            )
            assert queued.status_code == 202
            assert json_body(queued)["queued_message_id"]
            detail = await client.get(f"/conversations/{conversation_id}")
            assert json_body(detail)["permission_level"] == "manual"

            hang.set()
            await settle(app)

    async def test_an_unknown_level_is_refused_at_the_edge(self, monkeypatch: pytest.MonkeyPatch):
        patch_model_resolution(monkeypatch, output_text="ok")
        async with client_app() as (client, _app):
            resp = await client.post("/chat", json={"prompt": "hi", "permission_level": "root"})
            assert resp.status_code == 422

    async def test_a_thread_with_no_plan_reports_none_rather_than_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """ "This thread has no plan" is the ordinary state of nearly every thread, not a
        miss — so the backfill the panel reads answers `null` rather than 404, and the
        level stays exactly where the operator put it."""
        patch_model_resolution(monkeypatch, output_text="no plan here")
        async with client_app() as (client, _app):
            created = await client.post("/chat", json={"prompt": "hi", "permission_level": "plan"})
            conversation_id = s(json_body(created)["conversation_id"])
            _ = await collect_sse_events(client, s(json_body(created)["run_id"]))

            plan = await client.get(f"/conversations/{conversation_id}/plan")
            assert parse_json(plan.content) is None
            detail = await client.get(f"/conversations/{conversation_id}")
            assert json_body(detail)["permission_level"] == "plan"


def _write_categories(calls: list[str]) -> dict[str, FunctionToolset[RunDeps]]:
    """A category whose one tool classifies as a workspace write — and carries no marking
    of its own, so whether its call runs is entirely the level's answer."""
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain
    def write_file(path: str) -> str:
        calls.append(path)
        return f"wrote {path}"

    return {"files": toolset}


async def _run_at(level: PermissionLevel, calls: list[str]) -> Run:
    registry = RunRegistry()
    orchestrator = build_chat_orchestrator(
        "write the file",
        model=TestModel(custom_output_text="done"),
        categories=_write_categories(calls),
        binding=ConversationBinding(permission=level),
    )
    run = registry.submit(kind="chat", owner_id=OWNER, orchestrator=orchestrator)
    await run.wait()
    return run


class TestATurnUnderALevel:
    """The two halves, end to end: a real turn, a real tool, and the level deciding
    whether the call ever happens."""

    async def test_edit_lets_a_workspace_write_through(self):
        calls: list[str] = []
        run = await _run_at("edit", calls)
        assert run.status is RunStatus.done
        assert calls  # ran with no operator round-trip

    async def test_manual_parks_the_same_call(self):
        calls: list[str] = []
        run = await _run_at("manual", calls)
        assert run.status is RunStatus.awaiting_input
        assert not calls  # requested, not executed
        assert "approval.required" in [e.body.type for e in run.stream.replay()]

    async def test_plan_refuses_it_without_asking_anyone(self):
        calls: list[str] = []
        run = await _run_at("plan", calls)
        # The run finished on its own: the model was told the level does not permit this
        # and answered anyway. Nothing ran, and nothing waited on the operator.
        assert run.status is RunStatus.done
        assert not calls
        assert "approval.required" not in [e.body.type for e in run.stream.replay()]
