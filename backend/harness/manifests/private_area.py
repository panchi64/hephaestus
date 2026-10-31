"""The Private area's PIN and lock surface. The service itself is core (`app.py`), since the
conversation routes and the recall tools all consult it; no token scope claims `/private`."""

from __future__ import annotations

from harness.manifest import FeatureManifest
from routes import private_area as private_area_routes

MANIFEST = FeatureManifest(
    name="private-area",
    routers=(private_area_routes.router,),
)
