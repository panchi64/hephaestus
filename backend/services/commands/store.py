"""``CommandStore`` — persistence for the workflows the operator writes.

Small on purpose. A workflow is a name, four pieces of text and a switch; there is no
bundle, no import format, no second reader. What this owns beyond plain CRUD is the one
thing a store should: **sealing happens here, never in the columns** — the title, the
description, the hint and the body go through the vault on the way in and out, and only the
name and the flag stay clear, for the reasons ``models/command.py`` gives.

The registry reads it through :meth:`specs`, which is deliberately narrower than
:meth:`list_commands`: the picker wants enabled rows as :class:`CommandSpec`, and handing it
the editor's view would mean the catalog and the settings pane disagreeing the first time
one of them grew a field.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlmodel import Session, select

from core.db import in_session
from core.exceptions import CommandValidationError, NotFoundError
from core.vault import Vault
from models.command import Command

from .authoring import (
    ARGUMENT_HINT_MAX_CHARS,
    DESCRIPTION_MAX_CHARS,
    TITLE_MAX_CHARS,
    validate_body,
    validate_line,
    validate_name,
)
from .spec import CommandSpec


@dataclass(frozen=True)
class CommandView:
    """One workflow as its author sees it — every field, opened."""

    id: str
    name: str
    title: str
    description: str
    body: str
    argument_hint: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class CommandStore:
    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine = engine
        self._vault = vault

    # ── writes ───────────────────────────────────────────────────────────────────────────

    async def create(
        self,
        owner_id: str,
        *,
        name: str,
        body: str,
        title: str = "",
        description: str = "",
        argument_hint: str | None = None,
        enabled: bool = True,
    ) -> CommandView:
        """Write a workflow. Enabled by default — unlike a skill, there is no trust
        boundary to cross first: nothing but the operator's own picker will ever see it."""
        fields = self._clean(
            name=name,
            body=body,
            title=title,
            description=description,
            argument_hint=argument_hint,
        )

        def work(session: Session) -> CommandView:
            if _find_by_name(session, owner_id, fields["name"]) is not None:
                raise CommandValidationError(
                    "name", f"a command named /{fields['name']} already exists"
                )
            command = Command(
                owner_id=owner_id,
                name=fields["name"],
                title_enc=self._vault.encrypt_str(fields["title"]),
                description_enc=self._vault.encrypt_str(fields["description"]),
                body_enc=self._vault.encrypt_str(fields["body"]),
                argument_hint_enc=self._seal(fields["argument_hint"]),
                enabled=enabled,
            )
            session.add(command)
            session.flush()
            return self._view(command)

        return await in_session(self._engine, work)

    async def update(
        self,
        owner_id: str,
        command_id: str,
        *,
        name: str | None = None,
        body: str | None = None,
        title: str | None = None,
        description: str | None = None,
        argument_hint: str | None = None,
        enabled: bool | None = None,
    ) -> CommandView:
        """Change a workflow. Every supplied field is re-validated, so a command edited
        through the API is exactly as well-formed as one created through it; an omitted
        field is left alone, so the pane can save one box without resending the rest."""
        clean_name = validate_name(name) if name is not None else None
        clean_body = validate_body(body) if body is not None else None
        clean_title = (
            validate_line(title, field="title", limit=TITLE_MAX_CHARS)
            if title is not None
            else None
        )
        clean_description = (
            validate_line(description, field="description", limit=DESCRIPTION_MAX_CHARS)
            if description is not None
            else None
        )
        clean_hint = (
            validate_line(argument_hint, field="argument hint", limit=ARGUMENT_HINT_MAX_CHARS)
            if argument_hint is not None
            else None
        )

        def work(session: Session) -> CommandView:
            command = _require(session, owner_id, command_id)
            if clean_name is not None and clean_name != command.name:
                if _find_by_name(session, owner_id, clean_name) is not None:
                    raise CommandValidationError(
                        "name", f"a command named /{clean_name} already exists"
                    )
                command.name = clean_name
            if clean_title is not None:
                command.title_enc = self._vault.encrypt_str(clean_title or command.name)
            if clean_description is not None:
                command.description_enc = self._vault.encrypt_str(clean_description)
            if clean_body is not None:
                command.body_enc = self._vault.encrypt_str(clean_body)
            if clean_hint is not None:
                # Empty clears it: the hint is the one field whose absence is meaningful —
                # it says this command takes nothing after its name.
                command.argument_hint_enc = self._seal(clean_hint or None)
            if enabled is not None:
                command.enabled = enabled
            command.updated_at = datetime.now(UTC)
            session.add(command)
            session.flush()
            return self._view(command)

        return await in_session(self._engine, work)

    async def delete(self, owner_id: str, command_id: str) -> None:
        """Remove it. No soft-archive — a command the operator no longer wants typed is
        one row, and ``enabled`` is already there for the case where they might."""

        def work(session: Session) -> None:
            session.delete(_require(session, owner_id, command_id))

        return await in_session(self._engine, work)

    # ── reads ────────────────────────────────────────────────────────────────────────────

    async def get(self, owner_id: str, command_id: str) -> CommandView:
        def work(session: Session) -> CommandView:
            return self._view(_require(session, owner_id, command_id))

        return await in_session(self._engine, work)

    async def list_commands(self, owner_id: str) -> list[CommandView]:
        """Every workflow, enabled or not — the settings pane's view. Newest edit first,
        which is where someone who has just saved one looks for it."""

        def work(session: Session) -> list[CommandView]:
            rows = session.exec(
                select(Command)
                .where(Command.owner_id == owner_id)
                .order_by(Command.updated_at.desc())
            ).all()
            return [self._view(row) for row in rows]

        return await in_session(self._engine, work)

    async def specs(self, owner_id: str) -> list[CommandSpec]:
        """The **enabled** workflows, as the registry's own vocabulary.

        Sorted by name rather than by edit time: this is a menu, and a menu that reorders
        itself because something was saved an hour ago is one the operator has to read
        instead of aim at.
        """

        def work(session: Session) -> list[CommandSpec]:
            rows = session.exec(
                select(Command)
                .where(Command.owner_id == owner_id)
                .where(Command.enabled)
                .order_by(Command.name)
            ).all()
            return [_spec(self._view(row)) for row in rows]

        return await in_session(self._engine, work)

    # ── sealing ──────────────────────────────────────────────────────────────────────────

    def _view(self, command: Command) -> CommandView:
        title = self._vault.decrypt_str(command.title_enc)
        return CommandView(
            id=command.id,
            name=command.name,
            # A row with no title of its own shows its name, which is what the operator
            # typed and therefore never a blank line in the picker.
            title=title or command.name,
            description=self._vault.decrypt_str(command.description_enc),
            body=self._vault.decrypt_str(command.body_enc),
            argument_hint=self._open(command.argument_hint_enc),
            enabled=command.enabled,
            created_at=command.created_at,
            updated_at=command.updated_at,
        )

    def _clean(self, **fields: str | None) -> dict[str, str]:
        """Every field of a new workflow, validated together so the first failure the
        operator sees is the first one their form has."""
        return {
            "name": validate_name(fields["name"] or ""),
            "body": validate_body(fields["body"] or ""),
            "title": validate_line(fields["title"] or "", field="title", limit=TITLE_MAX_CHARS),
            "description": validate_line(
                fields["description"] or "", field="description", limit=DESCRIPTION_MAX_CHARS
            ),
            "argument_hint": validate_line(
                fields["argument_hint"] or "",
                field="argument hint",
                limit=ARGUMENT_HINT_MAX_CHARS,
            ),
        }

    def _seal(self, value: str | None) -> str | None:
        return None if value is None else self._vault.encrypt_str(value)

    def _open(self, enc: str | None) -> str | None:
        return None if enc is None else self._vault.decrypt_str(enc)


# ── module helpers ───────────────────────────────────────────────────────────────────────


def _spec(view: CommandView) -> CommandSpec:
    return CommandSpec(
        name=view.name,
        source="workflow",
        kind="prompt",
        title=view.title,
        description=view.description,
        argument_hint=view.argument_hint,
        body=view.body,
    )


def _require(session: Session, owner_id: str, command_id: str) -> Command:
    command = session.get(Command, command_id)
    if command is None or command.owner_id != owner_id:
        raise NotFoundError("command not found")
    return command


def _find_by_name(session: Session, owner_id: str, name: str) -> Command | None:
    return session.exec(
        select(Command).where(Command.owner_id == owner_id).where(Command.name == name)
    ).first()
