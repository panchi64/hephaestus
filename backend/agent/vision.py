"""Vision OCR — the codebase's first multimodal model call.

The engine-layer implementation of the :class:`~services.upload_extraction.VisionOCR`
seam the upload extractor depends on. It belongs here, beside the conversation namer,
because it *runs a model*: the registry resolves a vision-capable endpoint and a bare
``make_utility_agent`` transcribes a page image — the same one-shot utility-call shape
as titling, just with image input. Keeping it in ``agent/`` lets the extractor stay a
pure services-layer capability that never imports the engine.

A vision model is resolved **once per document** (``prepare``) and reused across its
pages, so a 40-page scan isn't 40 registry resolutions. Each page transcription is
bounded by a timeout so a slow or stuck model can't hold an upload's extraction open.
"""

from __future__ import annotations

import asyncio

from pydantic_ai import Agent, BinaryContent

from agent.meta import make_utility_agent
from prompts.utility import OCR_INSTRUCTIONS
from services.registry import ModelRegistry

# The per-call nudge that accompanies a page image; the standing transcription rules
# live in OCR_INSTRUCTIONS.
_OCR_PROMPT = "Transcribe all readable text from this document page."


class _PreparedTranscriber:
    """A vision agent bound to one document's owner, ready to read its page images."""

    def __init__(self, agent: Agent[None, str], timeout_s: float) -> None:
        self._agent = agent
        self._timeout_s = timeout_s

    async def transcribe(self, image: bytes) -> str:
        result = await asyncio.wait_for(
            self._agent.run([_OCR_PROMPT, BinaryContent(data=image, media_type="image/jpeg")]),
            timeout=self._timeout_s,
        )
        return str(result.output).strip()


class VisionTranscriber:
    """Resolves a vision model and transcribes document page images with it.

    Implements the extractor's ``VisionOCR`` seam. ``prepare`` resolves the vision
    endpoint (raising ``DegradedCapabilityError`` when none is configured — the
    extractor turns that into a note) and returns a transcriber the extractor drives
    page by page."""

    def __init__(self, registry: ModelRegistry, *, timeout_s: float = 120.0) -> None:
        self._registry = registry
        self._timeout_s = timeout_s

    async def prepare(self, owner_id: str) -> _PreparedTranscriber:
        model = await self._registry.resolve_vision(owner_id)
        agent = make_utility_agent(model, output_type=str, instructions=OCR_INSTRUCTIONS)
        return _PreparedTranscriber(agent, self._timeout_s)
