"""The MinerU extractor + the FallbackExtractor composition + engine selection.

MinerU itself is never installed here (its model stack is heavy and detected, not
bundled), so the subprocess and the availability probe are stubbed — what's verified is
the parsing/labelling, the degrade-to-built-in behavior, and the selection logic."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import final, override

import pytest

from core.config import Settings
from core.db import make_engine
from core.vault import Vault
from harness.manifests.uploads import build_extractor as build_upload_extractor
from services.registry import ModelRegistry
from services.upload_extraction import (
    BasicExtractor,
    ExtractionResult,
    FallbackExtractor,
    UploadExtractor,
)
from services.upload_mineru import MinerUExtractor, clean_markdown, read_markdown

from .test_uploads_extraction import text_pdf

OWNER = "operator"


# --- MinerU extractor ------------------------------------------------------


async def test_unsupported_input_raises_for_fallback():
    extractor = MinerUExtractor()
    # A text file isn't MinerU's job — it raises so the fallback handles it.
    with pytest.raises(Exception, match="does not handle"):
        _ = await extractor.extract(OWNER, b"plain text", "text/plain", "n.txt")


async def test_extract_parses_markdown_and_labels_mineru(monkeypatch: pytest.MonkeyPatch):
    extractor = MinerUExtractor()

    async def fake_run(_raw: bytes, _ext: str) -> str:
        return "# Title\n\n![](images/fig.jpg)\nthe real body text"

    monkeypatch.setattr(extractor, "_run", fake_run)
    pdf = text_pdf("this document has a genuine embedded text layer")
    result = await extractor.extract(OWNER, pdf, "application/pdf", "d.pdf")
    assert result.extractor == "mineru"
    assert "the real body text" in result.text
    assert "![](" not in result.text  # standalone image lines stripped
    assert result.vision is False  # the PDF had a text layer


def test_is_available_reflects_path(monkeypatch: pytest.MonkeyPatch):
    def which_nothing(_binary: str) -> None:
        return None

    def which_mineru(_binary: str) -> str:
        return "/usr/bin/mineru"

    monkeypatch.setattr("services.upload_mineru.shutil.which", which_nothing)
    assert MinerUExtractor.is_available() is False
    monkeypatch.setattr("services.upload_mineru.shutil.which", which_mineru)
    assert MinerUExtractor.is_available() is True


def test_clean_strips_image_lines():
    assert clean_markdown("a\n![](x.jpg)\nb") == "a\nb"


def test_read_markdown_picks_largest():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a").mkdir()
        _ = (root / "a" / "small.md").write_text("hi")
        _ = (root / "a" / "big.md").write_text("the bigger document body")
        assert read_markdown(root) == "the bigger document body"


# --- FallbackExtractor -----------------------------------------------------


@final
class _StubExtractor(UploadExtractor):
    def __init__(self, *, result: ExtractionResult | None = None, boom: bool = False) -> None:
        self._result: ExtractionResult | None = result
        self._boom: bool = boom
        self.calls: int = 0

    @override
    async def extract(
        self, owner_id: str, raw: bytes, mime: str, filename: str
    ) -> ExtractionResult:
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


# --- engine selection (build_upload_extractor) ----------------------------


def _registry() -> ModelRegistry:
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    return ModelRegistry(make_engine("sqlite:///:memory:"), vault)


def _availability(value: bool) -> Callable[[str], bool]:
    def is_available(_binary: str = "mineru") -> bool:
        return value

    return is_available


def test_selection_basic_pin(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(MinerUExtractor, "is_available", staticmethod(_availability(True)))
    extractor = build_upload_extractor(_registry(), Settings(upload_extractor="basic"))
    assert isinstance(extractor, BasicExtractor)


def test_selection_auto_follows_availability(monkeypatch: pytest.MonkeyPatch):
    def _avail(value: bool) -> None:
        monkeypatch.setattr(MinerUExtractor, "is_available", staticmethod(_availability(value)))

    _avail(False)
    auto = Settings(upload_extractor="auto")
    assert isinstance(build_upload_extractor(_registry(), auto), BasicExtractor)
    _avail(True)
    assert isinstance(build_upload_extractor(_registry(), auto), FallbackExtractor)
