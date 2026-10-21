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

import json
import sys
from collections.abc import Iterable
from pathlib import Path, PurePath
from typing import cast
from urllib.parse import unquote

import yaml

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


_SSH_READABLE = ("known_hosts", "known_hosts.old", "config")

_ALWAYS_DENIED = (
    ".aws/credentials",
    ".aws/sso/cache",
    ".aws/cli/cache",
    ".config/gcloud/credentials.db",
    ".config/gcloud/access_tokens.db",
    ".config/gcloud/legacy_credentials",
    ".config/gcloud/application_default_credentials.json",
    ".netrc",
    ".docker/.token_seed",
    "Library/Keychains",
)

_KUBE_USER_SECRETS = ("token", "client-key-data", "client-key", "password")
_KUBE_PROVIDER_TOKENS = ("access-token", "id-token", "refresh-token", "token", "client-secret")


def _has_key(obj: object, names: tuple[str, ...]) -> bool:
    """Whether any mapping nested in ``obj`` holds a non-empty value under one of ``names``."""
    if isinstance(obj, dict):
        mapping = cast(dict[object, object], obj)
        return any(
            (key in names and bool(value)) or _has_key(value, names)
            for key, value in mapping.items()
        )
    if isinstance(obj, list):
        return any(_has_key(item, names) for item in cast(list[object], obj))
    return False


def _mapping(obj: object) -> dict[str, object] | None:
    return cast(dict[str, object], obj) if isinstance(obj, dict) else None


def _yaml_file(path: Path) -> object:
    return cast(object, yaml.safe_load(path.read_text(encoding="utf-8")))


def _gh_holds_secret(path: Path) -> bool:
    data = _yaml_file(path)
    if data is None:
        return False
    return _mapping(data) is None or _has_key(data, ("oauth_token",))


def _docker_holds_secret(path: Path) -> bool:
    config = _mapping(cast(object, json.loads(path.read_text(encoding="utf-8"))))
    if config is None:
        return True
    raw_auths: object = config.get("auths") or {}
    entries = _mapping(raw_auths)
    if entries is None:
        return True
    if any(_has_key(entry, ("auth", "identitytoken")) for entry in entries.values()):
        return True
    return bool(entries) and not (config.get("credsStore") or config.get("credHelpers"))


def _kube_holds_secret(path: Path) -> bool:
    data = _yaml_file(path)
    if data is None:
        return False
    config = _mapping(data)
    if config is None:
        return True
    users: object = config.get("users") or []
    if not isinstance(users, list):
        return True
    for entry in cast(list[object], users):
        fields = _mapping((_mapping(entry) or {}).get("user"))
        if fields is None:
            continue
        if any(fields.get(name) for name in _KUBE_USER_SECRETS):
            return True
        provider = fields.get("auth-provider")
        if provider:
            body = _mapping(provider)
            if body is None or _has_key(body.get("config"), _KUBE_PROVIDER_TOKENS):
                return True
    return False


def _inline_secret_files(home: Path) -> tuple[str, ...]:
    """The mixed config files that hold a secret inline, or cannot be shown not to."""
    checks = (
        (home / ".config" / "gh" / "hosts.yml", _gh_holds_secret),
        (home / ".docker" / "config.json", _docker_holds_secret),
        (home / ".kube" / "config", _kube_holds_secret),
    )
    denied: list[str] = []
    for path, holds_secret in checks:
        if not path.is_file():
            continue
        try:
            secret = holds_secret(path)
        except OSError, ValueError, yaml.YAMLError:
            secret = True
        if secret:
            denied.append(str(path))
    return tuple(denied)


def _ssh_private_files(home: Path) -> tuple[str, ...]:
    """Everything in ``~/.ssh`` but the host keys, ``config`` and ``*.pub``, as the
    directory stands now, plus globs for files that appear later (macOS only)."""
    ssh = home / ".ssh"
    present: list[str] = []
    if ssh.is_dir():
        present = [
            str(entry)
            for entry in sorted(ssh.iterdir())
            if entry.name not in _SSH_READABLE and not entry.name.endswith(".pub")
        ]
    # The runtime escapes `^` in globs and seatbelt matches case-insensitively, so "not
    # ending in b" is spelled as the other final characters, minus B; that keeps `id_*.pub`
    # readable, which a deny-read glob cannot otherwise do.
    globs = [str(ssh / "id_*[aAc-zC-Z0-9]"), str(ssh / "*.pem"), str(ssh / "*_key")]
    return (*present, *globs)


def _docker_tls_keys(home: Path) -> tuple[str, ...]:
    contexts = home / ".docker" / "contexts"
    found = [str(p) for p in contexts.rglob("*.pem")] if contexts.is_dir() else []
    return (*found, str(contexts / "**" / "*.pem"))


def credential_read_floor(home: Path) -> tuple[str, ...]:
    """Secrets reach tools through helpers (ssh-agent, Keychain IPC, credential helpers),
    never as readable files: this denies key and token files, and each of ``gh``, docker
    and kube configs only while it holds a secret inline. Recomputed on every call."""
    return merged(
        (str(home / rel) for rel in _ALWAYS_DENIED),
        merged(
            _ssh_private_files(home),
            merged(_inline_secret_files(home), _docker_tls_keys(home)),
        ),
    )


def merged(floor: Iterable[str], extra: Iterable[str]) -> tuple[str, ...]:
    """``floor`` first, then whatever ``extra`` adds, without duplicates."""
    return tuple(dict.fromkeys((*floor, *extra)))


def fold(path: PurePath) -> tuple[str, ...]:
    """The comparable parts of ``path``: lowercased on macOS, whose default volumes are
    case-insensitive."""
    if sys.platform == "darwin":
        return tuple(part.lower() for part in path.parts)
    return path.parts


def is_under(candidate: PurePath, protected: PurePath) -> bool:
    """Whether ``candidate`` is ``protected`` or inside it, compared as :func:`fold` does."""
    base = fold(protected)
    return fold(candidate)[: len(base)] == base


def is_protected_write(path: str | Path, *, home: Path) -> str | None:
    """Why a write to ``path`` is refused, or ``None`` when the floor has no objection."""
    target = Path(path).expanduser()
    if not target.is_absolute():
        return None
    for candidate in {target, target.resolve()}:
        parts = fold(candidate)
        for idx in range(len(parts) - 1):
            if parts[idx] == ".git" and parts[idx + 1] in ("hooks", "config"):
                return "git hooks and config can make git run arbitrary code"
        for base in {home, home.resolve()}:
            for entry in write_floor(base):
                if is_under(candidate, Path(entry)):
                    return f"{entry} is on the protected-write floor"
    return None
