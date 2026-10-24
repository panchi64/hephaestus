"""How a command that could not reach the network reports it."""

from __future__ import annotations

import re

#: Node's DNS failure code. A whole word only: as a substring it sits inside
#: ``FileNotFoundError`` and ``ModuleNotFoundError``.
_ENOTFOUND = re.compile(r"\benotfound\b")

#: Lowercase phrases a client prints when its connection never got as far as the gateway, or
#: when the gateway refused a CONNECT. A refused HTTPS tunnel carries no body, so it is legible
#: only in these phrasings: urllib, urllib3 and pip say the first, curl one of the other two.
#: pip's own "no matching distribution" summary is not here: a misspelled package name prints
#: it against a registry that was reached perfectly well.
NO_NETWORK_SIGNS = (
    "temporary failure in name resolution",
    "name or service not known",
    "could not resolve host",
    "network is unreachable",
    "no address associated with hostname",
    "failed to lookup address",
    "failed to resolve address",
    "no such host",
    "unknownhostexception",
    "eai_again",
    "failed to establish a new connection",
    "tunnel connection failed",
    "connect tunnel failed",
    "from proxy after connect",
)


def mentions_no_network(text: str) -> bool:
    """Whether ``text`` reads as a command that could not reach the network."""
    low = text.lower()
    return any(sign in low for sign in NO_NETWORK_SIGNS) or _ENOTFOUND.search(low) is not None
