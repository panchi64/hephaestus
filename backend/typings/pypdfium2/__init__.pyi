from pathlib import Path
from typing import BinaryIO

from PIL.Image import Image

class PdfiumError(RuntimeError): ...

class _AutoCloseable:
    def close(self, _by_parent: bool = False) -> bool: ...

class PdfBitmap(_AutoCloseable):
    def to_pil(self) -> Image: ...

class PdfTextPage(_AutoCloseable):
    def get_text_range(self, index: int = 0, count: int = -1, errors: str = "ignore") -> str: ...

class PdfPage(_AutoCloseable):
    def get_textpage(self) -> PdfTextPage: ...
    def render(
        self,
        scale: float = 1,
        rotation: int = 0,
        crop: tuple[float, float, float, float] = (0, 0, 0, 0),
        may_draw_forms: bool = True,
        fill_to_stroke: bool = False,
    ) -> PdfBitmap: ...

class PdfDocument(_AutoCloseable):
    def __init__(
        self,
        input: str | Path | bytes | BinaryIO,
        password: str | None = None,
        autoclose: bool = False,
    ) -> None: ...
    def __len__(self) -> int: ...
    def __getitem__(self, i: int) -> PdfPage: ...
