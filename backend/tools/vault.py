"""Password-vault tools (`VAULT-2`) — the agent's read access to the operator's secrets.

See ``tools/mail.py`` for why the category is registered before it exists.

The agent reading the operator's secrets manager is an explicitly sensitive action, so
**every** tool here carries ``requires_approval=True`` — the static marking, as in
``tools/code.py``'s ``run_host_command``. The marking is static rather than conditional on
purpose: there is no shape of vault read that is routine enough to be waved through, and a
condition is one more thing that can be wrong. Each call takes a plain-language ``reason``
that rides onto the approval prompt, so the operator judges *why* a credential is wanted
without reconstructing it from the conversation.

Thin, like every tool: the lock, the key, and the sealing live in ``services/secret_vault``.
A locked vault is reported back as a fact the model can act on (ask the operator to unlock),
not raised — the model retrying cannot open a lock only the operator can.
"""

from __future__ import annotations

from pydantic_ai import FunctionToolset, RunContext

from core.exceptions import NotFoundError
from services.secret_vault import SecretEntryView, SecretVaultLocked, SecretVaultService

from .deps import RunDeps

_UNAVAILABLE = (
    "The password vault is not available in this session, so stored credentials cannot be read."
)
_LOCKED = (
    "The password vault is locked. Only the operator can unlock it — ask them to "
    "open it before trying again."
)


def _summary(view: SecretEntryView) -> dict[str, object]:
    """An entry without its secret — enough to say what is stored and pick one."""
    return {"id": view.id, "name": view.name, "username": view.username, "url": view.url}


def vault_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool(requires_approval=True)
    async def list_entries(ctx: RunContext[RunDeps], reason: str) -> dict[str, object]:
        """Lists the entries in the operator's password vault: each one's `id`, `name`,
        `username` and `url`, never the passwords.

        Call it to find the entry a task needs before reading it with `vault_get_entry`.
        When the vault is locked, the result says so; only the operator can unlock it,
        so ask them rather than retrying.

        Args:
            reason: Which credential you are looking for and what task needs it.
        """
        del reason  # Read by the approval card from the call's arguments, not by this body.
        service = ctx.deps.caps.get_optional(SecretVaultService)
        if service is None:
            return {"ok": False, "error": _UNAVAILABLE}
        try:
            views = await service.list_entries(ctx.deps.owner_id)
        except SecretVaultLocked:
            return {"ok": False, "error": _LOCKED}
        return {"ok": True, "entries": [_summary(v) for v in views]}

    @toolset.tool(requires_approval=True)
    async def get_entry(ctx: RunContext[RunDeps], entry_id: str, reason: str) -> dict[str, object]:
        """Returns one vault entry in full, its password included.

        Use it only when the task needs the secret itself, such as signing in or
        configuring a service, and never to show the operator a password they can open
        themselves. Once you have it, use it where the task needs it and nowhere else:
        don't repeat it in your reply, write it into a file the task did not ask for, or
        pass it to a host it does not belong to.

        Args:
            entry_id: The entry's `id`, from `vault_list_entries`.
            reason: What you will do with this credential, and where it will be used.
        """
        del reason  # Read by the approval card from the call's arguments, not by this body.
        service = ctx.deps.caps.get_optional(SecretVaultService)
        if service is None:
            return {"ok": False, "error": _UNAVAILABLE}
        try:
            view = await service.get(ctx.deps.owner_id, entry_id)
        except SecretVaultLocked:
            return {"ok": False, "error": _LOCKED}
        except NotFoundError:
            return {"ok": False, "error": f"No vault entry with id {entry_id!r}."}
        return {"ok": True, "entry": {**_summary(view), "password": view.password}}

    return toolset
