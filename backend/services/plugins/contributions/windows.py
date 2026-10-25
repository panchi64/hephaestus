"""``windows``: small apps a plugin shows over the app, served from its ``ui/`` folder.

```yaml
windows:
  - id: main
    title: Snake
    entry: ui/index.html
    size: {width: 360, height: 420}
    min_size: {width: 240, height: 280}   # optional
    resizable: true                       # optional
```

A window's page runs in a sandboxed frame with no network, so the files under ``ui/`` are
everything it can load. They are checked here: only the listed asset types, text that is
UTF-8, and images whose bytes are what their extension says.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import override

from core.exceptions import PluginValidationError

from ..fields import as_mapping, table_list, text
from .base import BundleSource, Described, TypedContribution
from .registry import CONTRIBUTIONS

UI_FOLDER = "ui/"

#: Extension → content type, for every file a window may load.
CONTENT_TYPES: Mapping[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
}
_TEXT = frozenset({".html", ".js", ".mjs", ".css", ".json", ".svg", ".txt"})
_MAGIC: Mapping[str, tuple[bytes, ...]] = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
    ".woff2": (b"wOF2",),
    ".mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
    ".wav": (b"RIFF",),
    ".ogg": (b"OggS",),
}
_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MIN_SIDE = 120
_MAX_SIDE = 2000


@dataclass(frozen=True)
class PluginWindow:
    id: str
    title: str
    #: The page it opens, a path under ``ui/``.
    entry: str
    width: int
    height: int
    min_width: int
    min_height: int
    resizable: bool = True


def content_type(path: str) -> str | None:
    """What a window file is served as; None for a file a window may not load."""
    _, dot, ext = path.rpartition(".")
    return CONTENT_TYPES.get(f".{ext.lower()}") if dot else None


def check_assets(files: Mapping[str, bytes]) -> None:
    """Raise unless every file under ``ui/`` is one a window may load, and is what its
    extension claims."""
    for path, blob in sorted(files.items()):
        if not path.startswith(UI_FOLDER):
            continue
        name = path.removeprefix(UI_FOLDER)
        if any(part.startswith("__") for part in name.split("/")):
            raise PluginValidationError("windows", f"{path}: names starting `__` are reserved")
        if content_type(path) is None:
            allowed = ", ".join(sorted(CONTENT_TYPES))
            raise PluginValidationError("windows", f"{path}: a window may load only {allowed}")
        ext = path[path.rfind(".") :].lower()
        if ext in _TEXT:
            try:
                _ = blob.decode()
            except UnicodeDecodeError:
                raise PluginValidationError("windows", f"{path} is not UTF-8 text") from None
        elif not blob.startswith(_MAGIC[ext]):
            raise PluginValidationError("windows", f"{path} is not a {ext[1:]} file")


class Windows(TypedContribution[PluginWindow]):
    @override
    def parse(self, source: BundleSource) -> tuple[PluginWindow, ...]:
        if source.declared and source.files is not None:
            check_assets(source.files)
        windows = tuple(_window(raw, source.files) for raw in table_list(source.raw, self.key))
        if len({window.id for window in windows}) != len(windows):
            raise PluginValidationError(self.key, "two windows share an id")
        return windows

    @override
    def describe_one(self, item: PluginWindow) -> Described:
        return {"name": item.id, "title": item.title}


def _window(raw: Mapping[str, object], files: Mapping[str, bytes] | None) -> PluginWindow:
    unknown = set(raw) - {"id", "title", "entry", "size", "min_size", "resizable"}
    if unknown:
        raise PluginValidationError("windows", f"unknown window key {sorted(unknown)[0]!r}")
    window_id = str(raw.get("id", "main"))
    if not _ID.fullmatch(window_id):
        raise PluginValidationError("windows", f"{window_id!r} is not a window id")
    entry = text(raw, "entry", field="windows")
    if not entry.startswith(UI_FOLDER) or not entry.endswith(".html"):
        raise PluginValidationError("windows", f"{window_id}: entry is an .html file under ui/")
    if files is not None and entry not in files:
        raise PluginValidationError("windows", f"{window_id}: there is no {entry}")
    width, height = _size(raw.get("size"), window_id, "size")
    min_width, min_height = (
        _size(raw["min_size"], window_id, "min_size")
        if "min_size" in raw
        else (min(width, 240), min(height, 200))
    )
    if min_width > width or min_height > height:
        raise PluginValidationError("windows", f"{window_id}: min_size is larger than size")
    resizable = raw.get("resizable", True)
    if not isinstance(resizable, bool):
        raise PluginValidationError("windows", f"{window_id}: resizable is true or false")
    return PluginWindow(
        id=window_id,
        title=text(raw, "title", field="windows"),
        entry=entry,
        width=width,
        height=height,
        min_width=min_width,
        min_height=min_height,
        resizable=resizable,
    )


def _size(raw: object, window_id: str, key: str) -> tuple[int, int]:
    table = as_mapping(raw)
    if table is None:
        raise PluginValidationError("windows", f"{window_id}: {key} is {{width, height}}")
    sides: list[int] = []
    for side in ("width", "height"):
        value = table.get(side)
        if not isinstance(value, int) or isinstance(value, bool):
            raise PluginValidationError("windows", f"{window_id}: {key}.{side} is a number")
        if not _MIN_SIDE <= value <= _MAX_SIDE:
            raise PluginValidationError(
                "windows", f"{window_id}: {key}.{side} is {_MIN_SIDE} to {_MAX_SIDE} pixels"
            )
        sides.append(value)
    return sides[0], sides[1]


WINDOWS = CONTRIBUTIONS.register(Windows("windows", "Windows", PluginWindow, trust="inert"))
