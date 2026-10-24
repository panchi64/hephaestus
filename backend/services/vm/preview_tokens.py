"""Which token names which conversation's preview, and why a dead one died.

The proxy route and the live-status route both arrive holding nothing but an opaque
token, and both need an answer that outlives the thing it refers to: a preview torn down
by the idle reaper has to read as *stopped* rather than as *never existed*, or the
frontend cannot tell "the machine went idle and killed your server" from an iframe that
is merely still loading.

So this keeps two maps rather than one. The live index is authoritative only while the
session is; the tombstones are what remains afterwards, and they are pruned by age
because a token nobody has asked about in an hour is one nobody is going to. Explicit
closes are deliberately *not* tombstoned — the close already emits its own event, and a
client that asked for the teardown does not need to be told it happened twice.

Bookkeeping only: nothing here starts, stops, or touches the VM. The manager owns
that, and calls in here to record what it did.
"""

from __future__ import annotations

import time

#: How long a tombstone is worth keeping. Long enough that an operator who leaves a tab
#: open and comes back still learns what happened to it, short enough that the map is
#: bounded by the sessions of one sitting.
STOPPED_TTL_S = 3600.0


class PreviewTokens:
    """The token → session index plus the tombstones of recently killed previews.

    Deliberately synchronous and lock-free: every method is a plain dict operation, and
    the manager calls them from inside its own lock where they need to be atomic against
    the reaper. A second lock here would buy nothing and could only deadlock against that
    one.
    """

    def __init__(self) -> None:
        #: token → safe session key, so the proxy route resolves a preview in O(1).
        self._live: dict[str, str] = {}
        #: token → monotonic time it was torn down *without* an explicit close.
        self._stopped: dict[str, float] = {}

    def index(self, token: str, safe: str) -> None:
        """Record ``token`` as this session's current preview."""
        self._live[token] = safe

    def owner(self, token: str) -> str | None:
        """The safe session key this token belongs to, or ``None`` if unknown."""
        return self._live.get(token)

    def drop(self, safe: str) -> None:
        """Forget every token pointing at this session — one preview per conversation,
        so starting a new one retires the old token as surely as stopping it does."""
        self._live = {t: k for t, k in self._live.items() if k != safe}

    def tombstone(self, token: str) -> None:
        """Mark a token as stopped-without-a-signal, and prune tombstones that have
        aged out. Called *before* the preview it names is torn down."""
        now = time.monotonic()
        cutoff = now - STOPPED_TTL_S
        self._stopped = {t: ts for t, ts in self._stopped.items() if ts > cutoff}
        self._stopped[token] = now

    def was_stopped(self, token: str) -> bool:
        """Whether this token names a preview we tore down rather than one we never saw."""
        return token in self._stopped

    def clear(self) -> None:
        """Drop the live index. Tombstones are left alone: the process is going away, and
        on the way down a status check is still better answered than not."""
        self._live.clear()
