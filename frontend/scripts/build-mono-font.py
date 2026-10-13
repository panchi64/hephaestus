#!/usr/bin/env python3
"""Build the self-hosted JetBrains Mono woff2 files in src/ui/theme/fonts/.

Provenance: JetBrains Mono patched by Nerd Fonts (pinned release below),
subset to the unicode ranges the UI actually uses plus the Braille Patterns
block (U+2800-28FF) so the braille throbber (src/ui/components/Frames.tsx)
renders natively without a system-font install. The family is renamed to
"JetBrains Mono" so the result is a drop-in for the existing font stacks.

Usage:
    python3 scripts/build-mono-font.py [path/to/JetBrainsMono.tar.xz]

Without an argument the pinned release archive is downloaded.
Requires: python3 with fontTools and brotli (`pip install fonttools brotli`).
"""

import logging
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from fontTools import subset as ftsubset
from fontTools.ttLib import TTFont


class _PfEdFilter(logging.Filter):
    # Expected: the proportional-figure feature is dropped on subset; it is a
    # no-op in a monospace face (all advances are already equal).
    def filter(self, record):
        return "PfEd" not in record.getMessage()


logging.getLogger("fontTools.subset").addFilter(_PfEdFilter())

RELEASE = "v3.5.0"
URL = (
    "https://github.com/ryanoasis/nerd-fonts/releases/download/"
    f"{RELEASE}/JetBrainsMono.tar.xz"
)
FAMILY = "JetBrains Mono"
# (Nerd Font instance, CSS weight)
WEIGHTS = [("Regular", 400), ("Medium", 500), ("Bold", 700)]
# The six @fontsource/JetBrains Mono subset ranges (latin, latin-ext, cyrillic,
# cyrillic-ext, greek, vietnamese) plus the Braille Patterns block.
RANGES = [
    # latin
    (0x0000, 0x00FF), (0x0131, 0x0131), (0x0152, 0x0153), (0x02BB, 0x02BC),
    (0x02C6, 0x02C6), (0x02DA, 0x02DA), (0x02DC, 0x02DC), (0x0304, 0x0304),
    (0x0308, 0x0308), (0x0329, 0x0329), (0x2000, 0x206F), (0x20AC, 0x20AC),
    (0x2122, 0x2122), (0x2191, 0x2191), (0x2193, 0x2193), (0x2212, 0x2212),
    (0x2215, 0x2215), (0xFEFF, 0xFEFF), (0xFFFD, 0xFFFD),
    # latin-ext
    (0x0100, 0x02BA), (0x02BD, 0x02C5), (0x02C7, 0x02CC), (0x02CE, 0x02D7),
    (0x02DD, 0x02FF), (0x0304, 0x0304), (0x0308, 0x0308), (0x0329, 0x0329),
    (0x1D00, 0x1DBF), (0x1E00, 0x1E9F), (0x1EF2, 0x1EFF), (0x2020, 0x2020),
    (0x20A0, 0x20AB), (0x20AD, 0x20C0), (0x2113, 0x2113), (0x2C60, 0x2C7F),
    (0xA720, 0xA7FF),
    # cyrillic
    (0x0301, 0x0301), (0x0400, 0x045F), (0x0490, 0x0491), (0x04B0, 0x04B1),
    (0x2116, 0x2116),
    # cyrillic-ext
    (0x0460, 0x052F), (0x1C80, 0x1C8A), (0x20B4, 0x20B4), (0x2DE0, 0x2DFF),
    (0xA640, 0xA69F), (0xFE2E, 0xFE2F),
    # greek
    (0x0370, 0x0377), (0x037A, 0x037F), (0x0384, 0x038A), (0x038C, 0x038C),
    (0x038E, 0x03A1), (0x03A3, 0x03FF),
    # vietnamese
    (0x0102, 0x0103), (0x0110, 0x0111), (0x0128, 0x0129), (0x0168, 0x0169),
    (0x01A0, 0x01A1), (0x01AF, 0x01B0), (0x0300, 0x0301), (0x0303, 0x0304),
    (0x0308, 0x0309), (0x0323, 0x0323), (0x0329, 0x0329), (0x1EA0, 0x1EF9),
    (0x20AB, 0x20AB),
    # braille patterns
    (0x2800, 0x28FF),
]

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE.parent / "src" / "ui" / "theme" / "fonts"


def download(url: str) -> Path:
    tmp = Path(tempfile.mkstemp(suffix=".tar.xz")[1])
    req = urllib.request.Request(url, headers={"User-Agent": "hephaestus-font-build"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    return tmp


def extract(archive: Path, members: list[str]) -> dict[str, Path]:
    tmpdir = Path(tempfile.mkdtemp(prefix="jbmono-"))
    with tarfile.open(archive, "r:xz") as tf:
        for m in members:
            tf.extract(m, tmpdir, filter="data")
    return {m: tmpdir / m for m in members}


def rename_family(font: TTFont, style: str) -> None:
    name = font["name"]
    for name_id, value in (
        (1, FAMILY),
        (2, style),
        (4, f"{FAMILY} {style}"),
        (16, FAMILY),
        (17, style),
    ):
        name.names = [r for r in name.names if r.nameID != name_id]
        name.setName(value, name_id, 3, 1, 0x409)


def build_weight(ttf: Path, weight: int, style: str, out: Path) -> None:
    font = TTFont(str(ttf))
    subsetter = ftsubset.Subsetter()
    subsetter.populate(unicodes=[c for lo, hi in RANGES for c in range(lo, hi + 1)])
    subsetter.subset(font)
    rename_family(font, style)
    font.flavor = "woff2"
    font.save(str(out))

    check = TTFont(str(out), lazy=True)
    cmap = check.getBestCmap()
    missing = [c for c in range(0x2800, 0x2900) if c not in cmap]
    family = check["name"].getDebugName(1)
    check.close()
    if missing:
        sys.exit(f"{out.name}: missing {len(missing)}/256 braille glyphs")
    if family != FAMILY:
        sys.exit(f"{out.name}: unexpected family {family!r}")
    print(f"  {out.name}: {out.stat().st_size / 1024:.1f} KiB, "
          f"{len(cmap)} glyphs, braille 256/256")


def main() -> None:
    members = [f"JetBrainsMonoNerdFontMono-{style}.ttf" for style, _ in WEIGHTS] + ["OFL.txt"]
    if len(sys.argv) > 1:
        archive = Path(sys.argv[1])
        if not archive.is_file():
            sys.exit(f"archive not found: {archive}")
    else:
        print(f"Downloading {URL}")
        archive = download(URL)

    files = extract(archive, members)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(files["OFL.txt"], OUT_DIR / "OFL.txt")
    print(f"Writing to {OUT_DIR}")
    for style, weight in WEIGHTS:
        build_weight(files[f"JetBrainsMonoNerdFontMono-{style}.ttf"], weight, style,
                     OUT_DIR / f"jetbrains-mono-{weight}.woff2")
    print("Done.")


if __name__ == "__main__":
    main()
