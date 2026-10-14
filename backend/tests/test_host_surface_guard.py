"""Regression guard for the host surface: it is **agent-unreachable by construction**,
the same invariant the Operator Shell carries. Nothing under `tools/` or `agent/` may
reference `services/host_picker.py` or `services/host_open.py` — a model that could open
dialogs on the operator's desktop (and read back whatever path came out), or launch an
application on a path it chose, would be acting on the host outside every
sensitive-action approval gate.

Both halves are guarded together because they are one posture, and because the second is
the easier mistake to make: opening a file *looks* like a helpful thing to hand a model.

The needles are matched as substrings and ``host_open`` is a prefix, so the opener's
type-policy module (``services/host_open_policy.py``) is covered by the same entry — an
import of it names ``host_open`` too. Anything split out of these files must keep that
prefix, or earn its own needle here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: ``projects.listing`` is the third: it enumerates the operator's own checkout for the
#: composer's `@` picker, which is the same reach as the file dialog and belongs to the
#: operator alone. The agent already walks this filesystem through ``files_list_directory``
#: and ``files_search_files``, rooted at the workspace *its run* resolved — a second path
#: with the project root in its hand would be a way around that rooting.
#:
#: The containment check those references are resolved against is deliberately **not**
#: here: it lives in ``services/sandbox/base`` with the rest of the containment, is what
#: the file tools already use, and answering "does this path stay inside this root" is the
#: opposite of a way out of one.
_FORBIDDEN = ("host_picker", "PickerAvailability", "host_open", "projects.listing")
_SCAN_DIRS = ("tools", "agent")


def _source_files() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    return [path for dirname in _SCAN_DIRS for path in (root / dirname).rglob("*.py")]


def test_scan_dirs_contain_source_files() -> None:
    # A parametrize over a possibly-empty rglob list collects zero cases and pytest
    # reports the parametrized test below as SKIPPED, not FAILED — so a renamed/missing
    # scan dir would silently drop the guard instead of failing loudly.
    assert _source_files(), (
        f"expected source files under {_SCAN_DIRS} to scan — the host-surface guard "
        "would otherwise pass vacuously"
    )


@pytest.mark.parametrize("path", _source_files(), ids=str)
def test_agent_reachable_code_never_references_the_picker(path: Path) -> None:
    text = path.read_text()
    for needle in _FORBIDDEN:
        assert needle not in text, (
            f"{path} references {needle!r} — the host surface must stay "
            "agent-unreachable by construction"
        )
