"""Trial arms: variants resolve against the register, and a trial that tested nothing says so."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.variant import PromptVariant
from evals.drive.proxy import watched_in
from evals.harness.compare import Arm, landing
from evals.harness.evidence import ProxiedRequest
from evals.harness.record import Run, Sample
from evals.harness.run import main as run_main
from evals.harness.variants import BASE, VARIANTS_DIR, Op, Probe, load_variant, write_arm
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
    [edit] = variant.rewrites()
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
    write_arm(path, load_variant("without-h036-floor"))
    assert json.loads(path.read_text())["rewrite"][0]["replace"] == ""
    write_arm(path, None)
    assert json.loads(path.read_text()) == {"rewrite": []}
    _ = PromptVariant(path)


def test_the_proxy_notes_a_sentence_however_it_is_wrapped():
    wrapped = "Plan.\nThree or more\n steps always gets a list."
    body = json.dumps({"messages": [{"content": wrapped}]})
    found = watched_in(body.encode(), ("Three or more steps always gets a list.", "Absent."))
    assert found == ("Three or more steps always gets a list.",)


RULE = "A rule."


def _run(arm: str, *, sent: bool, probes: list[Probe] | None = None) -> Run:
    request = ProxiedRequest(endpoint="e", model="m", tools=True, seen=(RULE,) if sent else ())
    return Run(
        group="g",
        name=arm,
        started_at="now",
        prompt_fingerprint="f",
        arm=arm,
        probes=probes or [],
        stub=True,
        samples=[Sample(scenario="s", sample=1, requests=[request])],
    )


def test_a_removal_that_held_has_landed():
    removed = [Probe(text=RULE, present=False)]
    arms = [
        Arm("A", _run(BASE, sent=True), None),
        Arm("B", _run("without", sent=False, probes=removed), None),
    ]
    assert landing(arms) == {"without (B)": []}


def _trial(*, base_sent: bool, variant_sent: bool, probe: Probe) -> list[str]:
    arms = [
        Arm("A", _run(BASE, sent=base_sent), None),
        Arm("B", _run("v", sent=variant_sent, probes=[probe]), None),
    ]
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
