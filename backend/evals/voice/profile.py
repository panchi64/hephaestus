"""A made-up operator's workspace, applied before a run and removed after it, so the voice
is measured with the context a lived-in workspace puts in front of Talos.

Everything goes through the instance's HTTP API and is undone the same way; the profile
never touches anything it did not create, except the plugin switches it restores.
"""

from __future__ import annotations

import secrets
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .spec import Memory

PROFILES_DIR = Path(__file__).with_name("profiles")


class _Strict(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class Skill(_Strict):
    name: str
    description: str
    body: str = ""
    pinned: bool = False


class Connector(_Strict):
    preset: str
    name: str
    credentials: dict[str, str] | None = None


class Project(_Strict):
    name: str
    readme: str


class Profile(_Strict):
    name: str
    #: Built-in plugins switched on for the run.
    plugins: list[str] = Field(default_factory=list)
    memories: list[Memory] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    integrations: list[Connector] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)


def available() -> list[str]:
    return sorted(path.stem for path in PROFILES_DIR.glob("*.toml"))


def load_profile(name: str) -> Profile:
    path = PROFILES_DIR / f"{name}.toml"
    if not path.is_file():
        raise ValueError(f"no profile {name!r}; available: {', '.join(available())}")
    return Profile.model_validate({"name": name, **tomllib.loads(path.read_text())})


class _Created(BaseModel):
    id: str


class _Plugin(BaseModel):
    name: str
    enabled: bool


@dataclass
class Applied:
    """What a profile created, so :func:`remove` undoes exactly that."""

    memories: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    integrations: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    folder: Path | None = None
    #: Each switched plugin's state before the run.
    plugins: dict[str, bool] = field(default_factory=dict)


async def _create(http: httpx.AsyncClient, path: str, body: dict[str, object]) -> str:
    response = await http.post(path, json=body)
    _ = response.raise_for_status()
    return _Created.model_validate_json(response.content).id


async def apply(
    http: httpx.AsyncClient, data_dir: Path, profile: Profile, applied: Applied
) -> None:
    """Create the profile's workspace, recording each row in ``applied`` as it lands, so a
    failure partway still leaves :func:`remove` everything to undo."""
    response = await http.get("/plugins/builtin")
    _ = response.raise_for_status()
    states = {p.name: p.enabled for p in map(_Plugin.model_validate, response.json())}
    for name in profile.plugins:
        applied.plugins[name] = states[name]
        _ = (await http.put(f"/plugins/builtin/{name}", json={"enabled": True})).raise_for_status()
    for memory in profile.memories:
        applied.memories.append(await _create(http, "/memory", memory.model_dump()))
    for skill in profile.skills:
        body = skill.model_dump(exclude={"pinned"})
        skill_id = await _create(http, "/skills", body)
        applied.skills.append(skill_id)
        _ = (await http.post(f"/skills/{skill_id}/publish")).raise_for_status()
        if skill.pinned:
            _ = (await http.post(f"/skills/{skill_id}/pin")).raise_for_status()
    for connector in profile.integrations:
        applied.integrations.append(
            await _create(http, "/integrations", connector.model_dump(exclude_none=True))
        )
    # A random parent keeps each project's own folder name, which becomes its name.
    applied.folder = data_dir / "voice-profile" / secrets.token_hex(3)
    for project in profile.projects:
        root = applied.folder / project.name
        root.mkdir(parents=True)
        _ = (root / "README.md").write_text(f"# {project.name}\n\n{project.readme}\n")
        applied.projects.append(await _create(http, "/projects/ensure", {"root_path": str(root)}))


async def remove(http: httpx.AsyncClient, applied: Applied) -> list[str]:
    """Undo :func:`apply` in reverse; returns what could not be undone instead of raising,
    so one failure doesn't strand the rest."""
    steps = [
        *(f"/projects/{i}" for i in reversed(applied.projects)),
        *(f"/integrations/{i}" for i in reversed(applied.integrations)),
        *(f"/skills/{i}" for i in reversed(applied.skills)),
        *(f"/memory/{i}" for i in reversed(applied.memories)),
    ]
    failed: list[str] = []
    for path in steps:
        try:
            response = await http.delete(path)
            if response.status_code not in (204, 404):
                failed.append(f"DELETE {path}: {response.status_code}")
        except httpx.HTTPError as exc:
            failed.append(f"DELETE {path}: {exc}")
    for name, enabled in applied.plugins.items():
        try:
            response = await http.put(f"/plugins/builtin/{name}", json={"enabled": enabled})
            if response.is_error:
                failed.append(f"plugin {name}: {response.status_code}")
        except httpx.HTTPError as exc:
            failed.append(f"plugin {name}: {exc}")
    if applied.folder is not None:
        shutil.rmtree(applied.folder, ignore_errors=True)
    return failed
