"""`command_prefixes`: what a standing grant on a command tool is scoped to, so that it
covers the act on another target and never a different act."""

from __future__ import annotations

import pytest

from services.permissions import command_prefixes


@pytest.mark.parametrize(
    ("command", "prefixes"),
    [
        ("uv run pytest tests/test_a.py -k thing", (("uv", "run", "pytest"),)),
        ("brew install ripgrep", (("brew", "install", "ripgrep"),)),
        ("git commit -m 'x'", (("git", "commit", "-m", "x"),)),
        ("ls", (("ls",),)),
    ],
)
def test_the_program_and_the_words_that_say_which_of_its_modes(
    command: str, prefixes: tuple[tuple[str, ...], ...]
):
    assert command_prefixes(command) == prefixes


@pytest.mark.parametrize(
    ("command", "prefixes"),
    [
        ("curl -sS https://api.example/x", (("curl", "-sS", "https://api.example/x"),)),
        ("env -i true", (("env", "-i", "true"),)),
        ("git --version", (("git", "--version"),)),
        ("npm -w pkg run build", (("npm", "-w", "pkg", "run"),)),
    ],
)
def test_a_flag_narrows_the_prefix_instead_of_ending_it(
    command: str, prefixes: tuple[tuple[str, ...], ...]
):
    # Ending at the first `-` let one yes to `curl -sS <url>` stand for `curl -d @secrets
    # <elsewhere>`. A flag is part of the act; what the cap withholds is the target.
    assert command_prefixes(command) == prefixes


def test_the_cap_counts_operands_and_stops_the_reading():
    assert command_prefixes("uv run pytest -k thing tests/a.py") == (("uv", "run", "pytest"),)


@pytest.mark.parametrize(
    ("command", "prefixes"),
    [
        ("git diff | head -20", (("git", "diff"), ("head", "-20"))),
        ("git add . && git commit -m x", (("git", "add", "."), ("git", "commit", "-m", "x"))),
    ],
)
def test_every_stage_answers_for_itself(command: str, prefixes: tuple[tuple[str, ...], ...]):
    assert command_prefixes(command) == prefixes


@pytest.mark.parametrize("command", ["cat $TARGET", "ls |", "$TOOL --version", ""])
def test_a_command_that_could_not_be_read_has_no_prefix(command: str):
    assert command_prefixes(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest > ~/.ssh/authorized_keys",
        "LD_PRELOAD=/tmp/evil.so uv run pytest",
        "cat ../outside/x",
        "cd .. && ls",
        "git --git-dir=/elsewhere/.git log",
        "sh -c 'cat /etc/passwd'",
    ],
)
def test_a_command_that_leaves_the_working_directory_has_no_prefix(command: str):
    # The same leading words, and not the same act.
    assert command_prefixes(command) is None


def test_a_program_name_that_is_more_than_one_word_is_not_a_name():
    assert command_prefixes("'my prog' arg") is None


def test_an_empty_argument_ends_the_prefix_rather_than_joining_it():
    assert command_prefixes('git commit ""') == (("git", "commit"),)


def test_every_word_of_a_prefix_survives_a_round_trip_through_display():
    for command in ("uv run pytest -k x", "git commit -m 'x'", "brew install ripgrep"):
        for prefix in command_prefixes(command) or ():
            assert all(word and word.split() == [word] for word in prefix)
