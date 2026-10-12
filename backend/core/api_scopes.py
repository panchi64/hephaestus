"""The scope catalog for inbound API tokens (`AUTH-4`).

A scope is a **named group of API surfaces**, not a permission bit: the operator issuing a
token picks the capabilities it may reach ("chat", "knowledge"), and the gate resolves the
request path back to the scope that owns it. Path prefixes are the grain because the HTTP
surface *is* the capability boundary here — each router owns exactly one feature — so the
mapping needs no per-route annotation and can't silently drift as routes are added within a
surface.

Core owns the **vocabulary** (`SCOPE_DEFS` — the operator-facing groups) and the
**machinery** (`ScopeTable`); the *claims* mapping prefixes into a scope arrive at app
assembly — `CORE_CLAIMS` for the surfaces core itself wires, plus each feature manifest's
own claims — so core never has to know which features exist.

Three rules make it safe:

* **Longest prefix wins.** A narrow claim (``/models/roles``) resolves ahead of the broader
  one it sits inside (``/models``), so a sub-surface can be granted — or withheld — on its
  own without restating the parent.
* **A claim may narrow to read methods.** Where one prefix covers both reading a surface
  and reconfiguring it, the claim says which methods it means, so a scope described to the
  operator as read-only cannot be spent on a write.
* **Deny by default.** A path no claim covers is unreachable with a token, whatever scopes
  it carries. That is deliberate for the surfaces that claim nothing — ``/auth``,
  ``/setup``, ``/tokens``, ``/credentials``, ``/vault``, ``/backup``, ``/shell``, mail,
  calendar, external tools — so a token can never mint another token, read the operator's
  secrets, or reach the host. Widening a token's reach is an explicit claim, an act,
  rather than a side effect of adding a router.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ScopeDef:
    """One grantable capability group, as the operator sees it."""

    id: str
    label: str
    description: str


# The HTTP methods that only read. A claim restricted to these turns its prefixes into a
# read-only reach: a write to the same path matches no claim and is therefore denied by
# default, exactly as an unclaimed surface is.
READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class ScopeClaim:
    """Path prefixes claimed into a scope — contributed by core assembly and by
    feature manifests. The scope id must name a `SCOPE_DEFS` entry.

    ``methods`` narrows the claim to those HTTP methods; ``None`` (the default) claims
    every method. It exists because a prefix is not always uniformly sensitive: the whole
    model registry is worth reading under one grant, while creating an endpoint and
    rebinding a role — same prefix — is the power to point every future turn at someone
    else's inference server. A scope the operator is told is read-only has to *be* one.
    """

    scope_id: str
    prefixes: tuple[str, ...]
    methods: frozenset[str] | None = None

    def covers(self, method: str) -> bool:
        return self.methods is None or method.upper() in self.methods


@dataclass(frozen=True)
class ApiScope:
    """A scope with its merged prefixes — the shape the token-issuing surface lists."""

    id: str
    label: str
    description: str
    prefixes: tuple[str, ...]


SCOPE_DEFS: tuple[ScopeDef, ...] = (
    ScopeDef("chat", "Chat", "Start conversations, stream runs, and read the View."),
    ScopeDef("memory", "Memory", "Read and write the assistant's long-term memory."),
    ScopeDef(
        "knowledge",
        "Knowledge",
        "Documents, uploads, images, skills, and the retrieval corpus.",
    ),
    # Claims no prefix and grants no reach: research happens in ordinary chats. Kept in
    # the vocabulary because tokens the operator already issued carry it, and an id no
    # scope table knows fails validation and would break a working token.
    ScopeDef(
        "research",
        "Research",
        "Retired. Research happens in ordinary chats; this grants no reach.",
    ),
    ScopeDef("search", "Search", "Query the web through the managed search provider."),
    ScopeDef(
        "tasks",
        "Tasks & notifications",
        "Scheduled tasks, their run history, and the notification stream.",
    ),
    ScopeDef("models", "Models", "Read the model registry, endpoints, and role bindings."),
    ScopeDef("status", "Status", "Read-only system status and the home overview."),
    ScopeDef(
        "projects",
        "Projects",
        "List the operator's working directories and switch the active one.",
    ),
)

# The surfaces the core assembly itself wires (see `app.py`); every feature surface
# claims through its own manifest.
CORE_CLAIMS: tuple[ScopeClaim, ...] = (
    ScopeClaim("chat", ("/chat", "/conversations", "/runs")),
    # Read-only, as the scope's own description promises. Writes under `/models` are not
    # a lesser version of reading it: `POST /models/endpoints` plus `PUT /models/roles/main`
    # is enough to route every future turn — prompts, history, recalled memories — through
    # an endpoint of the token holder's choosing. Those stay unreachable with a token,
    # like `/vault` and `/tokens`.
    ScopeClaim("models", ("/models",), methods=READ_METHODS),
    ScopeClaim("status", ("/overview",)),
)

_DEFS_BY_ID = {d.id: d for d in SCOPE_DEFS}


class ScopeTable:
    """The assembled path→scope mapping for one app instance."""

    def __init__(self, claims: Iterable[ScopeClaim]) -> None:
        merged: dict[str, list[str]] = {}
        entries: list[tuple[str, str, frozenset[str] | None]] = []
        for claim in claims:
            if claim.scope_id not in _DEFS_BY_ID:
                raise ValueError(f"scope claim names unknown scope id {claim.scope_id!r}")
            merged.setdefault(claim.scope_id, []).extend(claim.prefixes)
            entries.extend((prefix, claim.scope_id, claim.methods) for prefix in claim.prefixes)
        # Catalog order follows SCOPE_DEFS; a scope nothing claims is simply absent —
        # never offered on a token it could not put to use.
        self.scopes: tuple[ApiScope, ...] = tuple(
            ApiScope(d.id, d.label, d.description, tuple(merged[d.id]))
            for d in SCOPE_DEFS
            if d.id in merged
        )
        self.scope_ids: frozenset[str] = frozenset(merged)
        # Longest prefix first, so the first match is the most specific one.
        self._by_specificity: tuple[tuple[str, str, frozenset[str] | None], ...] = tuple(
            sorted(entries, key=lambda entry: len(entry[0]), reverse=True)
        )

    def scope_for_path(self, path: str, method: str = "GET") -> str | None:
        """The scope a request path+method belongs to, or ``None`` when no claim covers it.

        ``None`` means *unreachable with a token* — the deny-by-default half of the rule.
        A method-restricted claim is skipped rather than matched, so a write under a
        read-only prefix falls through to the shorter claims and, finding none, is denied.
        """
        for prefix, scope_id, methods in self._by_specificity:
            if path != prefix and not path.startswith(prefix + "/"):
                continue
            if methods is not None and method.upper() not in methods:
                continue
            return scope_id
        return None

    def unknown_scopes(self, scopes: list[str]) -> list[str]:
        """The requested scope ids that aren't in the table — an issue-time validation."""
        return [s for s in scopes if s not in self.scope_ids]
