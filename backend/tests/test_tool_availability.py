"""The seventh axis — a category whose backing service the operator never set up.

``mail``, ``calendar`` and ``vault`` are the three groups that can be fully registered,
fully enabled, and still have nothing behind them: no mailbox connected, no calendar
added, no secrets vault created. Before this axis existed the model was handed all
fourteen of those tools on a fresh install and learned what the catalog already knew, one
failed call at a time.

Most of it runs against a **booted app**, because the fact under test is a wiring one: a
manifest declares the check, the assembly resolves it to namespaced tool names, and the
one funnel every run path composes its disabled set through has to ask it. Testing the
function alone would pass while any of those three came apart.
"""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from core.vault import Vault
from runs import Run, RunStream
from services.calendar import CalendarService
from services.mail import MailService
from services.offline import OfflineModeService
from services.secret_vault import SecretVaultService
from services.settings_store import SettingsStore
from services.tool_policy import (
    CategoryAvailability,
    effective_disabled_tools,
    unavailable_tools,
)
from tools import RunDeps
from tools.catalog import tool_catalog
from tools.describe import category_names
from tools.tool_search import dormant_index_instructions

from ._helpers import app_state, client_app, full_tool_categories

OWNER = "operator"

#: The categories that carry a backing-service question, restated so one quietly gaining
#: or losing its check is a failing test rather than a silent change in what a fresh
#: install offers the model.
CHECKED = {"calendar", "mail", "vault"}


def _ctx(disabled: frozenset[str]) -> RunContext[RunDeps]:
    run = Run(id="t", kind="chat", owner_id=OWNER, stream=RunStream())
    deps = RunDeps(run=run, owner_id=OWNER, disabled_tools=disabled)
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


def _availability(app: FastAPI) -> tuple[CategoryAvailability, ...]:
    return cast(tuple[CategoryAvailability, ...], app_state(app, "category_availability", tuple))


def _tools(app: FastAPI, category: str) -> frozenset[str]:
    """The namespaced names the assembly resolved for one checked category."""
    (entry,) = [e for e in _availability(app) if e.category == category]
    return entry.tools


def _caps(app: FastAPI) -> ServiceContainer:
    return app_state(app, "capabilities", ServiceContainer)


async def _withheld(app: FastAPI) -> frozenset[str]:
    """What the app's own disabled set holds for a turn with nothing else against it —
    every other axis at its permissive default, so what is left is this one."""
    return await effective_disabled_tools(
        app_state(app, "settings_store", SettingsStore),
        app_state(app, "offline", OfflineModeService),
        OWNER,
        availability=_availability(app),
        caps=_caps(app),
    )


async def _connect_mailbox(app: FastAPI) -> None:
    _ = (
        await _caps(app)
        .get(MailService)
        .create_account(OWNER, name="Personal", address="operator@example.com", password="secret")
    )


# --- the declaration and its assembly ------------------------------------------------


async def test_the_checked_categories_are_the_ones_with_a_service_to_configure():
    async with client_app() as (_, app):
        assert {e.category for e in _availability(app)} == CHECKED


async def test_each_check_resolves_to_the_real_tools_of_its_category():
    """A check whose name set were empty would pass every other assertion here while
    withholding nothing at all, so the resolution is pinned against the live catalog."""
    async with client_app() as (_, app):
        categories = cast(
            dict[str, AbstractToolset[RunDeps]], app_state(app, "tool_categories", dict)
        )
        catalog = tool_catalog(categories)
        for entry in _availability(app):
            registered = {i.name for i in catalog if i.category == entry.category}
            assert entry.tools == registered
            assert entry.tools


# --- a fresh install offers none of them ---------------------------------------------


async def test_a_fresh_install_withholds_every_unconfigured_category():
    async with client_app() as (_, app):
        withheld = await _withheld(app)
        for category in CHECKED:
            assert _tools(app, category) <= withheld


async def test_connecting_a_mailbox_offers_the_mail_tools():
    async with client_app() as (_, app):
        await _connect_mailbox(app)
        withheld = await _withheld(app)
        assert not (_tools(app, "mail") & withheld)
        # And only mail moved: connecting an account says nothing about the other two.
        assert _tools(app, "calendar") <= withheld
        assert _tools(app, "vault") <= withheld


async def test_adding_a_calendar_offers_the_calendar_tools():
    async with client_app() as (_, app):
        _ = await _caps(app).get(CalendarService).create_calendar(OWNER, "Work")
        withheld = await _withheld(app)
        assert not (_tools(app, "calendar") & withheld)
        assert _tools(app, "mail") <= withheld


