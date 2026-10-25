"""Persistence for plugins: drafts, the published version and history.

Every write goes through :func:`~services.plugins.manifest.validate_files`, so a stored
plugin always has a manifest that parses and names files it contains. A draft never
runs except when tested; publishing is the operator's act, and it is what makes a
plugin's tools reachable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import Engine, delete
from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import NotFoundError, PluginValidationError
from core.vault import Vault
from models.plugin import Plugin, PluginSecret, PluginStorageEntry
from services.agents.definition import AgentDefinition
from services.revisions import Bundle, RevisionStore, decode_bundle, encode_bundle, records

from .contributions.agents import AGENTS
from .contributions.base import TypedContribution
from .contributions.skills import SKILLS, PluginSkill
from .manifest import PluginManifest, validate_files, validate_name

type Source = Literal["user", "agent"]


@dataclass(frozen=True)
class PluginView:
    id: str
    name: str
    source: Source
    published: bool
    #: The published version's manifest; None until the plugin is first published.
    active: PluginManifest | None
    draft: PluginManifest | None
    created_at: datetime
    updated_at: datetime

    @property
    def current(self) -> PluginManifest:
        """The draft when there is one, else the published version."""
        manifest = self.draft or self.active
        assert manifest is not None  # a stored plugin always has one or the other
        return manifest


class PluginStore:
    revision_kind: str = "plugin"

    def __init__(
        self, engine: Engine, vault: Vault, revisions: RevisionStore | None = None
    ) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self.revisions: RevisionStore | None = revisions
        self._listeners: list[Callable[[str], None]] = []

    # --- reads ------------------------------------------------------------------

    async def list(self, owner_id: str) -> list[PluginView]:
        def work(session: Session) -> list[PluginView]:
            rows = session.exec(
                select(Plugin).where(Plugin.owner_id == owner_id).order_by(col(Plugin.name))
            ).all()
            return [self._view(row) for row in rows]

        return await in_session(self._engine, work)

    async def get(self, owner_id: str, plugin_id: str) -> PluginView:
        def work(session: Session) -> PluginView:
            return self._view(_require(session, owner_id, plugin_id))

        return await in_session(self._engine, work)

    async def get_by_name(self, owner_id: str, name: str) -> PluginView | None:
        def work(session: Session) -> PluginView | None:
            row = _find_by_name(session, owner_id, name)
            return None if row is None else self._view(row)

        return await in_session(self._engine, work)

    async def files(
        self, owner_id: str, plugin_id: str, which: Literal["draft", "active", "current"]
    ) -> Bundle:
        """A version's files; ``current`` is the draft when there is one."""

        def work(session: Session) -> Bundle:
            row = _require(session, owner_id, plugin_id)
            if which == "draft":
                sealed = row.draft_enc
            elif which == "active":
                sealed = row.active_enc
            else:
                sealed = row.draft_enc or row.active_enc
            return {} if sealed is None else self._open(sealed)

        return await in_session(self._engine, work)

    async def published(self, owner_id: str) -> list[tuple[PluginView, Bundle]]:
        """Every published plugin with the files its tools run."""

        def work(session: Session) -> list[tuple[PluginView, Bundle]]:
            rows = session.exec(
                select(Plugin)
                .where(Plugin.owner_id == owner_id)
                .where(col(Plugin.published).is_(True))
                .order_by(col(Plugin.name))
            ).all()
            return [
                (self._view(row), self._open(row.active_enc))
                for row in rows
                if row.active_enc is not None
            ]

        return await in_session(self._engine, work)

    async def contributed[T](
        self, owner_id: str, kind: TypedContribution[T]
    ) -> list[tuple[str, T]]:
        """One kind of contribution across every published plugin, with each one's plugin."""
        return [
            (view.name, item)
            for view, _files in await self.published(owner_id)
            if view.active is not None
            for item in kind.of(view.active.contributions)
        ]

    async def agents(self, owner_id: str) -> list[AgentDefinition]:
        return [agent for _plugin, agent in await self.contributed(owner_id, AGENTS)]

    async def skills(self, owner_id: str) -> list[PluginSkill]:
        return [skill for _plugin, skill in await self.contributed(owner_id, SKILLS)]

    # --- writes -----------------------------------------------------------------

    async def write_draft(
        self,
        owner_id: str,
        name: str,
        changes: Mapping[str, bytes | None],
        *,
        source: Source,
    ) -> PluginView:
        """Apply ``changes`` (a path's new bytes, or None to remove it) to the plugin's
        draft, starting one from the published version if there is none. A new name
        creates the plugin."""
        clean = validate_name(name)

        def work(session: Session) -> PluginView:
            row = _find_by_name(session, owner_id, clean)
            if row is None:
                row = Plugin(owner_id=owner_id, name=clean, source=source)
                base: Bundle = {}
            else:
                sealed = row.draft_enc or row.active_enc
                base = {} if sealed is None else self._open(sealed)
            files = dict(base)
            for path, blob in changes.items():
                if blob is None:
                    _ = files.pop(path, None)
                else:
                    files[path] = blob
            manifest = validate_files(files)
            if manifest.name != clean:
                raise PluginValidationError(
                    "name", f"plugin.yaml names {manifest.name!r}, but this plugin is {clean!r}"
                )
            row.draft_enc = self._seal(files)
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.flush()
            return self._view(row)

        return await in_session(self._engine, work)

    async def import_files(self, owner_id: str, files: Bundle) -> PluginView:
        """A new plugin from a whole bundle, as the operator's draft."""
        manifest = validate_files(files)

        def work(session: Session) -> PluginView:
            if _find_by_name(session, owner_id, manifest.name) is not None:
                raise PluginValidationError(
                    "name", f"a plugin named {manifest.name!r} already exists"
                )
            row = Plugin(owner_id=owner_id, name=manifest.name, draft_enc=self._seal(files))
            session.add(row)
            session.flush()
            return self._view(row)

        return await in_session(self._engine, work)

    async def discard_draft(self, owner_id: str, plugin_id: str) -> PluginView | None:
        """Drop the draft; a plugin that was never published goes with it."""

        def work(session: Session) -> PluginView | None:
            row = _require(session, owner_id, plugin_id)
            if row.active_enc is None:
                _delete(session, row)
                return None
            row.draft_enc = None
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.flush()
            return self._view(row)

        view = await in_session(self._engine, work)
        if view is None:
            await self._forget(owner_id, plugin_id)
        return view

    @records("published")
    async def publish(self, owner_id: str, plugin_id: str) -> PluginView:
        """Make the draft the published version, or switch a disabled plugin back on."""

        def work(session: Session) -> PluginView:
            row = _require(session, owner_id, plugin_id)
            if row.draft_enc is not None:
                _ = validate_files(self._open(row.draft_enc))
                row.active_enc, row.draft_enc = row.draft_enc, None
            if row.active_enc is None:
                raise PluginValidationError("draft", "there is nothing to publish")
            row.published = True
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.flush()
            return self._view(row)

        return self._changed(await in_session(self._engine, work))

    async def publish_if_inert(self, owner_id: str, plugin_id: str) -> PluginView:
        """Publish the draft when it can only show the operator something; anything that
        runs code or reaches out still waits for them."""
        view = await self.get(owner_id, plugin_id)
        if view.draft is None or not view.draft.inert:
            return view
        return await self.publish(owner_id, plugin_id)

    async def unpublish(self, owner_id: str, plugin_id: str) -> PluginView:
        """Switch the plugin's tools off, keeping its versions."""

        def work(session: Session) -> PluginView:
            row = _require(session, owner_id, plugin_id)
            row.published = False
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.flush()
            return self._view(row)

        return self._changed(await in_session(self._engine, work))

    async def delete(self, owner_id: str, plugin_id: str) -> None:
        def work(session: Session) -> None:
            _delete(session, _require(session, owner_id, plugin_id))

        await in_session(self._engine, work)
        await self._forget(owner_id, plugin_id)
        self._notify(plugin_id)

    def on_change(self, listener: Callable[[str], None]) -> None:
        """Call ``listener`` with a plugin's id whenever what it runs changes or stops."""
        self._listeners.append(listener)

    # --- history ----------------------------------------------------------------

    async def snapshot(self, owner_id: str, plugin_id: str) -> Bundle:
        return await self.files(owner_id, plugin_id, "active")

    async def working_copy(self, owner_id: str, plugin_id: str) -> Bundle:
        return await self.files(owner_id, plugin_id, "current")

    async def restore_snapshot(self, owner_id: str, plugin_id: str, bundle: Bundle) -> PluginView:
        """Publish ``bundle`` as the plugin's version, leaving any draft as it is."""
        manifest = validate_files(bundle)

        def work(session: Session) -> PluginView:
            row = _require(session, owner_id, plugin_id)
            if manifest.name != row.name:
                raise PluginValidationError("name", f"that version is named {manifest.name!r}")
            row.active_enc = self._seal(bundle)
            row.published = True
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.flush()
            return self._view(row)

        return self._changed(await in_session(self._engine, work))

    # --- helpers ----------------------------------------------------------------

    def _notify(self, plugin_id: str) -> None:
        for listener in self._listeners:
            listener(plugin_id)

    def _changed(self, view: PluginView) -> PluginView:
        self._notify(view.id)
        return view

    async def _forget(self, owner_id: str, plugin_id: str) -> None:
        if self.revisions is not None:
            await self.revisions.forget(owner_id, self.revision_kind, plugin_id)

    def _seal(self, files: Bundle) -> bytes:
        return self._vault.encrypt_bytes(encode_bundle(files))

    def _open(self, sealed: bytes) -> Bundle:
        return decode_bundle(self._vault.decrypt_bytes(sealed))

    def _view(self, row: Plugin) -> PluginView:
        return PluginView(
            id=row.id,
            name=row.name,
            source="agent" if row.source == "agent" else "user",
            published=row.published,
            active=None if row.active_enc is None else validate_files(self._open(row.active_enc)),
            draft=None if row.draft_enc is None else validate_files(self._open(row.draft_enc)),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def _delete(session: Session, row: Plugin) -> None:
    _ = session.exec(delete(PluginSecret).where(col(PluginSecret.plugin_id) == row.id))
    _ = session.exec(delete(PluginStorageEntry).where(col(PluginStorageEntry.plugin_id) == row.id))
    session.delete(row)


def _require(session: Session, owner_id: str, plugin_id: str) -> Plugin:
    row = session.get(Plugin, plugin_id)
    if row is None or row.owner_id != owner_id:
        raise NotFoundError("plugin not found")
    return row


def _find_by_name(session: Session, owner_id: str, name: str) -> Plugin | None:
    return session.exec(
        select(Plugin).where(Plugin.owner_id == owner_id).where(Plugin.name == name)
    ).first()
