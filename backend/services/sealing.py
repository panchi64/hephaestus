"""Sealing a column that used to be stored in the clear (`XC-SEC-3`).

Adding at-rest encryption to a field that already has rows is not a migration problem —
it is a **lock problem**. ``core/db.init_db`` upgrades to head at startup, *before* any
login, so a migration runs with the vault locked and no key: it can add the sealed column
but it cannot fill it. The established answer is the one ``_backfill_embeddings`` and the
lock-aware ``WriteBehindWorker`` already take — do the key-needing work in the background
**after unlock**.

That makes a converting column live in two states at once, so this module owns both
halves of the pattern:

- :func:`open_sealed` — the **read**. Prefer the sealed value; fall back to the legacy
  cleartext while it is still there. A half-migrated database therefore reads correctly
  from the first request, before the backfill has touched a single row — never a garbled
  string, never a missing one.
- :func:`seal_legacy_column` — the **heal**. Once unlocked, seal every row that still has
  cleartext and **clear the legacy column in the same write**, so the plaintext stops
  existing rather than merely being ignored. Idempotent, so a re-run after a crash
  finishes the job instead of double-sealing.

New sensitive fields don't need any of this — they are sealed from their first write. This
is only for the columns that predate their own encryption.
"""

from __future__ import annotations

import logging
from typing import cast

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, col, select

from core.db import in_session
from core.vault import Vault

logger = logging.getLogger(__name__)


def open_sealed(vault: Vault, sealed: str | None, legacy: str | None) -> str | None:
    """The field's value, from whichever state this row is in.

    The sealed column wins whenever it is populated; the legacy cleartext answers only
    until the backfill has reached this row. ``None`` from both means the field is simply
    unset (an untitled conversation), which is not the same as un-migrated.
    """
    if sealed is not None:
        return vault.decrypt_str(sealed)
    return legacy


async def seal_legacy_column(
    *,
    engine: Engine,
    vault: Vault,
    model_cls: type[SQLModel],
    legacy_attr: str,
    sealed_attr: str,
    batch_size: int = 200,
) -> int:
    """Seal every row whose legacy cleartext column is still populated, clearing it as we
    go. Returns how many rows were healed.

    Requires an unlocked vault — call it behind ``vault.unlocked_event.wait()``. Rows are
    walked in bounded batches so a large table doesn't hold one session (or one
    transaction) for the whole sweep, and each batch is sealed *inside* its own write, so
    an interruption leaves consistent rows behind and the next run picks up the rest.
    """
    healed = 0
    while True:

        def work(session: Session) -> int:
            legacy_col = col(cast("str | None", getattr(model_cls, legacy_attr)))
            rows = session.exec(
                select(model_cls).where(legacy_col.is_not(None)).limit(batch_size)
            ).all()
            for row in rows:
                plaintext = cast("str", getattr(row, legacy_attr))
                # Seal only what the sealed column doesn't already hold: a row written by
                # the new code path is already sealed and just needs its legacy value
                # dropped (belt-and-braces — that path clears it at write time).
                if getattr(row, sealed_attr) is None:
                    setattr(row, sealed_attr, vault.encrypt_str(plaintext))
                setattr(row, legacy_attr, None)
                session.add(row)
            return len(rows)

        count = await in_session(engine, work)
        healed += count
        if count < batch_size:
            break
    if healed:
        logger.info(
            "at-rest: sealed %d legacy %s.%s value(s)",
            healed,
            getattr(model_cls, "__tablename__", model_cls.__name__),
            legacy_attr,
        )
    return healed
