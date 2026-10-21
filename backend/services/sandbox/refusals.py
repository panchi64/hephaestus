"""How a command that a fence stopped is reported, for the VM's fence and the host's.

Both execution tools classify a finished command here, so a refusal reads the same
whichever tool ran it. The result is a note for the model and, for a VM egress refusal that
names its host, the host itself, which the command card offers to allow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from services.vm import DENIED_MARKER

from .floor import is_protected_write
from .network_errors import mentions_no_network


@dataclass(frozen=True, slots=True)
class Refusal:
    """A fence's refusal: what the model should do about it, and the denied host when the
    output names one."""

    note: str
    denied_host: str | None = None


#: How a client reports the gateway's 407 to a request that carried no credentials.
_PROXY_REFUSED_SIGNS = ("Proxy Authentication Required", "response 407", "hephaestus-egress")

#: A refused HTTPS tunnel carries no body, so it is legible only in the client's own
#: phrasing of the gateway's 403.
_TUNNEL_DENIED_SIGNS = (
    "tunnel connection failed: 403",
    "connect tunnel failed, response 403",
    "http code 403 from proxy after connect",
)

_DENIED_BODY_HOST = re.compile(re.escape(DENIED_MARKER) + r"\s+([^\s]+)", re.IGNORECASE)

#: The places a client names the host it was reaching: a URL, urllib3's pool, curl's resolver.
_HOST_CANDIDATES = (
    re.compile(r"""host=['"]([^'"\s]+)['"]"""),
    re.compile(r"https?://([A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9])"),
    re.compile(r"Could not resolve host: ([A-Za-z0-9][A-Za-z0-9.-]*)"),
)

_HOST_DENIAL_SIGNS = ("operation not permitted", "permission denied")

_ABSOLUTE_PATH = re.compile(r"""(/[^\s'"`:]+)""")

_OFFLINE_NOTE = (
    "This command ran with reach `offline`, which has no network. The VM does have "
    "network: run the same command again with reach `network` to reach the domains the "
    "operator has allowed. Do not move it to `host`; installs and downloads belong in the VM."
)

_CREDENTIALS_NOTE = (
    "The VM's egress gateway did not accept this command's proxy credentials. Run it "
    "again; if it is refused the same way, tell the operator rather than moving it to `host`."
)

_READ_ONLY_NOTE = (
    "A project you may only read is mounted read-only. To change it, edit the file "
    "with the `files_*` tools, which ask the operator once and then let this thread "
    "write that project, and run the command again."
)


def is_egress_denied(text: str) -> bool:
    """Whether ``text`` reads as a connection the VM's gateway refused or that never left."""
    return DENIED_MARKER.lower() in text.lower() or mentions_no_network(text)


def denied_host(text: str) -> str | None:
    """The host the gateway refused, when ``text`` names it."""
    found = _DENIED_BODY_HOST.search(text)
    if found is not None:
        return found.group(1).strip().rstrip(".,;)").lower() or None
    low = text.lower()
    if not any(sign in low for sign in _TUNNEL_DENIED_SIGNS):
        return None
    for pattern in _HOST_CANDIDATES:
        match = pattern.search(text)
        if match is not None:
            return match.group(1).rstrip(".").lower()
    return None


def _egress_note(host: str | None) -> str:
    named = f" (`{host}`)" if host else ""
    return (
        f"The VM's egress gateway refused a host{named} this conversation has not been "
        "allowed to reach. Ask for it with `code_request_egress`, then run the command again."
    )


def vm_refusal(*, network: bool, ok: bool, stdout: str, stderr: str) -> Refusal | None:
    """What the model should do about a refusal by the VM's fence, or None."""
    text = f"{stdout}\n{stderr}"
    if DENIED_MARKER in text:
        host = denied_host(text)
        return Refusal(_egress_note(host), host)
    if not ok:
        refused = "407" in text and any(sign in text for sign in _PROXY_REFUSED_SIGNS)
        if network and refused:
            return Refusal(_CREDENTIALS_NOTE)
        if network and (host := denied_host(text)) is not None:
            return Refusal(_egress_note(host), host)
        if not network and (refused or mentions_no_network(text)):
            return Refusal(_OFFLINE_NOTE)
    if "Read-only file system" in text:
        return Refusal(_READ_ONLY_NOTE)
    return None


def _denied_paths(stderr: str) -> list[str]:
    paths: list[str] = []
    for line in stderr.splitlines():
        if any(sign in line.lower() for sign in _HOST_DENIAL_SIGNS):
            paths += _ABSOLUTE_PATH.findall(line)
    return paths


def host_refusal(
    *, fence_active: bool, ok: bool, stderr: str, home: Path | None = None
) -> Refusal | None:
    """What the model should do about a host file denial, or None.

    The sandbox runtime leaves no marker in stderr, so a denial is inferred from the OS
    error. One naming a path on the protected-write floor is certain; any other is reported
    only for a failed command, because a successful one can print the same words about files
    it was never meant to touch.
    """
    if not fence_active:
        return None
    text = stderr.lower()
    if not any(sign in text for sign in _HOST_DENIAL_SIGNS):
        return None
    base = home if home is not None else Path.home()
    for path in _denied_paths(stderr):
        reason = is_protected_write(path, home=base)
        if reason is not None:
            return Refusal(
                f"The host fence refused a write to {path}: {reason}. The path is outside "
                "what the host fence allows, so report it to the operator rather than "
                "working around it."
            )
    if ok:
        return None
    return Refusal(
        "This failed with a permission error, which can be the host fence: it allows writes "
        "under the home, the temp directories and the scratch directory, minus protected "
        "paths such as shell rc files and `~/.claude`, and it hides credential files, browser "
        "profiles and this application's data. If the path is one of those, report it to the "
        "operator rather than working around it."
    )
