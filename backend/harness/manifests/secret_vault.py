"""The operator's secrets manager (`VAULT-*`) — distinct from the at-rest key
custody vault. Constructing it *is* registering its lock hook (it calls
`vault.register_on_lock` itself), so an app lock ends every secret session too and
there is deliberately nothing more to wire here."""

from __future__ import annotations

from harness.manifest import (
    DormantCategory,
    FeatureManifest,
    FeatureRuntime,
    HarnessContext,
    ServiceContainer,
)
from routes import secret_vault as secret_vault_routes
from services.secret_vault import SecretVaultService
from tools.vault import vault_toolset


async def _available(caps: ServiceContainer, owner_id: str) -> bool:
    """Whether a secrets vault has ever been created — ``configured``, deliberately not
    ``unlocked``.

    Locked is a state the tools are built for: the agent asks, the operator opens it, the
    work continues. Never created is not, and the two must not be conflated — withholding
    on a lock would take the tools away exactly when the model is about to ask for them
    back. ``status`` is also the one read that does *not* slide the idle deadline
    (``services/secret_vault.py``), so asking it every turn cannot hold the vault open.
    """
    return (await caps.get(SecretVaultService).status(owner_id)).configured


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    secret_vault = SecretVaultService(ctx.engine, ctx.vault)
    return FeatureRuntime(
        services=(secret_vault,),
        capabilities=(secret_vault,),
        state={"secret_vault": secret_vault},
    )


MANIFEST = FeatureManifest(
    name="secret-vault",
    routers=(secret_vault_routes.router,),
    toolsets=(("vault", vault_toolset),),
    dormant=(
        DormantCategory(
            "vault",
            "the operator's stored secrets — list what is held, and read one credential "
            + "by name when a task needs it",
        ),
    ),
    available=_available,
    build=_build,
)
