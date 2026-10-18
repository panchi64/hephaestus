"""Classes and callsigns: how an agent gets the names the operator knows it by, and how a
reference by either name finds it again."""

from __future__ import annotations

import random

import pytest

from harness.manifests._subagent_wake import report_text
from models.subagent import SubagentRecord
from prompts.voice import CLASSES
from routes.deps import app_state
from services.agents import builtin_roster, describe_roster
from services.agents.definition import class_key
from services.agents.files import AgentFileError, parse_agent_file, render_agent_file
from services.subagents import SubagentParent, SubagentUnavailableError
from services.subagents.callsigns import (
    allocate,
    designation,
    next_serial,
    resolve,
    stem_of,
    with_serial,
)
from tests._helpers import client_app, patch_model_resolution
from tests.test_subagents_launch import launcher_of, settle_run

TRIPODES = CLASSES["tripodes"].callsigns
KOURAI = CLASSES["kourai"].callsigns


def _row(handle: str, callsign: str, status: str = "running") -> SubagentRecord:
    return SubagentRecord(
        owner_id="operator",
        parent_conversation_id="root",
        child_conversation_id=f"child-{handle}",
        run_id=f"run-{handle}",
        spec_name="worker",
        handle=handle,
        callsign=callsign,
        agent_class="tripodes",
        task_enc="",
        status=status,
    )


class TestAllocate:
    def test_it_draws_from_the_class_pool(self):
        assert allocate("tripodes", ()) in TRIPODES

    def test_it_never_repeats_a_taken_callsign(self):
        taken = set(TRIPODES[:-1])
        assert allocate("tripodes", taken) == TRIPODES[-1]

    def test_taken_is_compared_without_case(self):
        taken = {name.lower() for name in TRIPODES[:-1]}
        assert allocate("tripodes", taken) == TRIPODES[-1]

    def test_it_passes_over_avoided_names_while_others_are_free(self):
        avoid = set(TRIPODES[1:])
        assert allocate("tripodes", (), avoid=avoid) == TRIPODES[0]

    def test_an_avoided_name_is_still_used_once_nothing_else_is_free(self):
        assert allocate("tripodes", TRIPODES[1:], avoid={TRIPODES[0]}) == TRIPODES[0]

    def test_an_exhausted_pool_reuses_a_stem(self):
        assert allocate("tripodes", TRIPODES, rng=random.Random(0)) in TRIPODES


class TestSerial:
    def test_it_is_padded_to_two_digits_and_grows_past_them(self):
        assert with_serial("BRAX", 7) == "BRAX-07"
        assert with_serial("BRAX", 131) == "BRAX-131"

    def test_the_next_serial_is_one_past_the_highest_carried(self):
        assert next_serial(["BRAX-03", "TIKK-11", "GORM-02"]) == 12

    def test_the_first_serial_is_one(self):
        assert next_serial([]) == 1

    def test_a_callsign_from_before_serials_counts_as_none(self):
        assert next_serial(["BRAX"]) == 1
        assert stem_of("BRAX") == "BRAX"
        assert stem_of("BRAX-07") == "BRAX"

    def test_a_kourai_callsign_ends_in_e_macron(self):
        for seed in range(20):
            assert allocate("kourai", (), rng=random.Random(seed)).endswith("Ē")

    def test_a_kourai_callsign_taken_without_the_macron_is_still_taken(self):
        taken = {name.replace("Ē", "E") for name in KOURAI[:-1]}
        assert allocate("kourai", taken) == KOURAI[-1]

    def test_a_class_without_a_pool_gets_no_callsign(self):
        assert allocate("talos", ()) == ""
        assert allocate("nonsense", ()) == ""


class TestResolve:
    def test_a_handle_resolves(self):
        rows = [_row("migrate-schema", "BRAX"), _row("backfill", "TIKK")]
        assert resolve("backfill", rows) is rows[1]

    def test_a_callsign_resolves_in_any_case(self):
        rows = [_row("migrate-schema", "BRAX"), _row("backfill", "TIKK")]
        assert resolve("brax", rows) is rows[0]
        assert resolve("Tikk", rows) is rows[1]

    def test_a_callsign_resolves_with_or_without_its_serial(self):
        rows = [_row("migrate-schema", "BRAX-07"), _row("backfill", "TIKK-08")]
        assert resolve("brax-07", rows) is rows[0]
        assert resolve("BRAX", rows) is rows[0]

    def test_a_full_callsign_settles_a_stem_two_agents_share(self):
        rows = [_row("first", "BRAX-07"), _row("second", "BRAX-12")]
        assert resolve("BRAX-12", rows) is rows[1]
        with pytest.raises(SubagentUnavailableError, match="more than one"):
            _ = resolve("brax", rows)

    def test_the_macron_folds_to_e(self):
        rows = [_row("auth-research", "KYRĒ")]
        assert resolve("kyre", rows) is rows[0]
        assert resolve("KYRE", rows) is rows[0]
        assert resolve("kyrē", rows) is rows[0]

    def test_a_live_agent_wins_over_a_finished_one_with_its_callsign(self):
        rows = [_row("old", "BRAX", status="done"), _row("new", "BRAX")]
        assert resolve("BRAX", rows) is rows[1]

    def test_an_ambiguous_callsign_is_refused_with_help(self):
        rows = [_row("one", "BRAX"), _row("two", "BRAX")]
        with pytest.raises(SubagentUnavailableError) as raised:
            _ = resolve("brax", rows)
        message = str(raised.value)
        assert message.startswith("error: ")
        assert "`one`" in message and "`two`" in message
        assert "\n  help: " in message

    def test_an_unknown_reference_is_refused_with_help(self):
        rows = [_row("migrate-schema", "BRAX")]
        with pytest.raises(SubagentUnavailableError) as raised:
            _ = resolve("BRAK", rows)
        message = str(raised.value)
        assert message.startswith("error: nothing in your delegation tree is called 'BRAK'")
        assert "help: did you mean `BRAX`?" in message
        assert "help: call agents_list" in message


