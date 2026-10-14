"""Writing the files a future session will actually read.

``.claude/`` is gitignored, so a hand-written launch config or skill dies with its clone
or worktree. ``docs/dev-instance.md`` is committed and is the source of truth; these are
materialized from it on every ``up`` and rewritten each time, since they carry this
instance's ports and a stale copy would aim a session at another worktree's instance.
"""

from __future__ import annotations

import json
from pathlib import Path

from devkit import repo
from devkit.instance import DevInstance

#: The committed recipe these are generated from.
SOURCE = repo.ROOT / "docs" / "dev-instance.md"

#: The name of the generated Claude Code launch configuration, and so the name a session
#: passes to `preview_start`. Fixed rather than per-slot: a session reads it from the
#: skill, and one name that always means "this worktree's instance" is what makes that
#: instruction correct in every worktree.
LAUNCH_CONFIG = "hephaestus-dev"

#: What the skill is called, and so what a session finds when it looks for a way to run
#: the app. The built-in `run` skill checks for a project skill covering app launch first.
SKILL_NAME = "dev-instance"

_SKILL_FRONTMATTER = """---
name: {skill}
description: >-
  Run this project's app to see a change working — an isolated dev instance with its own
  data, ports and a scripted model, separate from the operator's workspace. Use when
  asked to run, start, launch or preview the app, to screenshot or look at a screen, to
  verify a change visually or check it in the real product rather than only in tests, or
  to reproduce something end to end. Also covers seeding it, scripting the model's
  replies, injecting failures, and resetting it.
---

"""


def _substitute(text: str, instance: DevInstance, config_name: str) -> str:
    """Fill in this instance's own numbers, so the copy a session reads is about the
    instance it will actually get. The recipe still says to confirm them with
    ``status --json``, because a *second* worktree's copy will say something else."""
    for token, value in {
        "{{frontend_url}}": instance.frontend_url,
        "{{backend_url}}": instance.backend_url,
        "{{stub_port}}": str(instance.stub_port),
        "{{launch_config}}": config_name,
        "{{name}}": instance.name,
        "{{root}}": str(instance.root),
    }.items():
        text = text.replace(token, value)
    return text.replace("<worktree>", instance.name)


def _recipe() -> str:
    """The committed recipe, or a pointer to it if it has been moved or deleted.

    Degrading rather than failing: a missing document is not a reason for ``up`` to
    refuse to start an instance, and a session told where to look is better served than
    one told nothing because a file was renamed.
    """
    try:
        return SOURCE.read_text()
    except OSError:
        return (
            f"# The dev instance\n\n`{SOURCE}` is missing — it is the source this file is\n"
            "generated from. Run `uv run python dev_instance.py status --json` for the\n"
            "instance's ports, and `--help` for what the command can do.\n"
        )


def launch_entry(instance: DevInstance, config_name: str) -> dict[str, object]:
    """The Claude Code launch configuration that opens this instance in the browser pane.

    It carries no command: ``up`` owns starting the services, and a second launcher racing
    it would fight over ports. An entry with only a ``url`` attaches to what is already
    running, which is the relationship wanted here.
    """
    return {"name": config_name, "url": instance.frontend_url, "port": instance.frontend_port}


def write_launch_json(instance: DevInstance, config_name: str, root: Path) -> Path:
    """Write ``.claude/launch.json``, preserving any configurations we do not own."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "launch.json"

    configurations: list[object] = [launch_entry(instance, config_name)]
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
        except OSError, ValueError:
            existing = {}
        # Anything the operator added stays. Only the entry carrying our name is ours to
        # rewrite, and it has to be rewritten because it holds a port that can move.
        configurations += [
            entry
            for entry in existing.get("configurations", [])
            if isinstance(entry, dict) and entry.get("name") != config_name
        ]
    path.write_text(
        json.dumps({"version": "0.0.1", "configurations": configurations}, indent=2) + "\n"
    )
    return path


def write_skill(instance: DevInstance, config_name: str, root: Path) -> Path:
    """Write the project skill — the full recipe, where a session looking for one finds it."""
    directory = root / "skills" / SKILL_NAME
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    body = _substitute(_recipe(), instance, config_name)
    path.write_text(_SKILL_FRONTMATTER.format(skill=SKILL_NAME) + body)
    return path


def write_all(instance: DevInstance, root: Path | None = None) -> list[Path]:
    """Materialize every generated surface. Returns what was written, for reporting."""
    claude_dir = root or repo.CLAUDE_DIR
    return [
        write_launch_json(instance, LAUNCH_CONFIG, claude_dir),
        write_skill(instance, LAUNCH_CONFIG, claude_dir),
    ]
