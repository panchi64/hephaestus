"""How a command that a fence stopped is reported, for the VM's fence and the host's.

A conversation's VM resolves only the hosts its egress allowlist names, so a refused host
shows up as a name-resolution failure. The result is a note for the model and, where the
output names the host, the host itself, which the command card offers to allow.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from .floor import credential_read_floor, is_protected_write
from .network_errors import mentions_no_network


@dataclass(frozen=True, slots=True)
class Refusal:
    """A fence's refusal: what the model should do about it, and the denied host when the
    output names one."""

    note: str
    denied_host: str | None = None


#: The places a client names the host it was reaching: curl's and Python's resolvers, a
#: URL, urllib3's pool.
_HOST_CANDIDATES = (
    re.compile(r"Could not resolve host: ([A-Za-z0-9][A-Za-z0-9.-]*)"),
    re.compile(r"""host=['"]([^'"\s]+)['"]"""),
    re.compile(r"https?://([A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9])"),
)

_HOST_DENIAL_SIGNS = ("operation not permitted", "permission denied")

_ABSOLUTE_PATH = re.compile(r"""(/[^\s'"`:]+)""")

_READ_ONLY_NOTE = (
    "A project you may only read is mounted read-only. To change it, edit the file "
    "with the `files_*` tools, which ask the operator once and then let this thread "
    "write that project, and run the command again."
)


def is_egress_denied(text: str) -> bool:
    """Whether ``text`` reads as a connection that never left the VM."""
    return mentions_no_network(text)


def denied_host(text: str) -> str | None:
    """The host a refused connection was for, when ``text`` names it."""
    if not mentions_no_network(text):
        return None
    for pattern in _HOST_CANDIDATES:
        match = pattern.search(text)
        if match is not None:
            return match.group(1).rstrip(".").lower()
    return None


def egress_note(host: str | None) -> str:
    named = f" (`{host}`)" if host else ""
    return (
        f"Your VM could not reach a host{named}: it reaches only the hosts this conversation "
        "is allowed, and none while the platform is offline. Ask for it with "
        "`code_request_egress`, then run the command again."
    )


def vm_refusal(*, ok: bool, stdout: str, stderr: str) -> Refusal | None:
    """What the model should do about a refusal by the VM's fence, or None."""
    text = f"{stdout}\n{stderr}"
    if not ok and mentions_no_network(text):
        host = denied_host(text)
        return Refusal(egress_note(host), host)
    if "Read-only file system" in text:
        return Refusal(_READ_ONLY_NOTE)
    return None


def _denied_paths(stderr: str) -> list[str]:
    paths: list[str] = []
    for line in stderr.splitlines():
        if any(sign in line.lower() for sign in _HOST_DENIAL_SIGNS):
            paths += _ABSOLUTE_PATH.findall(line)
    return paths


_SSH_NOTE = (
    "the SSH private key is not readable inside the host fence; ask the operator to load it "
    "into ssh-agent once (`ssh-add --apple-use-keychain <key>` on macOS) so git can sign "
    "through the agent."
)

#: Path fragments under the home that name a credential file's tool, and what to call it.
_CREDENTIAL_KINDS = (
    (".config/gh", "GitHub CLI token file"),
    (".docker", "Docker credentials file"),
    (".kube", "kubeconfig"),
    (".aws", "AWS credentials file"),
    (".config/gcloud", "gcloud token file"),
    (".netrc", ".netrc credentials file"),
)


def _credential_note(path: str, home: Path) -> str | None:
    """The note for a path on the credential read floor, or None for any other path."""
    covered = any(
        fnmatch.fnmatchcase(path, entry) or path.startswith(entry.rstrip("/") + "/")
        for entry in credential_read_floor(home)
    )
    if not covered:
        return None
    relative = path.removeprefix(str(home) + "/")
    if relative.startswith(".ssh/"):
        return _SSH_NOTE
    kind = next((name for part, name in _CREDENTIAL_KINDS if relative.startswith(part)), None)
    return (
        f"the {kind or 'credentials file'} is not readable inside the host fence; ask the "
        "operator to use the tool's credential helper or keychain login instead."
    )


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
        credential = _credential_note(path, base)
        if credential is not None:
            return Refusal(credential)
        reason = is_protected_write(path, home=base)
        if reason is not None:
            return Refusal(
                f"The host fence refused a write to {path}: {reason}. The path is outside "
                + "what the host fence allows, so report it to the operator rather than "
                + "working around it."
            )
    if ok:
        return None
    return Refusal(
        "This failed with a permission error, which can be the host fence: it allows writes "
        + "under the home, the temp directories and the scratch directory, minus protected "
        + "paths such as shell rc files and `~/.claude`, and it hides credential files, browser "
        + "profiles and this application's data. If the path is one of those, report it to the "
        + "operator rather than working around it."
    )
