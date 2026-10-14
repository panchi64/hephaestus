"""Uploads (`UP-*`) — a sealed file store with off-request text extraction, indexed
into the corpus.

Bytes are stored sealed and deduped by content hash; extraction drains off the
request path on a lock-aware worker (MinerU in front when detected on the host,
built-in pypdfium2 + vision OCR as the floor). Vision OCR runs a model, so it lives
in the engine layer (`agent/vision`) and is injected through a narrow seam.
"""

from __future__ import annotations

import logging

from agent.vision import VisionTranscriber
from core.api_scopes import ScopeClaim
from core.config import Settings
from core.ratelimit import RateLimiter
from harness.manifest import FeatureManifest, FeatureRuntime, HarnessContext
from routes import uploads as uploads_routes
from services.corpus import CorpusChunkStore, CorpusIndex
from services.corpus.uploads import UploadsAdapter
from services.registry import ModelRegistry
from services.upload_extraction import BasicExtractor, FallbackExtractor, UploadExtractor
from services.upload_mineru import MinerUExtractor
from services.uploads import UploadStore
from tools.attachments import attachments_toolset

logger = logging.getLogger(__name__)


def _build_extractor(registry: ModelRegistry, settings: Settings) -> UploadExtractor:
    """Pick the extraction engine. The built-in (pypdfium2 text + vision OCR) is
    always available; when MinerU is pinned or detected on the host it goes in
    front, with the built-in as the fallback so a missing/broken/out-of-resources
    MinerU degrades to a working extraction instead of an error. The original bytes
    are kept sealed regardless, so a built-in extraction can be re-run later."""
    basic = BasicExtractor(
        VisionTranscriber(registry, timeout_s=settings.upload_ocr_timeout_s),
        max_pages=settings.upload_extract_max_pages,
    )
    if settings.upload_extractor == "basic":
        return basic
    if settings.upload_extractor == "mineru" or MinerUExtractor.is_available():
        logger.info("uploads: MinerU extraction enabled (high-fidelity, degrades to built-in)")
        return FallbackExtractor(MinerUExtractor(timeout_s=settings.upload_mineru_timeout_s), basic)
    logger.info("uploads: built-in extraction (MinerU not detected on host)")
    return basic


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    adapter = UploadsAdapter(
        ctx.engine, ctx.services.get(CorpusChunkStore), ctx.vault.unlocked_event
    )
    uploads = UploadStore(
        ctx.engine,
        ctx.vault,
        adapter,
        _build_extractor(ctx.services.get(ModelRegistry), ctx.settings),
    )
    ctx.services.get(CorpusIndex).register(adapter)
    await ctx.lifecycle.start("corpus-uploads", start=adapter.start, stop=adapter.stop)
    await ctx.lifecycle.start("uploads", start=uploads.start, stop=uploads.stop)
    rate_limiter = RateLimiter(
        rate_per_second=ctx.settings.upload_rate_per_minute / 60.0,
        burst=ctx.settings.upload_rate_burst,
    )
    return FeatureRuntime(
        services=(uploads, adapter),
        capabilities=(uploads,),
        state={
            "uploads": uploads,
            "corpus_uploads": adapter,
            "upload_rate_limiter": rate_limiter,
        },
    )


MANIFEST = FeatureManifest(
    name="uploads",
    after=("corpus",),
    routers=(uploads_routes.router,),
    api_scopes=(ScopeClaim("knowledge", ("/uploads",)),),
    toolsets=(("attachments", attachments_toolset),),
    build=_build,
)
