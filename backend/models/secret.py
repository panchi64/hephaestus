"""Password-vault schema (`VAULT-*`) — the operator's secrets manager.

See ``models/mail.py`` for why the module is imported before it declares anything.

This is the operator's user-facing secrets manager — an additional encrypted layer on top of
at-rest encryption — and is deliberately distinct from ``core/vault``, the password-derived
key custody that unlocks the app at login. Its unlocked state is held **in memory only and
never persisted**, so nothing here records whether the vault is open: the two tables below
carry only what must survive a restart — how to *re*-derive the key from a passphrase the
operator supplies again, and the sealed entries themselves.

**Two layers, two keys.** Every value an entry holds is sealed under the vault's own
passphrase-derived key first, and the resulting token is then sealed again by the login DEK
like any other sensitive column (`XC-SEC-3`). Reading one back therefore needs *both* locks
open, which is exactly what "an additional encrypted layer on top of at-rest encryption"
means: a login-unlocked process still cannot read a secret while the secrets manager is
locked.

Because names are ciphertext (and non-deterministic ciphertext at that), the DB cannot index
or uniquely constrain them — entry identity is the primary key, and a duplicate name is the
operator's business, not a schema violation.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table, TableArgs


class SecretVaultConfig(Table, table=True):
    """How to re-derive the secrets manager's key — never the key itself.

    Mirrors ``core/vault``'s keyfile part for part (a verifier, a KDF salt, and a data key
    wrapped under the passphrase-derived KEK) with **its own** salt and its own verifier, so
    the two locks are independent: neither passphrase derives the other's key. The wrapped
    key is what lets the vault passphrase change later without re-sealing every entry.
    """

    __tablename__: ClassVar[str] = "secret_vault_config"
    # One vault per operator: configuring twice is an error, not a second row.
    __table_args__: ClassVar[TableArgs] = (
        UniqueConstraint("owner_id", name="uq_secret_vault_config_owner"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # Argon2id PHC verifier for the vault passphrase — a one-way hash, useless for
    # decryption, so it stays in the clear exactly like the login keyfile's.
    verifier: str
    # The vault KDF's salt. Independent of the login KEK salt by construction.
    kek_salt: str
    # AEAD ciphertext of the vault data key, wrapped under the passphrase-derived KEK and
    # then sealed at rest by the login DEK. Opening it needs the passphrase; possessing the
    # login DEK alone is not enough.
    wrapped_key_enc: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class SecretEntry(Table, table=True):
    """One stored credential: what it is, who it's for, where it's used, and the secret."""

    __tablename__: ClassVar[str] = "secret_entries"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # Double-sealed (vault key, then the login DEK) ciphertext of the entry's four fields.
    # Even the name is content here — the label on a credential names the system it opens.
    name_enc: str
    username_enc: str
    url_enc: str
    password_enc: str
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow)
