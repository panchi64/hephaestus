"""``MailService`` — the mail capability's facade (`EMAIL-1..5`).

Everything above this file (the routes, the agent's tools) talks to one object; everything
below it — the provider adapters, the cache, triage, style — is an implementation detail it
composes. It owns four things:

- **Accounts.** CRUD over ``MailAccount``, with the secret sealed on write via
  :class:`~services.mail.oauth.MailSecrets` and never returned on read.
- **The transport factory.** Provider kind → adapter, built with a freshly-opened secret.
  Transports are cached per account and torn down when the account changes or is removed.
- **Reads, cache-first.** A listing is answered from the local cache inside a short
  freshness window (`EMAIL-5`, `XC-PERF-4`); outside it the provider is consulted, the
  cache reconciled, and the answer served from the cache either way — so the shape of a
  response never depends on whether it was a cache hit.
- **Sync off the request path.** New mail is pulled and triaged on a lock-aware
  :class:`~core.worker.WriteBehindWorker`, the same discipline upload extraction uses: a
  locked vault **parks** the sync (secrets and message content both need the key) rather
  than failing it, and it resumes on unlock.

Domain errors only — the routes map them to HTTP, the tools decide retry-vs-degrade.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine
from sqlmodel import Session, select

from core.db import in_session
from core.exceptions import NotFoundError
from core.tasks import STOP_TIMEOUT_S, cancel_and_wait
from core.vault import Vault
from core.worker import WriteBehindWorker
from models._fields import utcnow
from models.mail import (
    AUTH_OAUTH,
    AUTH_PASSWORD,
    PROVIDER_GMAIL,
    PROVIDER_GRAPH,
    PROVIDER_IMAP,
    PROVIDER_JMAP,
    MailAccount,
)

from .cache import MailCache, MessageDetail, MessageView
from .drafts import KIND_SUGGESTED, DraftStore, DraftView, StyleProfileStore
from .errors import MailError, MailUnavailableError
from .gmail import GmailTransport
from .graph import GraphTransport
from .imap import ImapTransport
from .jmap import JmapTransport
from .models import ROLE_INBOX, ROLE_SENT, AccountSpec, MailAddress, MailFolder, OutgoingMail
from .oauth import MailSecrets, TokenBundle
from .quoting import split_body
from .style import SAMPLE_LIMIT, MailStyle
from .transport import MailTransport
from .triage import MailTriage

logger = logging.getLogger(__name__)

_TRANSPORTS = {
    PROVIDER_IMAP: ImapTransport,
    PROVIDER_JMAP: JmapTransport,
    PROVIDER_GMAIL: GmailTransport,
    PROVIDER_GRAPH: GraphTransport,
}

# How long a cached folder listing is served without consulting the provider
# (`EMAIL-5`/`XC-PERF-4`). Short enough that new mail shows up promptly, long enough that
# opening the inbox repeatedly costs nothing.
CACHE_TTL_S = 60.0
# How often the background loop pulls each enabled account.
SYNC_INTERVAL_S = 300.0


@dataclass(frozen=True, slots=True)
class AccountView:
    """An account as every read returns it — **never** carrying the secret."""

    id: str
    name: str
    address: str
    provider: str
    auth_kind: str
    enabled: bool
    status: str
    error_detail: str | None
    last_synced_at: datetime | None
    has_secret: bool


@dataclass(frozen=True, slots=True)
class LiveTransport:
    """A built adapter together with the credentials it was built with.

    One unit, so "cached" and "cached with these credentials" cannot drift apart: an
    adapter snapshots its secret into an :class:`~services.mail.models.AccountSpec` at
    construction and holds a connection authenticated with it, so a cache that remembers
    only the adapter has no way to notice that the account's token has rotated since.
    """

    transport: MailTransport
    credentials: tuple[str | None, str | None]


@dataclass(frozen=True, slots=True)
class SyncJob:
    """One account's folder, queued for a background pull. Carries ids only — the rows
    are re-read on each attempt, so a retry after a vault park sees current state."""

    owner_id: str
    account_id: str
    folder: str | None = None


class MailService:
    def __init__(
        self,
        engine: Engine,
        vault: Vault,
        credentials,
        registry,
        *,
        notifications=None,
        cache_ttl_s: float = CACHE_TTL_S,
        sync_interval_s: float = SYNC_INTERVAL_S,
    ) -> None:
        self._engine = engine
        self._vault = vault
        self._secrets = MailSecrets(engine, vault, credentials)
        self.cache = MailCache(engine, vault)
        self.drafts = DraftStore(engine, vault)
        self.profiles = StyleProfileStore(engine, vault)
        self.triage = MailTriage(registry, notifications)
        self.style = MailStyle(registry)
        self._cache_ttl_s = cache_ttl_s
        self._sync_interval_s = sync_interval_s
        self._transports: dict[str, LiveTransport] = {}
        self._transport_locks: dict[str, asyncio.Lock] = {}
        self._refreshed: dict[tuple[str, str], float] = {}
        # Sync opens sealed credentials and seals message content, so it parks while the
        # vault is locked rather than failing — the upload-extraction discipline.
        self._worker: WriteBehindWorker[SyncJob] = WriteBehindWorker(
            self._run_sync, name="mail-sync", unlocked=vault.unlocked_event
        )
        self._loop_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._worker.start()
        self._loop_task = asyncio.create_task(self._sync_loop(), name="mail-sync-loop")

    async def stop(self) -> None:
        await cancel_and_wait(self._loop_task, STOP_TIMEOUT_S)
        await self._worker.stop()
        for live in list(self._transports.values()):
            with suppress(Exception):
                await live.transport.close()
        self._transports.clear()

    # --- accounts --------------------------------------------------------------

    async def list_accounts(self, owner_id: str) -> list[AccountView]:
        return [self._to_view(row) for row in await self._rows(owner_id)]

    async def has_accounts(self, owner_id: str) -> bool:
        """Whether a mailbox has been added at all — one id, and nothing opened.

        Asked on every turn to decide whether the mail tools are worth offering, which is
        a yes/no about existence and must be answerable while the vault is locked. Every
        other read here builds an ``AccountView``, and a view decrypts the address.
        """

        def work(session: Session) -> bool:
            query = select(MailAccount.id).where(MailAccount.owner_id == owner_id).limit(1)
            return session.exec(query).first() is not None

        return await in_session(self._engine, work)

    async def get_account(self, owner_id: str, account_id: str) -> AccountView:
        return self._to_view(await self._row(owner_id, account_id))

    async def create_account(
        self,
        owner_id: str,
        *,
        name: str,
        address: str,
        provider: str = PROVIDER_IMAP,
        config: dict | None = None,
        password: str | None = None,
        tokens: TokenBundle | None = None,
    ) -> AccountView:
        """Connect a mailbox. Exactly one of ``password``/``tokens`` supplies the secret,
        which is sealed here and never read back out to a caller."""
        if provider not in _TRANSPORTS:
            raise MailError(f"unsupported mail provider: {provider!r}")
        account = MailAccount(
            owner_id=owner_id,
            name=name,
            address_enc=self._vault.encrypt_str(address),
            provider=provider,
            auth_kind=AUTH_OAUTH if tokens is not None else AUTH_PASSWORD,
            config=config or {},
            secret_enc=(
                self._secrets.seal_bundle(tokens)
                if tokens is not None
                else (self._secrets.seal_password(password) if password else None)
            ),
            last_status="untested",
        )

        def work(session: Session) -> None:
            session.add(account)

        await in_session(self._engine, work)
        return self._to_view(account)

    async def update_account(
        self,
        owner_id: str,
        account_id: str,
        *,
        name: str | None = None,
        config: dict | None = None,
        password: str | None = None,
        tokens: TokenBundle | None = None,
        enabled: bool | None = None,
    ) -> AccountView:
        row = await self._row(owner_id, account_id)
        updates: dict = {"updated_at": utcnow()}
        if name is not None:
            updates["name"] = name
        if config is not None:
            updates["config"] = config
        if enabled is not None:
            updates["enabled"] = enabled
        if tokens is not None:
            updates["secret_enc"] = self._secrets.seal_bundle(tokens)
            updates["auth_kind"] = AUTH_OAUTH
        elif password:
            updates["secret_enc"] = self._secrets.seal_password(password)
            updates["auth_kind"] = AUTH_PASSWORD

        def work(session: Session) -> MailAccount:
            stored = session.get(MailAccount, row.id)
            if stored is None:
                # The row was there when `_row` loaded it, and an await has passed since:
                # a concurrent delete makes this None. Guarded like `delete_account`'s own
                # lookup — otherwise `setattr(None, …)` raises AttributeError inside the
                # worker thread and the caller sees a 500 where 404 is the truth.
                raise NotFoundError(f"mail account {account_id!r} not found")
            for field, value in updates.items():
                setattr(stored, field, value)
            session.add(stored)
            return stored

        updated = await in_session(self._engine, work)
        await self._drop_transport(account_id)
        return self._to_view(updated)

    async def delete_account(self, owner_id: str, account_id: str) -> None:
        row = await self._row(owner_id, account_id)

        def work(session: Session) -> None:
            stored = session.get(MailAccount, row.id)
            if stored is not None:
                session.delete(stored)

        await in_session(self._engine, work)
        await self._drop_transport(account_id)

    async def probe_account(self, owner_id: str, account_id: str) -> AccountView:
        """Test the connection and record the outcome as operator-facing health
        (`XC-DEG-3`). Never raises for a *connection* problem — a failed probe is a
        result, and the recorded status is what the operator reads."""
        row = await self._row(owner_id, account_id)
        status, detail = "ok", None
        try:
            transport = await self._transport(row)
            await transport.probe()
        except MailError as exc:
            status, detail = "error", str(exc)
            await self._drop_transport(account_id)
        await self._record_health(account_id, status, detail)
        return self._to_view(await self._row(owner_id, account_id))

    # --- reading ---------------------------------------------------------------

    async def list_folders(self, owner_id: str, account_id: str) -> list[MailFolder]:
        transport = await self._transport(await self._row(owner_id, account_id))
        return await transport.list_folders()

    async def list_messages(
        self,
        owner_id: str,
        *,
        account_id: str | None = None,
        folder: str | None = None,
        limit: int = 50,
        unread_only: bool = False,
        include_spam: bool = False,
        refresh: bool = False,
    ) -> list[MessageView]:
        """The inbox listing. Served from the cache; the provider is consulted only when
        the window has lapsed or ``refresh`` forces it, and a provider that can't be
        reached degrades to the cache rather than erroring (`XC-DEG-3`)."""
        if account_id is not None and (refresh or self._stale(account_id, folder)):
            with suppress(MailError):
                await self._pull(owner_id, account_id, folder, full=refresh)
        return await self.cache.list_messages(
            owner_id,
            account_id=account_id,
            folder=folder,
            limit=limit,
            unread_only=unread_only,
            include_spam=include_spam,
        )

    async def read_message(self, owner_id: str, message_id: str) -> MessageDetail:
        """Open a message. The body is fetched from the provider the first time and
        cached with its `EMAIL-4` split, so re-opening it costs nothing."""
        detail = await self.cache.get(owner_id, message_id)
        if detail.body:
            return detail
        row = await self.cache.row_for(owner_id, message_id)
        transport = await self._transport(await self._row(owner_id, row.account_id))
        body = await transport.fetch(row.folder, row.uid)
        await self.cache.store_body(message_id, body)
        return await self.cache.get(owner_id, message_id)

    async def set_flags(
        self, owner_id: str, message_id: str, *, seen: bool | None = None,
        flagged: bool | None = None,
    ) -> None:
        """Write a flag remotely, then locally — the remote write is authoritative, so a
        failure leaves the cache honest rather than showing a state the server rejected."""
        row = await self.cache.row_for(owner_id, message_id)
        transport = await self._transport(await self._row(owner_id, row.account_id))
        await transport.flag(row.folder, row.uid, seen=seen, flagged=flagged)
        await self.cache.set_flags(message_id, seen=seen, flagged=flagged)

    async def move_message(self, owner_id: str, message_id: str, destination: str) -> None:
        row = await self.cache.row_for(owner_id, message_id)
        transport = await self._transport(await self._row(owner_id, row.account_id))
        await transport.move(row.folder, row.uid, destination)
        await self.cache.forget(message_id)

    async def delete_message(self, owner_id: str, message_id: str) -> None:
        row = await self.cache.row_for(owner_id, message_id)
        transport = await self._transport(await self._row(owner_id, row.account_id))
        await transport.delete(row.folder, row.uid)
        await self.cache.forget(message_id)

    # --- sending ---------------------------------------------------------------

    async def send(
        self,
        owner_id: str,
        account_id: str,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        in_reply_to: str | None = None,
    ) -> str:
        """Send a message. **The approval gate for this lives above** — in the agent's
        tool (`AE-3.1`) — because an operator pressing Send in the UI has already
        consented, while the model asking to must be asked."""
        if not to:
            raise MailError("a message needs at least one recipient")
        account = await self._row(owner_id, account_id)
        transport = await self._transport(account)
        message = OutgoingMail(
            to=tuple(MailAddress(address=address) for address in to),
            cc=tuple(MailAddress(address=address) for address in cc or ()),
            bcc=tuple(MailAddress(address=address) for address in bcc or ()),
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
        )
        return await transport.send(message)

    async def reply(
        self, owner_id: str, message_id: str, body: str, *, reply_all: bool = False
    ) -> str:
        """Reply to a cached message, threading it and quoting nothing the operator
        didn't write."""
        detail = await self.cache.get(owner_id, message_id)
        message = detail.message
        recipients = [message.from_address]
        cc = [
            address
            for address in detail.cc
            if reply_all and address not in recipients
        ]
        subject = message.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}".strip()
        return await self.send(
            owner_id,
            message.account_id,
            to=recipients,
            cc=cc,
            subject=subject,
            body=body,
            in_reply_to=message.message_id,
        )

    # --- EMAIL-3: style + suggestions -------------------------------------------

    async def learn_style(self, owner_id: str, account_id: str) -> str | None:
        """Learn the operator's writing style from their Sent folder. A hand-edited
        profile is left alone — their own description of their voice outranks ours."""
        stored = await self.profiles.get(owner_id)
        if stored is not None and stored.edited:
            return stored.profile
        account = await self._row(owner_id, account_id)
        transport = await self._transport(account)
        sent = next(
            (f for f in await transport.list_folders() if f.role == ROLE_SENT), None
        )
        if sent is None:
            raise MailError("this account has no Sent folder to learn from")
        headers = await transport.list_messages(sent.id, limit=SAMPLE_LIMIT)
        samples: list[str] = []
        for header in headers:
            with suppress(MailError):
                body = await transport.fetch(sent.id, header.uid)
                # Learn from what the operator wrote, not the history they quoted under it.
                samples.append(split_body(body.text).reply)
        profile = await self.style.learn(owner_id, samples)
        if profile is None:
            return stored.profile if stored is not None else None
        await self.profiles.set(owner_id, profile, sample_count=len(samples), edited=False)
        return profile

    async def suggest_replies(
        self, owner_id: str, message_id: str, *, count: int = 1
    ) -> list[DraftView]:
        """Pre-generate reply drafts for a message, using the prior exchange with that
        sender and the learned style (`EMAIL-3`). Returns already-stored suggestions when
        they exist, so opening a message twice doesn't re-spend model calls."""
        existing = await self.drafts.suggestions_for(owner_id, message_id)
        if existing:
            return existing
        detail = await self.read_message(owner_id, message_id)
        stored = await self.profiles.get(owner_id)
        context = await self.cache.list_messages(
            owner_id, account_id=detail.message.account_id, limit=50
        )
        prior = [
            await self.cache.get(owner_id, view.id)
            for view in context
            if view.from_address == detail.message.from_address and view.id != message_id
        ][:3]
        made: list[DraftView] = []
        for _ in range(max(1, count)):
            draft = await self.style.draft_reply(
                owner_id,
                detail,
                profile=stored.profile if stored is not None else None,
                context=prior,
            )
            if draft is None:
                break
            made.append(
                await self.drafts.create(
                    owner_id,
                    detail.message.account_id,
                    in_reply_to_id=message_id,
                    kind=KIND_SUGGESTED,
                    to=[detail.message.from_address],
                    subject=detail.message.subject,
                    body=draft.body,
                    label=draft.label,
                )
            )
        return made

    # --- sync ------------------------------------------------------------------

    def queue_sync(self, owner_id: str, account_id: str, folder: str | None = None) -> None:
        """Ask for a background pull. Non-blocking — the request path never waits on it."""
        self._worker.submit(SyncJob(owner_id, account_id, folder))

    async def _sync_loop(self) -> None:
        """Queue every enabled account on an interval. Parks implicitly: the worker holds
        the jobs until the vault is unlocked."""
        while True:
            try:
                await asyncio.sleep(self._sync_interval_s)
                for row in await self._rows(None):
                    if row.enabled:
                        self.queue_sync(row.owner_id, row.id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the loop must outlive one bad pass
                logger.warning("mail: scheduled sync pass failed", exc_info=True)

    async def _run_sync(self, job: SyncJob) -> None:
        await self._pull(job.owner_id, job.account_id, job.folder)
        await self._triage_backlog(job.owner_id)

    async def _pull(
        self, owner_id: str, account_id: str, folder: str | None, *, full: bool = False
    ) -> None:
        """Reconcile one folder from the provider into the cache.

        **Incremental by default, full on an explicit refresh.** The background sync asks
        only for what arrived after the newest cached uid, which is what keeps a poll
        cheap (an IMAP listing costs a fetch per message). That cursor can't see a flag
        another client changed on an older message, so an operator-driven refresh drops it
        and re-lists the newest page, reconciling read/flagged state as well as arrivals.
        """
        account = await self._row(owner_id, account_id)
        transport = await self._transport(account)
        target = folder or await self._default_folder(transport)
        if target is None:
            return
        since = None if full else await self.cache.newest_uid(account_id, target)
        headers = await transport.list_messages(target, since_uid=since)
        await self.cache.upsert_headers(owner_id, account_id, target, headers)
        # Keyed by the *requested* folder (``None`` for "the account's default"), so the
        # window a listing checks and the window a pull stamps are always the same key.
        self._refreshed[(account_id, folder or "")] = time.monotonic()
        await self._record_health(account_id, "ok", None, synced=True)

    async def _default_folder(self, transport: MailTransport) -> str | None:
        """The account's inbox, or its first folder when the provider declares no role."""
        folders = await transport.list_folders()
        inbox = next((f for f in folders if f.role == ROLE_INBOX), None)
        if inbox is not None:
            return inbox.id
        return folders[0].id if folders else None

    async def _triage_backlog(self, owner_id: str) -> None:
        """Triage what the pull just cached (`EMAIL-2`). Each message needs its body, so
        this fetches one at a time — bounded by the cache's backlog limit."""
        for detail in await self.cache.untriaged(owner_id):
            try:
                full = await self.read_message(owner_id, detail.message.id)
            except (MailError, NotFoundError):
                continue
            verdict = await self.triage.triage(owner_id, full)
            if verdict is None:
                return  # no model available — leave the rest untriaged, retry next pass
            await self.cache.apply_triage(
                detail.message.id,
                summary=verdict.summary,
                urgency=verdict.urgency,
                tags=[verdict.category],
                spam=verdict.spam,
                implied_events=[event.model_dump() for event in verdict.events],
            )

    # --- internals -------------------------------------------------------------

    def _stale(self, account_id: str, folder: str | None) -> bool:
        last = self._refreshed.get((account_id, folder or ""))
        return last is None or (time.monotonic() - last) > self._cache_ttl_s

    async def _transport(self, account: MailAccount) -> MailTransport:
        """The adapter for ``account``, rebuilt whenever its credentials have rotated.

        Credentials are resolved *first*, on every call: :meth:`MailSecrets.open_access`
        is the only place that knows an access token is near expiry and the only place
        that refreshes one, so short-circuiting on a cache hit is what freezes an account
        on the token it happened to hold an hour ago. Resolving is a vault decrypt and a
        JSON parse — cheap enough to pay per operation, and it reaches the provider only
        on the refresh it exists to perform.
        """
        # Serialized per account: `open_access` refreshes as a side effect, and two
        # concurrent refreshes of one account both spend the same refresh token. Against a
        # provider that rotates them (Google does) the loser re-seals a bundle the
        # provider has already invalidated, and the account needs a manual reconnect.
        async with self._transport_locks.setdefault(account.id, asyncio.Lock()):
            password, access_token = await self._secrets.open_access(account)
            if not password and not access_token:
                raise MailUnavailableError("this account has no stored credentials")
            credentials = (password, access_token)
            live = self._transports.get(account.id)
            if live is not None and live.credentials == credentials:
                return live.transport
            # Rotated token or edited password: the cached adapter's spec is frozen, and
            # the connection it holds is authenticated with a secret that no longer works.
            await self._drop_transport(account.id)
            spec = AccountSpec(
                account_id=account.id,
                address=self._vault.decrypt_str(account.address_enc),
                provider=account.provider,
                auth_kind=account.auth_kind,
                config=dict(account.config or {}),
                password=password,
                access_token=access_token,
            )
            transport = _TRANSPORTS[account.provider](spec)
            self._transports[account.id] = LiveTransport(transport, credentials)
            return transport

    async def _drop_transport(self, account_id: str) -> None:
        live = self._transports.pop(account_id, None)
        if live is not None:
            with suppress(Exception):
                await live.transport.close()

    async def _rows(self, owner_id: str | None) -> list[MailAccount]:
        def work(session: Session) -> list[MailAccount]:
            query = select(MailAccount)
            if owner_id is not None:
                query = query.where(MailAccount.owner_id == owner_id)
            return list(session.exec(query.order_by(MailAccount.created_at)).all())

        return await in_session(self._engine, work)

    async def _row(self, owner_id: str, account_id: str) -> MailAccount:
        def work(session: Session) -> MailAccount | None:
            row = session.get(MailAccount, account_id)
            return row if row is not None and row.owner_id == owner_id else None

        row = await in_session(self._engine, work)
        if row is None:
            raise NotFoundError(f"no such mail account: {account_id}")
        return row

    async def _record_health(
        self, account_id: str, status: str, detail: str | None, *, synced: bool = False
    ) -> None:
        def work(session: Session) -> None:
            row = session.get(MailAccount, account_id)
            if row is None:
                return
            row.last_status = status
            row.last_error_detail = detail
            if synced:
                row.last_synced_at = utcnow()
            session.add(row)

        await in_session(self._engine, work)

    def _to_view(self, row: MailAccount) -> AccountView:
        return AccountView(
            id=row.id,
            name=row.name,
            address=self._vault.decrypt_str(row.address_enc),
            provider=row.provider,
            auth_kind=row.auth_kind,
            enabled=row.enabled,
            status=row.last_status or "untested",
            error_detail=row.last_error_detail,
            last_synced_at=row.last_synced_at,
            has_secret=row.secret_enc is not None,
        )
