"""A repository's own config is content, and it must not be able to run a program.

`git status` and `git diff` read as observations — the deterministic stage cleared them as
such — but git consults `.git/config` on the way, and several of its keys name a program
git then executes: `core.fsmonitor` on a status, a pager on anything that prints. In a
worktree that file is the *main checkout's*, so it is not even the agent's own branch that
decides.

The reproduction below is the one from the review, run against a real repository with a
real hook. What it pins is not "git is safe" but that the environment we spawn commands in
takes those keys back off the repository — **and that it takes nothing else off them**:
the second test runs an ordinary `git diff` in an ordinary repository, because a pin that
disarms a key by breaking the command is not a fix, and the previous spelling of these
pins (`diff.external=`, an empty *program* rather than an absence) did exactly that in
every repository on the machine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from services.sandbox import git_config_pins, run_on_host
from services.sandbox.process import filtered_env

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _poisoned_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A git repository whose `core.fsmonitor` runs a script that leaves a marker."""
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "marker"
    hook = tmp_path / "hook.sh"
    _ = hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    hook.chmod(0o755)
    _ = subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    _ = subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=repo, check=True)
    _ = (repo / "a.txt").write_text("hello\n")
    return repo, marker


async def _status(repo: Path, env: dict[str, str] | None):
    return await run_on_host("git status --porcelain", cwd=repo, env=env, timeout_s=30)


async def test_a_repository_cannot_run_its_own_hook_on_a_status(tmp_path: Path):
    repo, marker = _poisoned_repo(tmp_path)

    # The precondition, asserted rather than assumed: on a git that does not run the hook
    # here there is nothing for the pins to prevent, and a green test would say nothing.
    _ = await _status(repo, dict(os.environ))
    if not marker.exists():
        pytest.skip("this git does not consult core.fsmonitor for a status here")

    marker.unlink()
    result = await _status(repo, git_config_pins(os.environ))
    assert not marker.exists()
    # ...and the command still worked: pinning the key must not break the observation.
    assert result.exit_code == 0


async def test_an_ordinary_diff_still_works_under_the_pins(tmp_path: Path):
    """The other half of the claim: the pins take a key off the repository and nothing
    else. `git diff` is the most common command an agent's turn runs, and the pinned
    environment has to leave it exactly as it found it — in a repository with no poison in
    it at all, which is where a pin that disarms by breaking would go unnoticed longest.
    """
    repo = tmp_path / "clean"
    repo.mkdir()
    _ = subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    _ = (repo / "a.txt").write_text("one\n")
    _ = subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    _ = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    _ = (repo / "a.txt").write_text("one\ntwo\n")

    result = await run_on_host("git diff", cwd=repo, env=git_config_pins(os.environ), timeout_s=30)
    assert result.exit_code == 0, result.stderr
    assert "+two" in result.stdout


class TestThePins:
    def test_both_are_set(self):
        env = git_config_pins({})
        assert env["GIT_CONFIG_COUNT"] == "2"
        pinned = {env[f"GIT_CONFIG_KEY_{n}"]: env[f"GIT_CONFIG_VALUE_{n}"] for n in range(2)}
        assert pinned == {"core.fsmonitor": "false", "core.pager": "cat"}

    def test_no_pin_is_a_program_named_nothing(self):
        # The rule the pins are chosen by: a key belongs here only if it has a *value*
        # meaning "do not run anything". An empty command line is a program, not an
        # absence, so a key that only accepts a command line cannot be pinned at all.
        env = git_config_pins({})
        values = [env[f"GIT_CONFIG_VALUE_{n}"] for n in range(int(env["GIT_CONFIG_COUNT"]))]
        assert all(value for value in values)

    def test_an_existing_count_is_continued_and_never_overwritten(self):
        # The operator's own environment may already be passing settings this way, and
        # renumbering from zero would silently drop theirs — git reads exactly `count`
        # pairs from index 0.
        env = git_config_pins(
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "x"}
        )
        assert env["GIT_CONFIG_COUNT"] == "3"
        assert env["GIT_CONFIG_KEY_0"] == "user.name"
        assert env["GIT_CONFIG_KEY_1"] == "core.fsmonitor"

    def test_an_unreadable_count_does_not_cost_the_pins(self):
        env = git_config_pins({"GIT_CONFIG_COUNT": "not a number"})
        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_KEY_0"] == "core.fsmonitor"

    def test_the_base_environment_is_carried_through_untouched(self):
        env = git_config_pins({"PATH": "/usr/bin", "HOME": "/home/x"})
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/x"


def test_the_spawn_environment_keeps_the_pins_and_still_drops_the_keys(
    monkeypatch: pytest.MonkeyPatch,
):
    """The two controls compose, and they compose in *one* function on purpose.

    Both answer the same question — what a spawned command reads out of its environment
    for free — and while they lived apart, a caller that built its own environment to get
    the pins silently lost the key filtering. That is not hypothetical: it is exactly what
    the host escape hatch did. `filtered_env` is now the single answer for every command
    this process spawns on the host, so there is no base for a caller to supply and no way
    to get one control without the other.
    """
    for name in [key for key in os.environ if key.startswith("GIT_CONFIG_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    env = filtered_env()
    assert env["GIT_CONFIG_KEY_0"] == "core.fsmonitor"
    assert env["GIT_CONFIG_KEY_1"] == "core.pager"
    assert "ANTHROPIC_API_KEY" not in env
