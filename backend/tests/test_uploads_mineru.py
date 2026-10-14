"""The MinerU extractor + the FallbackExtractor composition + engine selection.

MinerU itself is never installed here (its model stack is heavy and detected, not
bundled), so the subprocess and the availability probe are stubbed — what's verified is
the parsing/labelling, the degrade-to-built-in behavior, and the selection logic."""

from __future__ import annotations

import tempfile
from pathlib import Path

from core.db import make_engine
from core.vault import Vault
from services.upload_extraction import (
    BasicExtractor,
    ExtractionResult,
    FallbackExtractor,
)
from services.upload_mineru import MinerUExtractor, _clean, _read_markdown

from .test_uploads_extraction import text_pdf

OWNER = "operator"


# --- MinerU extractor ------------------------------------------------------


async def test_unsupported_input_raises_for_fallback():
    extractor = MinerUExtractor()
    # A text file isn't MinerU's job — it raises so the fallback handles it.
    try:
        await extractor.extract(OWNER, b"plain text", "text/plain", "n.txt")
    except Exception:  # noqa: BLE001
        pass
    else:
        raise AssertionError("expected MinerU to reject a text file")


async def test_extract_parses_markdown_and_labels_mineru(monkeypatch):
    extractor = MinerUExtractor()

    async def fake_run(raw: bytes, ext: str) -> str:
        return "# Title\n\n![](images/fig.jpg)\nthe real body text"

    monkeypatch.setattr(extractor, "_run", fake_run)
    pdf = text_pdf("this document has a genuine embedded text layer")
    result = await extractor.extract(OWNER, pdf, "application/pdf", "d.pdf")
    assert result.extractor == "mineru"
    assert "the real body text" in result.text
    assert "![](" not in result.text  # standalone image lines stripped
    assert result.vision is False  # the PDF had a text layer


def test_is_available_reflects_path(monkeypatch):
    monkeypatch.setattr("services.upload_mineru.shutil.which", lambda _b: None)
    assert MinerUExtractor.is_available() is False
    monkeypatch.setattr("services.upload_mineru.shutil.which", lambda _b: "/usr/bin/mineru")
    assert MinerUExtractor.is_available() is True


def test_clean_strips_image_lines():
    assert _clean("a\n![](x.jpg)\nb") == "a\nb"


def test_read_markdown_picks_largest():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a").mkdir()
        (root / "a" / "small.md").write_text("hi")
        (root / "a" / "big.md").write_text("the bigger document body")
        assert _read_markdown(root) == "the bigger document body"


# --- FallbackExtractor -----------------------------------------------------


class _StubExtractor:
    def __init__(self, *, result: ExtractionResult | None = None, boom=False) -> None:
        self._result = result
        self._boom = boom
        self.calls = 0

    async def extract(self, owner_id, raw, mime, filename) -> ExtractionResult:
        self.calls += 1
        if self._boom:
            raise RuntimeError("primary failed")
        assert self._result is not None
        return self._result


async def test_fallback_prefers_primary():
    primary = _StubExtractor(result=ExtractionResult(text="hi", extractor="mineru"))
    fallback = _StubExtractor(result=ExtractionResult(text="meh", extractor="basic"))
    fb = FallbackExtractor(primary, fallback)
    out = await fb.extract(OWNER, b"x", "application/pdf", "d.pdf")
    assert out.extractor == "mineru" and fallback.calls == 0


async def test_fallback_degrades_on_primary_failure():
    primary = _StubExtractor(boom=True)
    fallback = _StubExtractor(result=ExtractionResult(text="recovered", extractor="basic"))
    fb = FallbackExtractor(primary, fallback)
    out = await fb.extract(OWNER, b"x", "application/pdf", "d.pdf")
    assert out.text == "recovered" and out.extractor == "basic" and fallback.calls == 1


# --- engine selection (_build_upload_extractor) ----------------------------


def _registry():
    from services.registry import ModelRegistry

    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    return ModelRegistry(make_engine("sqlite:///:memory:"), vault)


def test_selection_basic_pin(monkeypatch):
    from core.config import Settings
    from harness.manifests.uploads import _build_extractor as _build_upload_extractor

    monkeypatch.setattr(MinerUExtractor, "is_available", staticmethod(lambda binary="mineru": True))
    extractor = _build_upload_extractor(_registry(), Settings(upload_extractor="basic"))
    assert isinstance(extractor, BasicExtractor)


def test_selection_auto_follows_availability(monkeypatch):
    from core.config import Settings
    from harness.manifests.uploads import _build_extractor as _build_upload_extractor

    def _avail(value):
        monkeypatch.setattr(
            MinerUExtractor, "is_available", staticmethod(lambda binary="mineru": value)
        )

    _avail(False)
    auto = Settings(upload_extractor="auto")
    assert isinstance(_build_upload_extractor(_registry(), auto), BasicExtractor)
    _avail(True)
    assert isinstance(_build_upload_extractor(_registry(), auto), FallbackExtractor)
