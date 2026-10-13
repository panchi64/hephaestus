"""High-fidelity upload extraction via MinerU — the detected-host-tool path.

MinerU turns a PDF (or image) into clean Markdown with layout, tables, and formulas
preserved — far better than the built-in text+OCR pass for complex documents. Its
model stack is heavy (PyTorch + multi-GB weights), so it is **detected, not bundled**:
the project venv never pulls it. It is a host capability discovered with
``shutil.which`` and shelled out to.

Crucially it runs as a **transient subprocess**: each extraction loads MinerU's models,
produces Markdown, and exits — so the models are lazy by construction and freed on exit,
with no warm server to budget against the operator's LLM. The trade is cold-start
latency per upload; a warm-server + resource-governor path is a deliberate later step.

This extractor only claims what MinerU handles (PDFs and images); anything else raises
so the composing :class:`~services.upload_extraction.FallbackExtractor` drops to the
built-in (which decodes text files). Any failure here — MinerU missing, a non-zero exit,
a timeout, no Markdown produced — propagates for the same reason: degrade, don't error.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path

from services.upload_extraction import (
    EXTRACTOR_MINERU,
    ExtractionResult,
    pdf_has_text_layer,
)

logger = logging.getLogger(__name__)

# Map a content type / name to the extension MinerU dispatches on. Only the formats
# MinerU actually parses; everything else is left to the built-in extractor.
_IMAGE_EXTS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}
# Lines that are nothing but an embedded-image reference — noise in retrievable text.
_IMAGE_LINE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")


class _Unsupported(Exception):
    """The input isn't a format MinerU handles — fall back to the built-in extractor."""


class MinerUExtractor:
    """Extract via the ``mineru`` CLI, run as a one-shot subprocess per document."""

    def __init__(self, *, timeout_s: float = 300.0, binary: str = "mineru") -> None:
        self._timeout_s = timeout_s
        self._binary = binary

    @staticmethod
    def is_available(binary: str = "mineru") -> bool:
        """Whether the ``mineru`` tool is on the host PATH (the detection gate)."""
        return shutil.which(binary) is not None

    async def extract(
        self, owner_id: str, raw: bytes, mime: str, filename: str
    ) -> ExtractionResult:
        ext = _input_ext(mime, filename)
        if ext is None:
            raise _Unsupported(f"MinerU does not handle {mime!r}")
        markdown = await self._run(raw, ext)
        text = _clean(markdown)
        # MinerU OCRs internally; a cheap text-layer probe tells us whether this doc
        # *needed* vision, which is what the upload's `vision` flag means.
        vision = ext != ".pdf" or not pdf_has_text_layer(raw)
        return ExtractionResult(text=text, vision=vision, extractor=EXTRACTOR_MINERU)

    async def _run(self, raw: bytes, ext: str) -> str:
        """Write the bytes to a temp file, run MinerU into a temp output dir, and read
        back the Markdown it produced. Raises on any failure (the fallback handles it)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            src = tmp_dir / f"input{ext}"
            src.write_bytes(raw)
            out_dir = tmp_dir / "out"
            out_dir.mkdir()
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                "-p",
                str(src),
                "-o",
                str(out_dir),
                "-b",
                "pipeline",
                "-m",
                "auto",
                # stdout is MinerU's progress chatter — discard it rather than buffer it
                # all in the parent; only stderr (the failure detail) is read.
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout_s
                )
            except TimeoutError:
                proc.kill()
                # Re-drain so the killed child's stderr pipe is consumed/closed rather
                # than left to GC; communicate() returns promptly once the process is dead.
                await proc.communicate()
                raise RuntimeError("MinerU timed out") from None
            except asyncio.CancelledError:
                # A shutdown (or a cancelled upload) is not a reason to leave a PyTorch
                # job running on the operator's machine: the child is ours to kill.
                if proc.returncode is None:
                    proc.kill()
                raise
            if proc.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace")[-500:]
                raise RuntimeError(f"MinerU exited {proc.returncode}: {detail}")
            return _read_markdown(out_dir)


def _input_ext(mime: str, filename: str) -> str | None:
    name = filename.lower()
    if mime == "application/pdf" or name.endswith(".pdf"):
        return ".pdf"
    if mime in _IMAGE_EXTS:
        return _IMAGE_EXTS[mime]
    for image_ext in (".png", ".jpg", ".jpeg", ".webp"):
        if name.endswith(image_ext):
            return ".jpg" if image_ext == ".jpeg" else image_ext
    return None


def _read_markdown(out_dir: Path) -> str:
    """The largest ``.md`` MinerU wrote under the output dir (it nests by stem/method)."""
    candidates = sorted(out_dir.rglob("*.md"), key=lambda p: p.stat().st_size, reverse=True)
    if not candidates:
        raise RuntimeError("MinerU produced no Markdown output")
    return candidates[0].read_text(encoding="utf-8", errors="replace")


def _clean(markdown: str) -> str:
    """Drop standalone image-embed lines (noise in retrievable text) and trim."""
    kept = [line for line in markdown.splitlines() if not _IMAGE_LINE.match(line)]
    return "\n".join(kept).strip()
