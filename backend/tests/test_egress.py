"""The one egress allowlist: what a workspace may reach."""

from __future__ import annotations

import pytest

from core.db import init_db, make_engine
from core.exceptions import InvalidInputError
from services.egress import EgressPolicy, normalise_domain

GLOBAL = ("pypi.org", "files.pythonhosted.org")
CONV = "conv-1"


def _policy(global_domains: tuple[str, ...] = GLOBAL) -> EgressPolicy:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return EgressPolicy(engine, global_domains)


# --- the union --------------------------------------------------------------


async def test_a_workspace_starts_with_the_installation_wide_list():
    policy = _policy()
    assert await policy.allowed_for(CONV) == frozenset(GLOBAL)


async def test_a_grant_adds_to_the_global_list_without_replacing_it():
    policy = _policy()
    allowed = await policy.allow(CONV, ["example.com"])
    assert allowed == frozenset([*GLOBAL, "example.com"])
    assert await policy.allowed_for(CONV) == allowed


async def test_grants_do_not_leak_between_workspaces():
    policy = _policy()
    _ = await policy.allow(CONV, ["example.com"])
    assert await policy.allowed_for("conv-2") == frozenset(GLOBAL)


async def test_granting_the_same_domain_twice_is_a_no_op():
    # The unique constraint is resolved in the database, so a re-approval is not an
    # IntegrityError and does not double the row.
    policy = _policy()
    _ = await policy.allow(CONV, ["example.com"])
    assert await policy.allow(CONV, ["EXAMPLE.com."]) == frozenset([*GLOBAL, "example.com"])


async def test_a_stateless_run_key_gets_its_own_allowlist():
    # A run without a conversation keys its workspace off the run id; the policy cannot
    # tell the two apart and must not have to.
    policy = _policy()
    _ = await policy.allow("run-abc", ["example.com"])
    assert await policy.allowed_for("run-abc") == frozenset([*GLOBAL, "example.com"])
    assert await policy.allowed_for(CONV) == frozenset(GLOBAL)


async def test_a_workspaceless_run_gets_the_global_list_and_cannot_be_granted():
    policy = _policy()
    assert await policy.allowed_for("") == frozenset(GLOBAL)
    with pytest.raises(InvalidInputError):
        _ = await policy.allow("", ["example.com"])


# --- normalisation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Example.COM", "example.com"),
        ("https://example.com", "example.com"),
        ("http://example.com/simple/index.html", "example.com"),
        ("example.com:8443", "example.com"),
        ("example.com.", "example.com"),
        ("  example.com  ", "example.com"),
        ("https://example.com:443/x?y=1#z", "example.com"),
        ("*.example.com", "*.example.com"),
        ("HTTPS://*.Example.com/", "*.example.com"),
        ("sub.example.co.uk", "sub.example.co.uk"),
        # The host is the one before the path, not one hidden inside it: an approval card
        # reading `pypi.org` must never grant what someone appended after the `?`.
        ("https://pypi.org/simple?next=https://evil.example.net", "pypi.org"),
        ("https://pypi.org/redirect#https://evil.example.net", "pypi.org"),
        ("http://good.example.com/a://b.example.com", "good.example.com"),
    ],
)
def test_normalisation_keeps_the_host_and_drops_everything_else(raw: str, expected: str):
    assert normalise_domain(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://",
        "1.2.3.4",
        "https://192.168.0.1:8080/x",
        "[::1]",
        "[2001:db8::1]:443",
        "*",
        "*.",
        "*example.com",
        "a.*.com",
        "exa mple.com",
        "-example.com",
        "example-.com",
        "exam_ple.com",
        # A whole top level behind a card that reads like one host.
        "com",
        "*.com",
        # Addresses in the spellings a resolver still expands.
        "127.1",
        "2130706433",
        # Userinfo: the host is the half *after* the `@`, so the card reads as the
        # allowlisted name and the grant is somebody else's.
        "pypi.org@evil.example.net",
        "https://user:pw@example.com",
        # An embedded newline would be a second line in the file the fences read.
        "good.example.com\n#x",
        "pypi.org\n.evil.example.net",
    ],
)
def test_normalisation_refuses_anything_wider_than_it_reads(raw: str):
    with pytest.raises(InvalidInputError):
        _ = normalise_domain(raw)


async def test_allow_refuses_the_whole_batch_when_one_domain_is_unusable():
    policy = _policy()
    with pytest.raises(InvalidInputError):
        _ = await policy.allow(CONV, ["example.com", "10.0.0.1"])
    assert await policy.allowed_for(CONV) == frozenset(GLOBAL)


def test_a_malformed_configured_domain_fails_at_construction():
    with pytest.raises(InvalidInputError):
        _ = _policy(("pypi.org", "not a domain"))


# --- forgetting -------------------------------------------------------------


async def test_forget_drops_the_grants():
    policy = _policy()
    _ = await policy.allow(CONV, ["example.com"])

    await policy.forget(CONV)

    assert await policy.allowed_for(CONV) == frozenset(GLOBAL)


async def test_forget_leaves_other_workspaces_alone():
    policy = _policy()
    _ = await policy.allow(CONV, ["example.com"])
    _ = await policy.allow("conv-2", ["other.example.com"])

    await policy.forget(CONV)

    assert await policy.allowed_for("conv-2") == frozenset([*GLOBAL, "other.example.com"])


async def test_forget_is_a_no_op_for_a_conversation_that_never_asked():
    policy = _policy()
    await policy.forget("conv-never")  # no rows, no directory, no error
    assert await policy.allowed_for("conv-never") == frozenset(GLOBAL)
