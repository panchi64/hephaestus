"""The shape of the sandbox package itself: modules with one reason to change each,
and one public surface everything outside the package binds to.

Both halves are regressions that only ever creep back. A size ceiling is the cheapest
thing that notices a module absorbing a second job. And the names below are what every
importer outside ``services.sandbox`` uses — moving a class between modules must stay
invisible to them.
"""

from __future__ import annotations

from pathlib import Path

import services.sandbox as sandbox_pkg

# Generous rather than tight: this is a ceiling that catches a module quietly absorbing
# a second job, not a style rule about the length any of them happens to be today.
_MAX_LINES = 700


def test_each_sandbox_module_stays_one_reason_to_change():
    package = Path(sandbox_pkg.__file__).parent
    too_long = {
        path.name: lines
        for path in package.glob("*.py")
        if (lines := len(path.read_text().splitlines())) > _MAX_LINES
    }
    assert not too_long, f"over {_MAX_LINES} lines: {too_long}"


def test_the_package_still_exposes_the_names_importers_bind_to():
    for name in ("SandboxError", "SandboxResult", "SandboxSpec", "stage_attachment", "run_on_host"):
        assert hasattr(sandbox_pkg, name)
        assert name in sandbox_pkg.__all__
