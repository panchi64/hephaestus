"""A project's name as a path segment: what follows `~/projects/` in the agent's home."""

from __future__ import annotations

import re
from collections.abc import Collection

#: What the guest accepts after `projects/` (`vm/guest/confine.py`): a leading letter or
#: digit, then letters, digits, `.`, `_` or `-`.
_KEEP = re.compile(r"[^a-z0-9._-]+")
_MAX = 40


def slug_for(name: str, taken: Collection[str]) -> str:
    """``name`` lowercased to path-safe characters, numbered ``-2``, ``-3`` past a clash."""
    base = _KEEP.sub("-", name.strip().lower()).strip("-._")[:_MAX].rstrip("-._") or "project"
    slug, n = base, 2
    while slug in taken:
        suffix = f"-{n}"
        slug = base[: _MAX - len(suffix)] + suffix
        n += 1
    return slug
