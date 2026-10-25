"""Push each manifest's static declarations into core's process-wide tables."""

from __future__ import annotations

from collections.abc import Iterable

from harness.manifest import FeatureManifest
from runs.lanes import RUN_KINDS
from services.credential_store import declare_services
from services.notification_channels import OUT_OF_BAND_KINDS
from services.permissions.projections import PROJECTIONS
from services.tool_sensitivity import declare_sensitivity
from tools.platform import PLATFORM_SOURCES
from tools.spill import PERSONAL_CATEGORIES


def declare(manifests: Iterable[FeatureManifest]) -> None:
    for manifest in manifests:
        declare_sensitivity(manifest.tool_sensitivity)
        PROJECTIONS.declare(manifest.projections)
        PERSONAL_CATEGORIES.declare(manifest.personal_categories)
        declare_services(manifest.credentials)
        RUN_KINDS.declare(manifest.run_kinds)
        OUT_OF_BAND_KINDS.declare(manifest.out_of_band_kinds)
        PLATFORM_SOURCES.declare(manifest.platform_sources)
