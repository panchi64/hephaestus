"""Persistence for plugins: drafts, the published version, history and secrets.

Every write goes through :func:`~services.plugins.manifest.validate_files`, so a stored
plugin always has a manifest that parses and names files it contains. A draft never
runs except when tested; publishing is the operator's act, and it is what makes a
plugin's tools reachable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import Engine, delete
from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import NotFoundError
from core.vault import Vault
from models.plugin import Plugin, PluginSecret
from services.revisions import Bundle, RevisionStore, decode_bundle, encode_bundle, records

from .manifest import (
    PluginManifest,
    PluginValidationError,
    secret_domain,
    validate_files,
    validate_name,
)

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


@dataclass(frozen=True)
class SecretStatus:
    name: str
    domain: str
    is_set: bool


@dataclass(frozen=True)
class SecretValue:
    value: str
    domain: str


class PluginStore:
    revision_kind = "plugin"

    def __init__(
        self, engine: Engine, vault: Vault, revisions: RevisionStore | None = None
    ) -> None:
        self._engine = engine
        self._vault = vault
        self.revisions = revisions

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
                    files.pop(path, None)
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
                validate_files(self._open(row.draft_enc))
                row.active_enc, row.draft_enc = row.draft_enc, None
            if row.active_enc is None:
                raise PluginValidationError("draft", "there is nothing to publish")
            row.published = True
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.flush()
            return self._view(row)

        return await in_session(self._engine, work)

    async def unpublish(self, owner_id: str, plugin_id: str) -> PluginView:
        """Switch the plugin's tools off, keeping its versions."""

        def work(session: Session) -> PluginView:
            row = _require(session, owner_id, plugin_id)
            row.published = False
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.flush()
            return self._view(row)

        return await in_session(self._engine, work)

    async def delete(self, owner_id: str, plugin_id: str) -> None:
        def work(session: Session) -> None:
            _delete(session, _require(session, owner_id, plugin_id))

        await in_session(self._engine, work)
        await self._forget(owner_id, plugin_id)

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

        return await in_session(self._engine, work)

    # --- secrets ----------------------------------------------------------------

    async def secrets(self, owner_id: str, plugin_id: str) -> list[SecretStatus]:
        """The secrets the current version declares, each with the host its value goes
        to and whether the operator has set it."""
        view = await self.get(owner_id, plugin_id)
        stored = await self._stored_secrets(owner_id, plugin_id)
        return [
            SecretStatus(name, stored[name].domain if name in stored else domain, name in stored)
            for name, domain in sorted(view.current.secrets.items())
        ]

    async def secret_values(self, owner_id: str, plugin_id: str) -> dict[str, SecretValue]:
        return await self._stored_secrets(owner_id, plugin_id)

    async def set_secret(
        self, owner_id: str, plugin_id: str, name: str, value: str, domain: str | None = None
    ) -> None:
        """Store a secret's value, bound to ``domain`` (by default the host the manifest
        names for it). Only the operator calls this."""
        view = await self.get(owner_id, plugin_id)
        declared = view.current.secrets
        if name not in declared:
            raise PluginValidationError("secrets", f"{view.name} declares no secret {name!r}")
        host = declared[name] if domain is None else secret_domain(domain)
        if not value:
            raise PluginValidationError("secrets", "a secret needs a value")

        def work(session: Session) -> None:
            row = session.exec(
                select(PluginSecret)
                .where(PluginSecret.plugin_id == plugin_id)
                .where(PluginSecret.name == name)
            ).first()
            if row is None:
                row = PluginSecret(owner_id=owner_id, plugin_id=plugin_id, name=name)
            row.domain = host
            row.value_enc = self._vault.encrypt_str(value)
            row.updated_at = datetime.now(UTC)
            session.add(row)

        await in_session(self._engine, work)

    async def delete_secret(self, owner_id: str, plugin_id: str, name: str) -> None:
        await self.get(owner_id, plugin_id)

        def work(session: Session) -> None:
            session.exec(
                delete(PluginSecret)
                .where(col(PluginSecret.plugin_id) == plugin_id)
                .where(col(PluginSecret.name) == name)
            )

        await in_session(self._engine, work)

    async def _stored_secrets(self, owner_id: str, plugin_id: str) -> dict[str, SecretValue]:
        def work(session: Session) -> dict[str, SecretValue]:
            rows = session.exec(
                select(PluginSecret)
                .where(PluginSecret.owner_id == owner_id)
                .where(PluginSecret.plugin_id == plugin_id)
            ).all()
            return {
                row.name: SecretValue(self._vault.decrypt_str(row.value_enc), row.domain)
                for row in rows
            }

        return await in_session(self._engine, work)

    # --- helpers ----------------------------------------------------------------

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
    session.exec(delete(PluginSecret).where(col(PluginSecret.plugin_id) == row.id))
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
