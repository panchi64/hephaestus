"""The one egress allowlist: what a workspace may reach, and the file the fences read."""

from __future__ import annotations

import os

import pytest

from core.db import init_db, make_engine
from core.exceptions import InvalidInputError
from services.egress import ALLOW_FILE, EgressPolicy, normalise_domain

GLOBAL = ("pypi.org", "files.pythonhosted.org")
CONV = "conv-1"


def _policy(tmp_path, global_domains: tuple[str, ...] = GLOBAL) -> EgressPolicy:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return EgressPolicy(engine, tmp_path, global_domains)


def _allow_text(policy: EgressPolicy, key: str) -> str:
    return (policy.allow_dir(key) / ALLOW_FILE).read_text()


# --- the union --------------------------------------------------------------


async def test_a_workspace_starts_with_the_installation_wide_list(tmp_path):
    policy = _policy(tmp_path)
    assert await policy.allowed_for(CONV) == frozenset(GLOBAL)


async def test_a_grant_adds_to_the_global_list_without_replacing_it(tmp_path):
    policy = _policy(tmp_path)
    allowed = await policy.allow(CONV, ["example.com"])
    assert allowed == frozenset([*GLOBAL, "example.com"])
    assert await policy.allowed_for(CONV) == allowed


async def test_grants_do_not_leak_between_workspaces(tmp_path):
    policy = _policy(tmp_path)
    await policy.allow(CONV, ["example.com"])
    assert await policy.allowed_for("conv-2") == frozenset(GLOBAL)


async def test_granting_the_same_domain_twice_is_a_no_op(tmp_path):
    # The unique constraint is resolved in the database, so a re-approval is not an
    # IntegrityError and does not double the row.
    policy = _policy(tmp_path)
    await policy.allow(CONV, ["example.com"])
    assert await policy.allow(CONV, ["EXAMPLE.com."]) == frozenset([*GLOBAL, "example.com"])


async def test_a_stateless_run_key_gets_its_own_allowlist(tmp_path):
    # A run without a conversation keys its workspace off the run id; the policy cannot
    # tell the two apart and must not have to.
    policy = _policy(tmp_path)
    await policy.allow("run-abc", ["example.com"])
    assert await policy.allowed_for("run-abc") == frozenset([*GLOBAL, "example.com"])
    assert await policy.allowed_for(CONV) == frozenset(GLOBAL)


async def test_a_workspaceless_run_gets_the_global_list_and_cannot_be_granted(tmp_path):
    policy = _policy(tmp_path)
    assert await policy.allowed_for("") == frozenset(GLOBAL)
    with pytest.raises(InvalidInputError):
        await policy.allow("", ["example.com"])


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
def test_normalisation_keeps_the_host_and_drops_everything_else(raw, expected):
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
def test_normalisation_refuses_anything_wider_than_it_reads(raw):
    with pytest.raises(InvalidInputError):
        normalise_domain(raw)


async def test_allow_refuses_the_whole_batch_when_one_domain_is_unusable(tmp_path):
    policy = _policy(tmp_path)
    with pytest.raises(InvalidInputError):
        await policy.allow(CONV, ["example.com", "10.0.0.1"])
    assert await policy.allowed_for(CONV) == frozenset(GLOBAL)


def test_a_malformed_configured_domain_fails_at_construction(tmp_path):
    with pytest.raises(InvalidInputError):
        _policy(tmp_path, ("pypi.org", "not a domain"))


# --- materialising ----------------------------------------------------------


async def test_materialise_writes_a_sorted_directory_the_fence_can_mount(tmp_path):
    policy = _policy(tmp_path)
    await policy.allow(CONV, ["zed.example.com", "alpha.example.com"])

    directory = await policy.materialise(CONV)

    assert directory == policy.allow_dir(CONV)
    assert directory.is_dir()
    lines = _allow_text(policy, CONV).splitlines()
    assert lines == sorted(lines)
    assert set(lines) == await policy.allowed_for(CONV)
    # Nothing but the file the fence reads — a temp file left behind would be mounted too.
    assert [p.name for p in directory.iterdir()] == [ALLOW_FILE]


async def test_materialise_creates_the_directory_when_it_is_missing(tmp_path):
    policy = _policy(tmp_path)
    assert not policy.allow_dir(CONV).exists()
    await policy.materialise(CONV)
    assert set(_allow_text(policy, CONV).split()) == frozenset(GLOBAL)


async def test_a_second_write_replaces_the_file_rather_than_rewriting_it(tmp_path):
    # Atomicity is the point: a reader mid-request sees the old list or the new one, never
    # a truncated file. A reader holding the old file keeps reading the old list whole.
    # Holding it open also keeps its inode from being reused by a later write.
    policy = _policy(tmp_path)
    await policy.materialise(CONV)
    path = policy.allow_dir(CONV) / ALLOW_FILE
    with path.open(encoding="utf-8") as reader:
        before = os.fstat(reader.fileno())

        await policy.allow(CONV, ["example.com"])
        await policy.materialise(CONV)
        after = path.stat()

        assert "example.com" not in reader.read().split()
    assert "example.com" in _allow_text(policy, CONV).split()
    assert after.st_ino != before.st_ino
    assert after.st_mtime_ns != before.st_mtime_ns
    assert [p.name for p in policy.allow_dir(CONV).iterdir()] == [ALLOW_FILE]


# --- forgetting -------------------------------------------------------------


async def test_forget_drops_the_grants_and_the_materialised_file(tmp_path):
    policy = _policy(tmp_path)
    await policy.allow(CONV, ["example.com"])
    await policy.materialise(CONV)

    await policy.forget(CONV)

    assert await policy.allowed_for(CONV) == frozenset(GLOBAL)
    assert not policy.allow_dir(CONV).exists()


async def test_forget_leaves_other_workspaces_alone(tmp_path):
    policy = _policy(tmp_path)
    await policy.allow(CONV, ["example.com"])
    await policy.allow("conv-2", ["other.example.com"])

    await policy.forget(CONV)

    assert await policy.allowed_for("conv-2") == frozenset([*GLOBAL, "other.example.com"])


async def test_forget_is_a_no_op_for_a_conversation_that_never_asked(tmp_path):
    policy = _policy(tmp_path)
    await policy.forget("conv-never")  # no rows, no directory, no error
    assert await policy.allowed_for("conv-never") == frozenset(GLOBAL)
