"""Platform search: one retrieval surface over everything outside the conversation."""

from __future__ import annotations

from harness.manifest import FeatureManifest
from tools.platform import platform_toolset

MANIFEST = FeatureManifest(
    name="platform",
    toolsets=(("platform", platform_toolset),),
    # Approval-gated at call time unless the search stays within memory and the catalogs.
    # `platform_read` of a private chat parks too, but as a guard, which no grant answers.
    gated_tools=frozenset({"platform_search"}),
)
