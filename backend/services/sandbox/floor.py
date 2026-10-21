"""The deny floor under every host command, and the input to the file-tool write guard.

Hardcoded on purpose: the settings lists can add to the floor and nothing in settings
removes an entry. :func:`is_protected_write` answers the same question for a single path,
so the guard over the file tools (which run unfenced) reads the list the fence is built
from.

**What the floor cannot reach.** ``.git/hooks`` and ``.git/config`` are denied for each
writable root itself. On macOS the fence also gets absolute globs that cover nested
repositories under each root. On Linux the runtime skips globs and any deny path that does
not exist yet, so a nested repository's hooks and config, and a missing ``~/.gitconfig`` or
``~/.claude``, are not protected there. On Linux the runtime blocks unix sockets
with seccomp and has no per-socket allowlist, so ssh-agent and docker sockets are
unavailable inside the fence there. Renaming a nested ``.git`` directory is not denied
on either platform, since that would also block ``git init``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path, PurePath
from urllib.parse import unquote

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
    ".config/google-chrome",
    ".config/chromium",
    ".mozilla",
    ".config/BraveSoftware",
)

_CREDENTIAL_FILES = (
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".config/git/credentials",
    ".cargo/credentials",
    ".cargo/credentials.toml",
)


def source_root() -> Path:
    """The backend package root, where the application's dotenv file sits."""
    return Path(core.__file__).resolve().parent.parent


def write_floor(home: Path) -> tuple[str, ...]:
    """Paths no command may write, whatever the settings allow."""
    return (
        *(str(home / name) for name in _SHELL_RC),
        str(home / "Library" / "LaunchAgents"),
        str(home / ".claude"),
        str(home / ".gitconfig"),
        str(home / ".config" / "git" / "config"),
    )


def hooks_floor(writable_roots: Iterable[str], *, nested: bool | None = None) -> tuple[str, ...]:
    """``.git/hooks`` and ``.git/config`` under each writable root.

    ``nested`` adds absolute globs reaching repositories below each root; it defaults to
    on for macOS, the only platform whose runtime honours them.
    """
    if nested is None:
        nested = sys.platform == "darwin"
    entries: list[str] = []
    for root in writable_roots:
        base = Path(root)
        entries += [str(base / ".git" / "hooks"), str(base / ".git" / "config")]
        if nested:
            entries += [
                str(base / "**" / ".git" / "hooks"),
                str(base / "**" / ".git" / "hooks" / "**"),
                str(base / "**" / ".git" / "config"),
            ]
    return tuple(entries)


def _db_files(db_url: str | None) -> tuple[str, ...]:
    prefix = "sqlite:///"
    if not db_url or not db_url.startswith(prefix) or ":memory:" in db_url:
        return ()
    path = unquote(db_url[len(prefix) :].split("?", 1)[0])
    if not path:
        return ()
    resolved = str(Path(path).expanduser().resolve())
    return (resolved, f"{resolved}-wal", f"{resolved}-shm")


def read_floor(home: Path, data_dir: Path, db_url: str | None = None) -> tuple[str, ...]:
    """Paths no command may read, whatever the settings say.

    The application's dotenv file is named at both places pydantic-settings can find it:
    the process working directory and the backend root. The source tree itself stays
    readable, since agents work in repositories that include this one.
    """
    data = data_dir.resolve()
    return (
        *(str(home / name) for name in _CREDENTIAL_FILES),
        *(str(home / rel) for rel in _BROWSER_PROFILES),
        str(Path.cwd() / ".env"),
        str(source_root() / ".env"),
        str(data),
        *(entry for entry in _db_files(db_url) if not Path(entry).is_relative_to(data)),
    )


def merged(floor: Iterable[str], extra: Iterable[str]) -> tuple[str, ...]:
    """``floor`` first, then whatever ``extra`` adds, without duplicates."""
    return tuple(dict.fromkeys((*floor, *extra)))


def _fold(path: PurePath) -> tuple[str, ...]:
    """The comparable parts of ``path``: lowercased on macOS, whose default volumes are
    case-insensitive."""
    if sys.platform == "darwin":
        return tuple(part.lower() for part in path.parts)
    return path.parts


def _is_under(candidate: PurePath, protected: PurePath) -> bool:
    base = _fold(protected)
    return _fold(candidate)[: len(base)] == base


def is_protected_write(path: str | Path, *, home: Path) -> str | None:
    """Why a write to ``path`` is refused, or ``None`` when the floor has no objection."""
    target = Path(path).expanduser()
    if not target.is_absolute():
        return None
    for candidate in {target, target.resolve()}:
        parts = _fold(candidate)
        for idx in range(len(parts) - 1):
            if parts[idx] == ".git" and parts[idx + 1] in ("hooks", "config"):
                return "git hooks and config can make git run arbitrary code"
        for base in {home, home.resolve()}:
            for entry in write_floor(base):
                if _is_under(candidate, Path(entry)):
                    return f"{entry} is on the protected-write floor"
    return None
