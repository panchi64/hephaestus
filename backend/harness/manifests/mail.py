"""Email (`EMAIL-1..5`) — accounts, the sync loop, the inbox cache, triage and
drafts. Built right after the attention surface (the other half of their cycle) so
a triage alert has somewhere to land. Its sync worker seals message content, so it
parks while the vault is locked rather than failing. It also stops before the
attention surface (registered after ⇒ stops earlier): the sync loop can raise a
triage alert on its way down, and a channel delivery may still be draining."""

from __future__ import annotations

from harness.manifest import (
    DormantCategory,
    FeatureManifest,
    FeatureRuntime,
    HarnessContext,
    ServiceContainer,
)
from routes import mail as mail_routes
from services.credential_store import CredentialStore
from services.mail import MailService
from services.notifications import NotificationService
from services.registry import ModelRegistry
from tools.mail import mail_toolset


async def _available(caps: ServiceContainer, owner_id: str) -> bool:
    """Whether there is a mailbox to read at all.

    One id off an indexed column — no transport is opened and nothing is decrypted, so
    the question is answerable on every turn and while the app is locked. Without it the
    seven mail tools ride in every request and each one answers "no account configured",
    which costs the model a call to learn what the catalog could have told it for free.
    """
    return await caps.get(MailService).has_accounts(owner_id)


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    mail = MailService(
        ctx.engine,
        ctx.vault,
        ctx.services.get(CredentialStore),
        ctx.services.get(ModelRegistry),
        notifications=ctx.services.get(NotificationService),
    )
    await ctx.lifecycle.start("mail", start=mail.start, stop=mail.stop)
    return FeatureRuntime(services=(mail,), capabilities=(mail,), state={"mail": mail})


MANIFEST = FeatureManifest(
    name="mail",
    after=("notifications",),
    routers=(mail_routes.router,),
    toolsets=(("mail", mail_toolset),),
    dormant=(
        DormantCategory(
            "mail",
            "read and act on the operator's email — search the inbox, open a message, "
            + "mark it, draft a reply, send",
        ),
    ),
    available=_available,
    build=_build,
)
