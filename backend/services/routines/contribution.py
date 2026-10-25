"""``routines``: routine templates from ``routines/<name>.md`` for the operator to set up,
with ``title``, ``schedule`` (the compact form `routines_write` takes) and an optional
``agent`` in the frontmatter, and the prompt as the body.

Registered by the routines feature rather than core: a contribution type belongs to the
feature that consumes it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import override

from core.exceptions import PluginValidationError, RoutineValidationError
from core.frontmatter import FrontmatterError, split_frontmatter
from services.plugins.contributions.base import BundleSource, Described, TypedContribution
from services.plugins.contributions.registry import CONTRIBUTIONS
from services.plugins.fields import in_folder, validate_name

from .schedule import parse_schedule


@dataclass(frozen=True)
class RoutineTemplate:
    name: str
    title: str
    schedule: str
    agent: str | None
    prompt: str


class Routines(TypedContribution[RoutineTemplate]):
    @override
    def parse(self, source: BundleSource) -> tuple[RoutineTemplate, ...]:
        if source.declared:
            raise PluginValidationError(self.key, "routines come from routines/<name>.md files")
        templates: list[RoutineTemplate] = []
        for path, stem, blob in in_folder(source.files or {}, "routines", ".md"):
            try:
                fields, body = split_frontmatter(blob.decode())
                template = RoutineTemplate(
                    name=validate_name(stem),
                    title=_required(fields, "title"),
                    schedule=_required(fields, "schedule"),
                    agent=str(fields["agent"]) if fields.get("agent") else None,
                    prompt=body.strip(),
                )
                if not template.prompt:
                    raise PluginValidationError("routines", "the body is the routine's prompt")
                _ = parse_schedule(template.schedule, "UTC")
            except (FrontmatterError, RoutineValidationError, UnicodeDecodeError) as exc:
                raise PluginValidationError("routines", f"{path}: {exc}") from None
            except PluginValidationError as exc:
                raise PluginValidationError("routines", f"{path}: {exc}") from None
            templates.append(template)
        return tuple(templates)

    @override
    def describe_one(self, item: RoutineTemplate) -> Described:
        return {"name": item.name, "title": item.title, "schedule": item.schedule}


def _required(fields: Mapping[str, object], key: str) -> str:
    value = str(fields.get(key) or "").strip()
    if not value:
        raise PluginValidationError("routines", f"needs a {key}")
    return value


ROUTINES = CONTRIBUTIONS.register(Routines("routines", "Routine templates", RoutineTemplate))
