"""The hook a running host command reports its output through."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

#: Called with everything a running command has newly printed and how long it has been
#: running. One string per tick, stdout ahead of stderr, since nothing records how the two
#: files interleaved.
type ProgressHook = Callable[[str, float], Awaitable[None]]
