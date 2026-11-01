"""Extract a usable text layer from an uploaded file — the `UP-2` pipeline.

Extraction is a **seam** (:class:`UploadExtractor`), so the heavy, high-fidelity path
and the light built-in one are interchangeable and composable:

- :class:`BasicExtractor` — the zero-setup built-in. PDFs are opened with ``pypdfium2``
  (a pure-wheel PDFium binding — no system packages, so it stays platform-agnostic,
  `XC-PORT`); each page's embedded text is pulled directly, and a page with little or
  no embedded text is image-only/scanned, so it is rasterized and handed to a **vision
  model** for OCR. Native text and OCR text occupy the same slot, so a mixed PDF comes
  out whole and in order. Plain-text files pass through decoded; anything else yields
  empty text. The vision call lives a layer up (``agent/vision.py``) so this capability
  never imports the engine — it depends only on the small :class:`VisionOCR` seam,
  which the agent layer implements and ``app.py`` injects.
- :class:`~services.upload_mineru.MinerUExtractor` — the high-fidelity path (layout,
  tables, formulas → clean Markdown), used when the operator has the ``mineru`` tool on
  the host. It is detected, not bundled, so the project venv never pulls its PyTorch stack.
- :class:`FallbackExtractor` — composes the two: try the primary (MinerU), drop to the
  built-in on any failure, so a missing or broken high-fidelity path degrades to a
  working extraction rather than an error.

Every bound or degradation is reported in the result's ``note`` rather than silently
dropping content — a scanned PDF with no vision model configured comes back empty *with
a reason*, which the store turns into an actionable error the operator can retry. CPU
work (parsing, rasterizing) runs in a worker thread so it never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass, field
from typing import Protocol

import pypdfium2 as pdfium

from core.exceptions import DegradedCapabilityError

logger = logging.getLogger(__name__)

# A page with fewer than this many embedded-text characters is treated as
# image-only and routed to vision OCR.
_SPARSE_TEXT_THRESHOLD = 16
# Render scale for OCR rasterization — ~144 DPI (72 × 2), a good legibility/size
# trade-off for a vision model that downscales internally anyway.
_RENDER_SCALE = 2
_JPEG_QUALITY = 85


class PreparedVisionOCR(Protocol):
    """A vision model resolved once for one document, ready to read its pages."""

    async def transcribe(self, image: bytes) -> str: ...


class VisionOCR(Protocol):
    """The seam the extractor needs from the engine: resolve a vision model for an
    owner (``prepare``), then transcribe page images with it. ``prepare`` raises
    :class:`DegradedCapabilityError` when the operator has configured no vision
    endpoint — the extractor turns that into a note rather than a failure."""

    async def prepare(self, owner_id: str) -> PreparedVisionOCR: ...


# Which extractor produced a result — recorded on the upload so the UI can flag
# built-in ("basic") extractions as candidates to re-run through MinerU later, and
# leave high-fidelity ("mineru") ones alone.
EXTRACTOR_BASIC = "basic"
EXTRACTOR_MINERU = "mineru"


@dataclass(frozen=True)
class ExtractionResult:
    """The outcome of extracting one upload's text."""

    text: str
    # True when a vision model produced (some of) the text — an image-only/scanned PDF.
    vision: bool = False
    # A short, clear note when extraction was bounded or degraded (pages beyond the
    # cap, scanned pages with no vision model, a page the model couldn't read). None
    # when extraction was clean. Never holds operator content.
    note: str | None = None
    # Which extractor produced this text ("basic" | "mineru"). The store persists it so
    # a fallback-extracted file is identifiable and re-runnable for higher fidelity.
    extractor: str = EXTRACTOR_BASIC


class UploadExtractor(Protocol):
    """The extraction seam the :class:`~services.uploads.UploadStore` depends on. One
    method: turn a file's bytes into an :class:`ExtractionResult`. The built-in
    :class:`BasicExtractor`, the high-fidelity :class:`~services.upload_mineru.MinerUExtractor`,
    and the composing :class:`FallbackExtractor` all satisfy it, so the store is blind
    to which is wired."""

    async def extract(
        self, owner_id: str, raw: bytes, mime: str, filename: str
    ) -> ExtractionResult: ...


@dataclass
class ScannedPage:
    index: int
    native_text: str
    image: bytes | None  # JPEG bytes when the page needs OCR, else None


@dataclass
class PdfScan:
    pages: list[ScannedPage] = field(default_factory=list)
    total_pages: int = 0


def _is_pdf(mime: str, filename: str) -> bool:
    return mime == "application/pdf" or filename.lower().endswith(".pdf")


def _is_text(mime: str, filename: str) -> bool:
    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        return True
    return filename.lower().endswith((".txt", ".md", ".markdown", ".csv", ".json"))


