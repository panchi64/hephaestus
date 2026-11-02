"""Trial arms: variants resolve against the register, and a trial that tested nothing says so."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.variant import PromptVariant
from evals.drive.proxy import RequestBody, parse_request, watched_in
from evals.harness.compare import Arm, landing
from evals.harness.evidence import ProxiedRequest
from evals.harness.implicit import CheckResult
from evals.harness.issues import collect
from evals.harness.record import Run, Sample
from evals.harness.run import main as run_main
from evals.harness.variants import (
    BASE,
    VARIANTS_DIR,
    ArmKey,
    Op,
    Probe,
    load_key,
    load_variant,
    write_arm,
)
from prompts.boundaries import register

SHIPPED = sorted(path.stem for path in VARIANTS_DIR.glob("*.toml"))


@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_variant_resolves_against_the_register(name: str):
    variant = load_variant(name)
    assert variant.rewrites()
    assert variant.probes()


def test_a_removal_probes_for_the_registered_sentence():
    variant = load_variant("without-h036-floor")
    text = register().by_id("B-083").text
    assert variant.rewrites() == [{"find": text, "replace": ""}]
    assert variant.probes() == [Probe(text=text, present=False)]


def test_an_addition_lands_after_its_anchor():
    variant = load_variant("h036-in-tool")
    anchor = register().by_id("B-495").text
    removal, edit = variant.rewrites()
    assert removal == {"find": register().by_id("B-083").text, "replace": ""}
    assert edit["find"] == anchor
    assert edit["replace"].startswith(f"{anchor} ")


def test_an_op_is_one_shape_or_the_other():
    with pytest.raises(ValidationError):
        _ = Op(boundary="B-001", action="remove", add="x", after="B-002")
    with pytest.raises(ValidationError):
        _ = Op(boundary="B-001", action="replace")


def test_an_unknown_id_fails_before_any_model_is_called(tmp_path: Path):
    _ = (tmp_path / "bad.toml").write_text('[[op]]\nboundary = "B-999999"\naction = "remove"\n')
    with pytest.raises(KeyError):
        _ = load_variant("bad", tmp_path)


async def test_an_unknown_arm_stops_the_run(capsys: pytest.CaptureFixture[str]):
    assert await run_main(["--arms", BASE, "no-such-variant"]) == 2
    assert "no variant 'no-such-variant'" in capsys.readouterr().out


async def test_the_arm_file_is_what_the_backend_reads(tmp_path: Path):
    path = tmp_path / "variant.json"
    variant = PromptVariant(path)
    write_arm(path, load_variant("without-h036-floor"))
    assert len(variant.rewrites()) == 1
    write_arm(path, None)
    assert variant.rewrites() == ()
    assert [p.name for p in tmp_path.iterdir()] == ["variant.json"]


def test_an_unreadable_arm_file_applies_nothing(tmp_path: Path):
    path = tmp_path / "variant.json"
    _ = path.write_text('{"rewrite": [{"find": ')
    assert PromptVariant(path).rewrites() == ()


async def test_a_bad_variant_file_stops_the_run(tmp_path: Path):
    _ = (tmp_path / "bad.toml").write_text('[[op]]\nboundary = "B-001"\naction = "replace"\n')
    with pytest.raises(ValidationError):
        _ = load_variant("bad", tmp_path)


async def test_an_arm_named_twice_stops_the_run(capsys: pytest.CaptureFixture[str]):
    assert await run_main(["--arms", BASE, BASE]) == 2
    assert "names an arm twice" in capsys.readouterr().out


def _body(**fields: object) -> RequestBody:
    return parse_request(json.dumps(fields).encode())


def test_the_proxy_notes_a_sentence_however_it_is_wrapped():
    wrapped = "Plan.\nThree or more\n steps always gets a list."
    body = _body(messages=[{"role": "system", "content": wrapped}])
    found = watched_in(body, ("Three or more steps always gets a list.", "Absent."))
    assert found == ("Three or more steps always gets a list.",)


def test_the_proxy_reads_prompts_and_tools_but_not_history():
    rule = "Three or more steps always gets a list."
    assert watched_in(_body(system=[{"type": "text", "text": rule}]), (rule,)) == (rule,)
    tool = {"type": "function", "function": {"name": "t", "description": rule}}
    assert watched_in(_body(tools=[tool]), (rule,)) == (rule,)
    quoted = [{"role": "tool", "content": rule}, {"role": "user", "content": rule}]
    assert watched_in(_body(messages=quoted), (rule,)) == ()


def test_a_trial_key_keeps_arms_out_of_the_run_and_reads_old_keys(tmp_path: Path):
    entry = ArmKey(model="m", arm="v", probes=(Probe(text="x", present=False),))
    _ = (tmp_path / "key.json").write_text(
        json.dumps({"A": entry.model_dump(), "B": "old-model", "C": None})
    )
    key = load_key(tmp_path)
    assert key["A"] == entry
    assert key["B"] == ArmKey(model="old-model")
    assert key["C"].arm == BASE
    assert "arm" not in Run.model_fields


def test_the_backlog_merge_leaves_variant_arms_out(tmp_path: Path):
    failing = Sample(
        scenario="main-multi-step",
        sample=1,
        checks=[CheckResult(label="lists", passed=False, kind="behavior", detail="no list")],
    )
    for letter, arm in (("A", BASE), ("B", "without-h036-floor")):
        run = _run(arm, sent=True)
        run.samples = [failing]
        (tmp_path / letter).mkdir()
        _ = (tmp_path / letter / "run.json").write_text(run.model_dump_json())
    key = {"A": ArmKey(arm=BASE).model_dump(), "B": ArmKey(arm="v").model_dump()}
    _ = (tmp_path / "key.json").write_text(json.dumps(key))
    assert collect(tmp_path).found[0].samples == 1


RULE = "A rule."


def _run(name: str, *, sent: bool) -> Run:
    request = ProxiedRequest(endpoint="e", model="m", tools=True, seen=(RULE,) if sent else ())
    return Run(
        group="g",
        name=name,
        started_at="now",
        prompt_fingerprint="f",
        stub=True,
        samples=[Sample(scenario="s", sample=1, requests=[request])],
    )


def _arm(letter: str, arm: str, *, sent: bool, probes: tuple[Probe, ...] = ()) -> Arm:
    return Arm(letter, ArmKey(arm=arm, probes=probes), _run(letter, sent=sent), None)


def test_a_removal_that_held_has_landed():
    removed = (Probe(text=RULE, present=False),)
    arms = [_arm("A", BASE, sent=True), _arm("B", "without", sent=False, probes=removed)]
    assert landing(arms) == {"without (B)": []}


def _trial(*, base_sent: bool, variant_sent: bool, probe: Probe) -> list[str]:
    arms = [_arm("A", BASE, sent=base_sent), _arm("B", "v", sent=variant_sent, probes=(probe,))]
    return landing(arms)["v (B)"]


def test_a_removal_still_sent_or_never_exercised_is_invalid():
    removed = Probe(text=RULE, present=False)
    still_sent = _trial(base_sent=True, variant_sent=True, probe=removed)
    never_seen = _trial(base_sent=False, variant_sent=False, probe=removed)
    assert any("still sent" in p for p in still_sent)
    assert any("no base arm" in p for p in never_seen)


def test_an_addition_never_sent_is_invalid():
    added = Probe(text=RULE, present=True)
    assert any("never sent" in p for p in _trial(base_sent=False, variant_sent=False, probe=added))
