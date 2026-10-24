"""Bulk clears of the operator's chats, memories, notifications and uploads."""

from __future__ import annotations

from harness.manifest import FeatureManifest
from routes import data as data_routes

MANIFEST = FeatureManifest(name="data", routers=(data_routes.router,))