class BasicExtractor:
    """The zero-setup built-in extractor: native PDF text via pypdfium2, vision OCR for
    scanned pages, decoded text files. Always available — it's also the fallback the
    high-fidelity path degrades to."""

    def __init__(self, ocr: VisionOCR, *, max_pages: int = 50) -> None:
        self._ocr: VisionOCR = ocr
        self._max_pages: int = max_pages

    async def extract(
        self, owner_id: str, raw: bytes, mime: str, filename: str
    ) -> ExtractionResult:
        """Pull a text layer from ``raw``. Never raises for an unreadable *content*
        problem (a scanned page with no vision model, an empty file) — that comes back
        as empty text plus a note. It may still propagate a hard parse failure, which
        the caller records as an error status."""
        if _is_pdf(mime, filename):
            return await self._extract_pdf(owner_id, raw)
        if _is_text(mime, filename):
            return ExtractionResult(text=raw.decode("utf-8", errors="replace").strip())
        return ExtractionResult(text="")

    async def _extract_pdf(self, owner_id: str, raw: bytes) -> ExtractionResult:
        scan = await asyncio.to_thread(scan_pdf, raw, self._max_pages)
        notes: list[str] = []
        skipped = scan.total_pages - len(scan.pages)
        if skipped > 0:
            notes.append(
                f"{skipped} page(s) beyond the {self._max_pages}-page limit were not extracted"
            )

        needs_ocr = [page for page in scan.pages if page.image is not None]
        ocr_text: dict[int, str] = {}
        vision_used = False
        if needs_ocr:
            vision_used, ocr_notes = await self._ocr_pages(owner_id, needs_ocr, ocr_text)
            notes.extend(ocr_notes)

        ordered = [ocr_text.get(page.index, page.native_text).strip() for page in scan.pages]
        text = "\n\n".join(part for part in ordered if part)
        return ExtractionResult(text=text, vision=vision_used, note="; ".join(notes) or None)

    async def _ocr_pages(
        self, owner_id: str, pages: list[ScannedPage], out: dict[int, str]
    ) -> tuple[bool, list[str]]:
        """OCR each scanned page into ``out`` (keyed by page index), resolving the
        vision model once for the whole document. Returns whether any page was read
        and any degradation notes."""
        try:
            prepared = await self._ocr.prepare(owner_id)
        except DegradedCapabilityError:
            return False, [
                f"{len(pages)} scanned page(s) need a vision model to read; none is configured"
            ]
        read_any = False
        notes: list[str] = []
        for page in pages:
            assert page.image is not None
            try:
                out[page.index] = await prepared.transcribe(page.image)
                read_any = True
            except Exception:  # one unreadable page mustn't abort the rest
                logger.exception("vision OCR failed for page %d", page.index)
                notes.append(f"page {page.index + 1} could not be read by the vision model")
        return read_any, notes


def scan_pdf(raw: bytes, max_pages: int) -> PdfScan:
    """Open the PDF and, for each page up to ``max_pages``, pull embedded text and —
    when that text is sparse (a scanned page) — rasterize it to JPEG for OCR. Pure
    CPU work; the caller runs it in a thread.

    Public because uploads are not its only caller: ``services/webfetch/pdf.py`` reads a
    fetched PDF through exactly this path, so the two agree on page limits and on what
    counts as a scanned page."""
    pdf = pdfium.PdfDocument(raw)
    try:
        total = len(pdf)
        scan = PdfScan(total_pages=total)
        for index in range(min(total, max_pages)):
            page = pdf[index]
            textpage = page.get_textpage()
            native = (textpage.get_text_range() or "").strip()
            _ = textpage.close()
            image = _render_jpeg(page) if len(native) < _SPARSE_TEXT_THRESHOLD else None
            _ = page.close()
            scan.pages.append(ScannedPage(index=index, native_text=native, image=image))
        return scan
    finally:
        _ = pdf.close()


def _render_jpeg(page: pdfium.PdfPage) -> bytes:
    bitmap = page.render(scale=_RENDER_SCALE)
    try:
        pil = bitmap.to_pil().convert("RGB")
        buffer = io.BytesIO()
        pil.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
        pil.close()
        return buffer.getvalue()
    finally:
        _ = bitmap.close()


# How many leading pages to sample when probing for a text layer. Bounded so a fully
# scanned (no-text) document — exactly the case that would otherwise scan every page —
# costs a few pages, not the whole file (MinerU has already parsed it; this only backs
# a flag). A doc whose first pages are scanned but body is digital is rare enough to
# accept the heuristic.
_TEXT_PROBE_PAGES = 8


def pdf_has_text_layer(raw: bytes) -> bool:
    """Whether a PDF appears to carry embedded text (vs. being image-only/scanned),
    sampling at most the first :data:`_TEXT_PROBE_PAGES` pages. A cheap probe a
    high-fidelity extractor uses to set its ``vision`` flag — which means the same thing
    across extractors: OCR/vision was involved in producing the text (true when the PDF
    has no text layer to read). Any read failure ⇒ assume no text layer."""
    try:
        pdf = pdfium.PdfDocument(raw)
    except pdfium.PdfiumError, ValueError:  # an unreadable PDF has no detectable text
        return False
    try:
        for index in range(min(len(pdf), _TEXT_PROBE_PAGES)):
            page = pdf[index]
            textpage = page.get_textpage()
            has = len((textpage.get_text_range() or "").strip()) >= _SPARSE_TEXT_THRESHOLD
            _ = textpage.close()
            _ = page.close()
            if has:
                return True
        return False
    finally:
        _ = pdf.close()


class FallbackExtractor:
    """Composes a primary extractor with a fallback: try the primary, drop to the
    fallback on **any** failure. Used to put the high-fidelity MinerU path in front of
    the always-available built-in, so a missing/broken/out-of-resources MinerU degrades
    to a working extraction rather than an error (the project's fail-soft posture)."""

    def __init__(self, primary: UploadExtractor, fallback: UploadExtractor) -> None:
        self._primary: UploadExtractor = primary
        self._fallback: UploadExtractor = fallback

    async def extract(
        self, owner_id: str, raw: bytes, mime: str, filename: str
    ) -> ExtractionResult:
        try:
            return await self._primary.extract(owner_id, raw, mime, filename)
        except Exception:  # any primary failure degrades to the built-in
            logger.warning(
                "primary extractor failed for %r; falling back to the built-in",
                filename,
                exc_info=True,
            )
            return await self._fallback.extract(owner_id, raw, mime, filename)