class TestClasses:
    def test_the_builtins_have_their_classes(self):
        roster = builtin_roster()
        assert {name: agent.agent_class for name, agent in roster.items()} == {
            "default": "talos",
            "explorer": "kourai",
            "researcher": "kourai",
            "worker": "tripodes",
            "reviewer": "khryseos",
            "test_runner": "argyreos",
        }

    def test_the_roster_names_each_agents_class(self):
        described = describe_roster(builtin_roster())
        assert "`worker` (Tripodes) — " in described
        assert "`reviewer` (Khryseos) — " in described

    def test_a_file_without_a_class_is_tripodes(self):
        text = "---\nname: scout\ndescription: Looks around.\n---\n\nLook around.\n"
        assert parse_agent_file(text, name="scout").agent_class == "tripodes"

    @pytest.mark.parametrize("declared", ["kourai", "Kourai", "KOURAI"])
    def test_a_file_names_its_class(self, declared: str):
        text = f"---\nname: scout\ndescription: Looks.\nclass: {declared}\n---\n\nLook.\n"
        assert parse_agent_file(text, name="scout").agent_class == "kourai"

    def test_a_display_name_with_macrons_is_read(self):
        assert class_key("Kēlēdones") == "keledones"

    def test_a_helper_class_fails_the_file(self):
        text = "---\nname: scout\ndescription: Looks.\nclass: Kēlēdones\n---\n\nLook.\n"
        with pytest.raises(AgentFileError, match="unknown class 'Kēlēdones'"):
            _ = parse_agent_file(text, name="scout")

    def test_an_unknown_class_fails_the_file_naming_the_classes(self):
        text = "---\nname: scout\ndescription: Looks.\nclass: golem\n---\n\nLook.\n"
        with pytest.raises(AgentFileError, match="unknown class 'golem'.*tripodes"):
            _ = parse_agent_file(text, name="scout")

    def test_the_class_survives_a_round_trip(self):
        text = "---\nname: scout\ndescription: Looks.\nclass: khryseos\n---\n\nLook.\n"
        rendered = render_agent_file(parse_agent_file(text, name="scout"))
        assert parse_agent_file(rendered, name="scout").agent_class == "khryseos"


class TestWhatTheModelSees:
    def test_a_report_names_the_handle_callsign_and_class(self):
        text = report_text(
            "done",
            "migrate-schema",
            "worker",
            "done",
            None,
            None,
            callsign="BRAX",
            agent_class="tripodes",
        )
        assert text.startswith("`migrate-schema` (BRAX · Tripodes, worker): done.")

    def test_a_designation_without_a_callsign_is_empty(self):
        assert designation("", "tripodes") == ""
        assert designation("BRAX", "tripodes") == "BRAX · Tripodes"


class TestSpawning:
    async def test_callsigns_are_unique_within_a_tree_and_resolve(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        worker = builtin_roster()["worker"]
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            root = await app_state(app).conversations.create_conversation("operator", title="root")
            launcher = launcher_of(app)
            started = [
                await launcher.launch(
                    "operator",
                    worker,
                    "do the thing",
                    handle=f"job-{index}",
                    parent=SubagentParent(conversation_id=root),
                )
                for index in range(6)
            ]
            callsigns = [each.callsign for each in started]
            stems = [stem_of(callsign) for callsign in callsigns]
            assert len(set(stems)) == len(stems)
            assert all(stem in TRIPODES for stem in stems)
            assert [callsign.rpartition("-")[2] for callsign in callsigns] == [
                f"{serial:02d}" for serial in range(1, 7)
            ]
            assert {each.agent_class for each in started} == {"tripodes"}

            view = await launcher.read("operator", root, started[2].callsign.lower())
            assert (view.handle, view.callsign) == ("job-2", started[2].callsign)
            for each in started:
                await settle_run(app, each.run_id)
