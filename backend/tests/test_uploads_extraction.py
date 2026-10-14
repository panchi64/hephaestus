"""The upload extraction pipeline (UP-2): native PDF text, vision OCR, text files.

Also the home for the shared upload-test fakes and PDF fixtures (a real text-layer
PDF via fpdf2, an image-only/scanned PDF via Pillow), imported by the service/route
tests so they aren't redefined."""

from __future__ import annotations

import io
from typing import final, override

from fpdf import FPDF
from PIL import Image

from core.exceptions import DegradedCapabilityError
from services.upload_extraction import BasicExtractor, PreparedVisionOCR, VisionOCR

OWNER = "operator"


# --- shared fakes (the VisionOCR seam) ------------------------------------


@final
class _Prepared(PreparedVisionOCR):
    def __init__(self, text: str) -> None:
        self._text: str = text

    @override
    async def transcribe(self, image: bytes) -> str:
        return self._text


@final
class FakeVisionOCR(VisionOCR):
    """A vision model that always transcribes a page to the same canned text."""

    def __init__(self, text: str = "OCR-TEXT") -> None:
        self._text: str = text
        self.prepared: int = 0

    @override
    async def prepare(self, owner_id: str) -> _Prepared:
        self.prepared += 1
        return _Prepared(self._text)


@final
class NoVisionOCR(VisionOCR):
    """Stands in for an operator with no vision endpoint configured."""

    @override
    async def prepare(self, owner_id: str) -> _Prepared:
        raise DegradedCapabilityError("no vision-capable model endpoint configured")


@final
class FlakyVisionOCR(VisionOCR):
    """No vision until ``enabled`` is flipped — for the retry-after-config path."""

    def __init__(self) -> None:
        self.enabled: bool = False
        self._inner: FakeVisionOCR = FakeVisionOCR("OCR-TEXT")

    @override
    async def prepare(self, owner_id: str) -> _Prepared:
        if not self.enabled:
            raise DegradedCapabilityError("no vision-capable model endpoint configured")
        return await self._inner.prepare(owner_id)


# --- shared PDF / file fixtures -------------------------------------------


def text_pdf(text: str) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=14)
    _ = pdf.cell(0, 10, text)
    return bytes(pdf.output())


def multipage_text_pdf(*pages: str) -> bytes:
    pdf = FPDF()
    pdf.set_font("Helvetica", size=14)
    for text in pages:
        pdf.add_page()
        _ = pdf.cell(0, 10, text)
    return bytes(pdf.output())


def image_pdf() -> bytes:
    """An image-only (scanned-looking) PDF: one white page, no text layer."""
    buffer = io.BytesIO()
    Image.new("RGB", (300, 120), "white").save(buffer, format="PDF")
    return buffer.getvalue()


# --- tests -----------------------------------------------------------------


async def test_text_file_passes_through():
    extractor = BasicExtractor(NoVisionOCR())
    result = await extractor.extract(OWNER, b"plain notes here", "text/plain", "n.txt")
    assert result.text == "plain notes here"
    assert result.vision is False and result.note is None


async def test_native_pdf_text_is_extracted_without_vision():
    extractor = BasicExtractor(NoVisionOCR())
    result = await extractor.extract(
        OWNER, text_pdf("native marker zebra"), "application/pdf", "d.pdf"
    )
    assert "native marker zebra" in result.text
    assert result.vision is False and result.note is None


async def test_scanned_pdf_uses_vision_ocr():
    ocr = FakeVisionOCR("transcribed scan content")
    extractor = BasicExtractor(ocr)
    result = await extractor.extract(OWNER, image_pdf(), "application/pdf", "scan.pdf")
    assert result.text == "transcribed scan content"
    assert result.vision is True
    assert ocr.prepared == 1  # vision model resolved once for the document


async def test_scanned_pdf_without_vision_is_noted_not_failed():
    extractor = BasicExtractor(NoVisionOCR())
    result = await extractor.extract(OWNER, image_pdf(), "application/pdf", "scan.pdf")
    assert result.text == "" and result.vision is False
    assert result.note is not None and "vision model" in result.note


async def test_page_cap_is_reported_not_silent():
    extractor = BasicExtractor(NoVisionOCR(), max_pages=1)
    result = await extractor.extract(
        OWNER, multipage_text_pdf("page one alpha", "page two beta"), "application/pdf", "d.pdf"
    )
    assert "alpha" in result.text and "beta" not in result.text  # only page 1
    assert result.note is not None and "beyond" in result.note
