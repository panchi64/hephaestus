"""The shared voice module stays in step with ``docs/voice.md``."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest

from prompts.voice import CLASSES

_DOC = Path(__file__).resolve().parents[2] / "docs" / "voice.md"
_DICTIONARY = Path("/usr/share/dict/words")


def _folded(callsign: str) -> str:
    return unicodedata.normalize("NFKD", callsign).encode("ascii", "ignore").decode().lower()


def _all_callsigns() -> list[str]:
    return [callsign for spec in CLASSES.values() for callsign in spec.callsigns]


def test_the_class_table_matches_the_doc() -> None:
    text = _DOC.read_text(encoding="utf-8")
    section = text.split("## 2. The classes", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^\| \*\*(.+?)\*\* \|", section, flags=re.MULTILINE))
    assert documented == {spec.display for spec in CLASSES.values()}


def test_callsigns_are_four_letters_and_unique() -> None:
    callsigns = _all_callsigns()
    assert all(len(c) == 4 and c.isupper() and c.isalpha() for c in callsigns)
    assert len(set(map(_folded, callsigns))) == len(callsigns)


def test_kourai_callsigns_end_in_long_e() -> None:
    assert all(c.endswith("Ē") for c in CLASSES["kourai"].callsigns)


def test_only_kourai_callsigns_carry_a_macron() -> None:
    others = [c for key, spec in CLASSES.items() if key != "kourai" for c in spec.callsigns]
    assert all(c.isascii() for c in others)


@pytest.mark.skipif(not _DICTIONARY.exists(), reason="no system word list")
def test_no_callsign_is_an_english_word() -> None:
    words = {line.strip().lower() for line in _DICTIONARY.read_text(encoding="utf-8").splitlines()}
    assert [c for c in _all_callsigns() if _folded(c) in words] == []
