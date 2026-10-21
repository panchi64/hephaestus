"""The operator's route for allowing a refused domain; claims no API scope on purpose."""

from __future__ import annotations

from harness.manifest import FeatureManifest
from routes import egress_grants as egress_grants_routes

MANIFEST = FeatureManifest(name="egress-grants", routers=(egress_grants_routes.router,))
