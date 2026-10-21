"""The deny floor under every host command and, through :func:`is_protected_write`, the file tools.

Hardcoded on purpose: the settings lists (``host_command_deny_read`` and the allow-write
lists) can add to the floor and nothing in settings removes an entry. Entries the OS fence
already denies by itself are listed anyway, because the same list also guards callers that
run unfenced.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import core

_SHELL_RC = (".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile", ".profile")

_BROWSER_PROFILES = (
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Chromium",
    "Library/Application Support/Firefox",
    "Library/Application Support/Safari",
    "Library/Application Support/Arc",
    "Library/Application Support/BraveSoftware",
    "Library/Safari",
)

_CREDENTIAL_FILES = (".git-credentials", ".npmrc", ".pypirc")


def source_root() -> Path:
    """The backend package root: the application's source tree and its dotenv file."""
    return Path(core.__file__).resolve().parent.parent


def write_floor(home: Path) -> tuple[str, ...]:
    """Paths no command may write, whatever the settings allow."""
    return (
        *(str(home / name) for name in _SHELL_RC),
        str(home / "Library" / "LaunchAgents"),
        str(home / ".claude"),
        str(home / ".gitconfig"),
    )


def hooks_floor(writable_roots: Iterable[str]) -> tuple[str, ...]:
    """``.git/hooks`` under each writable root."""
    return tuple(str(Path(root) / ".git" / "hooks") for root in writable_roots)


def read_floor(home: Path, data_dir: Path) -> tuple[str, ...]:
    """Paths no command may read, whatever the settings say."""
    return (
        *(str(home / name) for name in _CREDENTIAL_FILES),
        str(home / ".cargo" / "credentials"),
        str(home / ".cargo" / "credentials.toml"),
        *(str(home / rel) for rel in _BROWSER_PROFILES),
        str(source_root()),
        str(data_dir.resolve()),
    )


def merged(floor: Iterable[str], extra: Iterable[str]) -> tuple[str, ...]:
    """``floor`` first, then whatever ``extra`` adds, without duplicates."""
    return tuple(dict.fromkeys((*floor, *extra)))


def is_protected_write(path: str | Path, *, home: Path) -> str | None:
    """Why a write to ``path`` is refused, or ``None`` when the floor has no objection."""
    target = Path(path).expanduser()
    if not target.is_absolute():
        return None
    candidates = {target, target.resolve()}
    homes = {home, home.resolve()}
    for candidate in candidates:
        parts = candidate.parts
        for idx in range(len(parts) - 1):
            if parts[idx] == ".git" and parts[idx + 1] == "hooks":
                return "git hooks run arbitrary code on the operator's machine"
        for base in homes:
            for entry in write_floor(base):
                protected = Path(entry)
                if candidate == protected or protected in candidate.parents:
                    return f"{protected} is on the protected-write floor"
    return None
