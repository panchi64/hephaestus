"""Watching the services machine for a death while it is up."""

from __future__ import annotations

from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from models.notification import NotificationKind
from routes.deps import OPERATOR_ID
from services.notifications import NotificationService
from services.vm import ServicesMachine
from services.vm.liveness import VMLiveness


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    vm = ctx.services.get_optional(ServicesMachine)
    if vm is None:
        return FeatureRuntime()
    notifications = ctx.services.get(NotificationService)

    async def notify(title: str, body: str) -> None:
        _ = await notifications.notify(OPERATOR_ID, NotificationKind.VM_DOWN, title, body)

    liveness = VMLiveness(vm, notify)
    await ctx.lifecycle.start("vm-liveness", start=liveness.start, stop=liveness.stop)
    return FeatureRuntime(services=(liveness,))


MANIFEST = FeatureManifest(
    name="vm-liveness",
    after=("notifications",),
    enabled=lambda settings: settings.vm_enabled,
    build=_build,
)
