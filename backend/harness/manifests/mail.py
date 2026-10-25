"""Email — accounts, the sync loop, the inbox cache, triage, drafts, and the email
notification channel. Built after the attention surface so a triage alert has somewhere
to land, and so it stops first: the sync loop can raise an alert on its way down. Its sync
worker seals message content, so it parks while the vault is locked rather than failing."""

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
from services.mail.channel import EmailChannel
from services.mail.oauth import OAUTH_CLIENT_SERVICES
from services.notifications import NotificationService
from services.permissions.projections import Projection
from services.registry import ModelRegistry
from services.settings_store import SettingsStore
from services.tool_sensitivity import Sensitivity
from tools.mail import mail_toolset

_SENSITIVITY = {
    "mail_list_accounts": Sensitivity.READ,
    "mail_list_messages": Sensitivity.READ,
    "mail_read": Sensitivity.READ,
    # Sealed in this installation's own database; the draft never reaches the mail server.
    "mail_draft_reply": Sensitivity.WORKSPACE_WRITE,
    # Reversible, and still on the far side of the operator's mail server.
    "mail_mark": Sensitivity.EXTERNAL_EFFECT,
    "mail_reply": Sensitivity.EXTERNAL_EFFECT,
    "mail_send": Sensitivity.EXTERNAL_EFFECT,
}

# The body is in no group: it is pure content, often the operator's own words, and who a
# message goes to is what a review of "should this be sent" turns on. `explanation` is the
# tool's mandated plain statement of who it reaches and what it says, written for a reader.
_PROJECTIONS = {
    "mail_send": Projection(
        "Sends an email",
        identity=("account_id", "to", "cc", "subject"),
        content=("explanation",),
    ),
    "mail_reply": Projection(
        "Replies to an email",
        identity=("message_id", "reply_all"),
        content=("explanation",),
    ),
    "mail_mark": Projection("Changes an email's flags", identity=("message_id", "seen", "flagged")),
}


async def _available(caps: ServiceContainer, owner_id: str) -> bool:
    """Whether there is a mailbox to read at all.

    One id off an indexed column — no transport is opened and nothing is decrypted, so
    the question is answerable on every turn and while the app is locked. Without it the
    seven mail tools ride in every request and each one answers "no account configured",
    which costs the model a call to learn what the catalog could have told it for free.
    """
    return await caps.get(MailService).has_accounts(owner_id)


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    notifications = ctx.services.get(NotificationService)
    mail = MailService(
        ctx.engine,
        ctx.vault,
        ctx.services.get(CredentialStore),
        ctx.services.get(ModelRegistry),
        notifications=notifications,
    )
    notifications.add_channel(EmailChannel(mail, ctx.services.get(SettingsStore), ctx.vault))
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
    tool_sensitivity=_SENSITIVITY,
    projections=_PROJECTIONS,
    personal_categories=frozenset({"mail"}),
    credentials=OAUTH_CLIENT_SERVICES,
    build=_build,
)
