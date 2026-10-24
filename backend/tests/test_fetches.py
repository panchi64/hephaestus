"""`services/permissions/fetches.py`: which commands install packages or clone, read off
the commands the grammar walk produced."""

from __future__ import annotations

import pytest

from services.permissions.fetches import fetches
from services.permissions.shell_ast import shell_reach


def _fetches(command: str) -> bool:
    return fetches(shell_reach(command, root=None).commands)


@pytest.mark.parametrize(
    "command",
    [
        "pip install requests",
        "pip3 install -q requests",
        "pip3.12 download numpy",
        "python3 -m pip install requests",
        "/usr/bin/pip wheel .",
        "uv add httpx",
        "uv sync",
        "uv --directory app sync",
        "uv pip install -r requirements.txt",
        "uv tool install ruff",
        "npm install",
        "npm --prefix web ci",
        "pnpm add zod",
        "yarn",
        "bun install",
        "cargo fetch",
        "go mod download",
        "gem install rails",
        "bundle install",
        "composer require monolog/monolog",
        "apt-get install -y jq",
        "git clone repo",
        "git -C src fetch origin",
        "git pull --rebase",
        "git submodule update --init",
        "sudo -u root pip install requests",
        "env PIP_NO_CACHE_DIR=1 pip install requests",
        "env -u PIP_INDEX_URL pip install requests",
        "cd app && npm ci && npm test",
        "go mod tidy",
        "poetry install",
        "pipenv sync",
        "conda create -n env python",
        "npx prettier --check .",
        "pnpm dlx create-vite",
        "git ls-remote origin",
    ],
)
def test_an_install_or_clone_is_read_as_fetching(command: str):
    assert _fetches(command)


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest",
        "git status",
        "git -C install log",
        "git push origin main",
        "pip list",
        "python3 -m pytest",
        "npm test",
        "yarn test",
        "cargo build",
        "echo pip install",
        "ls install",
        "uv sync --offline",
        "pip install --no-index --find-links wheels/ requests",
        "npm ci --offline",
    ],
)
def test_other_commands_are_not(command: str):
    assert not _fetches(command)