async def test_configuring_the_vault_offers_the_vault_tools():
    async with client_app() as (_, app):
        await _caps(app).get(SecretVaultService).configure(OWNER, "a-passphrase")
        assert not (_tools(app, "vault") & await _withheld(app))


async def test_a_locked_vault_is_still_a_configured_one():
    """Locked is a state the vault tools are built for — the agent asks, the operator
    opens it, the work continues. Withholding on a lock would take the tools away exactly
    when the model was about to ask for them back, so the check reads ``configured``."""
    async with client_app() as (_, app):
        secrets = _caps(app).get(SecretVaultService)
        await secrets.configure(OWNER, "a-passphrase")
        secrets.logout()
        assert not (await secrets.status(OWNER)).unlocked
        assert not (_tools(app, "vault") & await _withheld(app))


async def test_a_locked_vault_still_answers_for_mail_and_calendar():
    """Whether a mailbox exists is a question about rows, not about their contents, and
    the check runs on every turn — including the turns before the operator has unlocked.
    Answering it by building views decrypts a column per account and raises while locked,
    which fails closed and takes both categories away exactly when nothing is wrong."""
    async with client_app() as (_, app):
        await _connect_mailbox(app)
        _ = await _caps(app).get(CalendarService).create_calendar(OWNER, "Work")
        app_state(app, "vault", Vault).lock()
        withheld = await _withheld(app)
        assert not (_tools(app, "mail") & withheld)
        assert not (_tools(app, "calendar") & withheld)


async def test_this_axis_reaches_no_further_than_the_categories_that_declared_it():
    """Asked on its own rather than through the union, because the union is where a
    reach past the declaring categories would hide behind mode's own withholdings."""
    async with client_app() as (_, app):
        checked = {name for e in _availability(app) for name in e.tools}
        withheld = await unavailable_tools(_availability(app), _caps(app), OWNER)
        assert withheld == checked
        await _connect_mailbox(app)
        withheld = await unavailable_tools(_availability(app), _caps(app), OWNER)
        assert withheld == checked - _tools(app, "mail")


# --- a broken check fails closed -----------------------------------------------------


async def _raises(_caps: ServiceContainer, _owner: str) -> bool:
    raise RuntimeError("the service is in pieces")


async def _yes(_caps: ServiceContainer, _owner: str) -> bool:
    return True


async def test_a_check_that_raises_withholds_only_its_own_category(
    caplog: pytest.LogCaptureFixture,
):
    broken = CategoryAvailability("mail", frozenset({"mail_send"}), _raises)
    fine = CategoryAvailability("calendar", frozenset({"calendar_agenda"}), _yes)
    with caplog.at_level("WARNING"):
        withheld = await unavailable_tools([broken, fine], ServiceContainer(), OWNER)
    assert withheld == {"mail_send"}
    assert "mail" in caplog.text


async def test_no_bag_to_ask_with_withholds_everything_declared():
    """Fail closed for the reason a raising check does: unconfigured is the ordinary
    state of these features, so being unable to ask is far likelier to mean "there is
    nothing there" than "this would have worked"."""
    entry = CategoryAvailability("mail", frozenset({"mail_send"}), _yes)
    assert await unavailable_tools([entry], None, OWNER) == {"mail_send"}


async def test_a_caller_that_declares_nothing_loses_nothing():
    """The default. A run path with no availability to pass is saying nothing about
    availability, and must not lose tools to the absence of an opinion."""
    assert await unavailable_tools([], None, OWNER) == frozenset()


# --- the agent's index agrees with the catalog ---------------------------------------


async def test_an_unconfigured_group_is_not_advertised_to_the_model():
    """The withholding and the standing index have to agree. A dormant group whose every
    tool is withheld drops out of the index, so a model is never told about a mailbox it
    would spend a round trip revealing to find empty."""
    provider = dormant_index_instructions(
        {"mail": "the operator's email", "calendar": "the operator's calendars"},
        category_names(full_tool_categories()),
    )
    async with client_app() as (_, app):
        text = await provider(_ctx(await _withheld(app)))
        assert "mail:" not in text
        assert "calendar:" not in text

        await _connect_mailbox(app)
        text = await provider(_ctx(await _withheld(app)))
        assert "mail:" in text
        assert "calendar:" not in text
