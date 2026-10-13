"""Checks that answer "why is this not working", each carrying its own fix.

The failure mode worth designing against is not a session that gets no information — it
is one that gets a true statement and still does the wrong thing next. "Port 8200 is in
use" is true and useless; "port 8200 is held by something that is not this instance's
backend — stop it, or delete instance.json to be given another slot" is the same fact with
the next move attached.

So every check returns a remedy alongside its verdict, and the remedy is a sentence
someone can act on without reading this package.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from devkit import launch, live, ports, repo, surfaces
from devkit.instance import DevInstance
from devkit.processes import listening

#: A check that passed needs no remedy; one that failed is useless without one.
OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Finding:
    name: str
    status: str
    detail: str
    remedy: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "check": self.name,
            "status": self.status,
            "detail": self.detail,
            "remedy": self.remedy,
        }


def _tooling() -> list[Finding]:
    findings = []
    for tool, why in (("bun", "the frontend dev server"), ("uv", "the backend")):
        # Resolved once: two scans would be wasteful, and could in principle disagree —
        # reporting `ok` alongside an empty path.
        found = shutil.which(tool)
        if found is None:
            findings.append(
                Finding(
                    tool,
                    FAIL,
                    f"not on PATH — {why} cannot start",
                    f"install {tool}, or run only the half that does not need it",
                )
            )
        else:
            findings.append(Finding(tool, OK, found))
    if not (repo.FRONTEND / "node_modules").is_dir():
        findings.append(
            Finding(
                "frontend deps",
                WARN,
                "node_modules is missing",
                "`up` installs them on its next run; nothing to do by hand",
            )
        )
    return findings


def _ports(instance: DevInstance) -> list[Finding]:
    findings = []
    healthy = launch.backend_healthy(instance)
    for name, port in (
        ("backend", instance.backend_port),
        ("frontend", instance.frontend_port),
        ("stub", instance.stub_port),
    ):
        if not listening(port):
            findings.append(Finding(f"{name} port", OK, f"{port} free"))
            continue
        # A held backend port that does not answer /health is the one genuinely
        # ambiguous case: it looks running and is not.
        if name == "backend" and not healthy:
            findings.append(
                Finding(
                    "backend port",
                    FAIL,
                    f"{port} is held by something that does not answer /health",
                    "stop it, or delete "
                    f"{instance.root / 'instance.json'} to be given another slot",
                )
            )
        else:
            findings.append(Finding(f"{name} port", OK, f"{port} in use by this instance"))
    return findings


def _isolation(instance: DevInstance) -> list[Finding]:
    """The checks that matter most, because their failure is silent and expensive."""
    findings = []
    env = instance.env()
    if not str(instance.data_dir).startswith(str(Path.home())):
        findings.append(
            Finding(
                "data dir",
                WARN,
                f"{instance.data_dir} is outside your home directory",
                "expected under ~/.odysseus/dev — check instance.json",
            )
        )
    else:
        findings.append(Finding("data dir", OK, str(instance.data_dir)))

    if env["ODYSSEUS_PORT"] in {"8000", "5173"}:
        findings.append(
            Finding(
                "ports",
                FAIL,
                "this instance is on the operator's own ports",
                "no slot can reach them — devkit/ports.py has been changed",
            )
        )
    return findings


def _model(_instance: DevInstance) -> list[Finding]:
    if live.configured():
        return [
            Finding(
                "model",
                OK,
                f"a real endpoint ({live.endpoint()['base_url']}) — the stub is not started",
            )
        ]
    missing = live.missing()
    if len(missing) < len(live.REQUIRED):
        return [
            Finding(
                "model",
                WARN,
                f"a real endpoint is half-configured; missing {', '.join(missing)}",
                "set them all to use it, or unset the rest to fall back to the stub",
            )
        ]
    return [Finding("model", OK, "the scripted stub")]


def _surfaces() -> list[Finding]:
    findings = []
    if not surfaces.SOURCE.is_file():
        findings.append(
            Finding(
                "recipe",
                WARN,
                f"{surfaces.SOURCE} is missing",
                "the generated skill falls back to a pointer; restore the file",
            )
        )
    for label, path in (
        ("launch.json", repo.CLAUDE_DIR / "launch.json"),
        ("skill", repo.CLAUDE_DIR / "skills" / surfaces.SKILL_NAME / "SKILL.md"),
    ):
        if path.is_file():
            findings.append(Finding(label, OK, str(path)))
        else:
            findings.append(
                Finding(
                    label,
                    WARN,
                    f"{path} has not been generated",
                    "run `up` — it writes these on every run",
                )
            )
    return findings


def run(instance: DevInstance) -> list[Finding]:
    """Every check, in the order a reader would want them."""
    findings = [
        *_tooling(),
        *_ports(instance),
        *_isolation(instance),
        *_model(instance),
        *_surfaces(),
    ]
    if len(set(ports.slot_ports(instance.slot))) != 3:
        findings.append(
            Finding(
                "slot",
                FAIL,
                "this slot's three ports are not distinct",
                "the port bases in devkit/ports.py have drifted into each other",
            )
        )
    return findings
