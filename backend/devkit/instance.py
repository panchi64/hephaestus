"""One dev instance: where its data lives, which ports it holds, how it is configured.

A dev instance is a second, disposable Hephaestus that an agent can drive without touching
the operator's. Everything separating the two is here, and all of it is ordinary
``Settings`` the app already reads from the environment — there is no dev-only code path
inside the app, which is the point: what runs under an agent is the same program the
operator runs, or it proves nothing.

**Keyed by git worktree.** This project is developed in several worktrees at once, and
two of them running the same instance would share a database and fight over ports. The
worktree's own directory name is the key, so isolation follows the way the work is
actually organised without anyone choosing a name.

**Rooted outside the repository.** ``~/.hephaestus/dev/<worktree>/`` rather than somewhere
under the checkout: a worktree is deleted when its branch lands, and a data directory
inside one would be deleted with it. It also puts the instance's database beyond any
reach of ``.gitignore`` being edited, which matters for a directory holding an encrypted
workspace shaped exactly like the operator's real one.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from devkit import ports

#: Where every dev instance on this host lives, one directory per worktree.
ROOT = Path("~/.hephaestus/dev").expanduser()

#: The file recording what was decided the first time an instance was created. Mode 0600
#: because it holds the vault passphrase.
STATE_FILE = "instance.json"


@dataclass(frozen=True, slots=True)
class DevInstance:
    """A resolved instance — every value that distinguishes it from the operator's."""

    name: str
    slot: int
    root: Path
    password: str
    #: The fixture-pack version last seeded into this workspace, or ``""`` if never.
    #: Compared against the current one to decide whether a re-seed is owed.
    seeded: str = ""
    #: Whether the login screen stands in front of the app. Off by default so a session
    #: that navigates here lands in the product rather than at a password prompt; on when
    #: the auth surfaces are themselves what is being worked on.
    auth: bool = False
    #: Whether the platform VM (and with it the sandbox, SearXNG and web fetch) runs. Off
    #: by default: it costs an image build, memory and boot time that most visual checks
    #: never need. The VM lives under this instance's own data dir.
    vm: bool = False

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def worktrees_dir(self) -> Path:
        """Deliberately mirrors the app's own split: code-mode worktrees live outside
        ``data_dir``, and a dev instance must not cut them into the operator's."""
        return self.root / "worktrees"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def backend_port(self) -> int:
        return ports.slot_ports(self.slot)[0]

    @property
    def frontend_port(self) -> int:
        return ports.slot_ports(self.slot)[1]

    @property
    def stub_port(self) -> int:
        return ports.slot_ports(self.slot)[2]

    @property
    def backend_url(self) -> str:
        return f"http://127.0.0.1:{self.backend_port}"

    @property
    def frontend_url(self) -> str:
        return f"http://127.0.0.1:{self.frontend_port}"

    @property
    def stub_url(self) -> str:
        """The OpenAI-compatible base URL the seeded ``main`` endpoint points at."""
        return f"http://127.0.0.1:{self.stub_port}/v1"

    def env(self) -> dict[str, str]:
        """The environment the backend runs under — the whole of the isolation.

        Returned as a complete overlay on ``os.environ`` rather than a patch, so what
        separates this instance from the operator's is one readable list instead of a
        set of assumptions about what the shell happened to hold. Every field that
        matters is set explicitly for the same reason: a ``backend/.env`` is read from
        the working directory, and anything left unset here would be answered by it.
        """
        origins = [
            f"http://127.0.0.1:{self.frontend_port}",
            f"http://localhost:{self.frontend_port}",
        ]
        return {
            **os.environ,
            "HEPHAESTUS_DATA_DIR": str(self.data_dir),
            "HEPHAESTUS_WORKTREES_DIR": str(self.worktrees_dir),
            "HEPHAESTUS_PORT": str(self.backend_port),
            # A list field, so pydantic-settings parses it as JSON. Without both origins
            # the browser blocks every call the dev frontend makes.
            "HEPHAESTUS_CORS_ORIGINS": json.dumps(origins),
            # Set even though `init_db` migrates at boot: it is what an `alembic` command
            # run by hand in a dev shell reads, and without it that command falls back to
            # `alembic.ini`, which points at the operator's live database.
            "HEPHAESTUS_DB_URL": f"sqlite:///{self.data_dir / 'app.db'}",
            "HEPHAESTUS_AUTH_ENABLED": str(self.auth).lower(),
            # Set up on the first boot and unlocked on every later one, so seeding and
            # browsing need no login step even when the gate above is on.
            "HEPHAESTUS_UNLOCK_PASSPHRASE": self.password,
            "HEPHAESTUS_VM_ENABLED": str(self.vm).lower(),
            "HEPHAESTUS_SANDBOX_ENABLED": str(self.vm).lower(),
            "HEPHAESTUS_SEARXNG_ENABLED": str(self.vm).lower(),
            "HEPHAESTUS_WEB_FETCH_ENABLED": str(self.vm).lower(),
            # Assume online without probing: the monitor's boot check is a real
            # connection, and an instance that exists to be screenshotted should not
            # reach the network to decide whether to start.
            "HEPHAESTUS_OFFLINE_CHECK_ENABLED": "false",
        }

    def frontend_env(self) -> dict[str, str]:
        """The environment the frontend dev server runs under — one seam, the API base."""
        return {**os.environ, "VITE_API_BASE": self.backend_url}

    def save(self) -> None:
        """Record what was decided, so the next run inherits it rather than re-deciding."""
        self.root.mkdir(parents=True, exist_ok=True)
        state = self.root / STATE_FILE
        state.write_text(
            json.dumps(
                {
                    "name": self.name,
                    "slot": self.slot,
                    "password": self.password,
                    "seeded": self.seeded,
                    "auth": self.auth,
                    "vm": self.vm,
                },
                indent=2,
            )
            + "\n"
        )
        state.chmod(0o600)

    def with_seeded(self, version: str) -> DevInstance:
        """A copy recording that this fixture-pack version is now in the workspace."""
        return replace(self, seeded=version)


def worktree_name() -> str:
    """This checkout's directory name — the key an instance is filed under.

    Falls back to the current directory's name when git cannot answer, which keeps the
    tool usable in a plain source tree rather than failing on a question that only ever
    exists to keep two checkouts apart.
    """
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=True,
        ).stdout.strip()
    except subprocess.SubprocessError, OSError:
        return Path.cwd().name
    return Path(top).name if top else Path.cwd().name


def claimed_slots(root: Path = ROOT) -> set[int]:
    """The slots every other instance on this host has recorded.

    Read from the state files rather than from what is listening, because an instance
    that is simply not running right now still owns its ports — handing them to a second
    instance would break the first the next time anyone started it.
    """
    slots: set[int] = set()
    if not root.is_dir():
        return slots
    for state in root.glob(f"*/{STATE_FILE}"):
        try:
            slots.add(int(json.loads(state.read_text())["slot"]))
        except OSError, ValueError, KeyError, TypeError:
            continue  # an unreadable neighbour is not a reason to fail
    return slots


def resolve(
    *,
    root: Path = ROOT,
    name: str | None = None,
    auth: bool | None = None,
    vm: bool | None = None,
) -> DevInstance:
    """This worktree's instance, created on first use and read back on every later one.

    ``auth`` and ``vm`` are the only values a caller may change after creation —
    they are flags on a run, not facts about the workspace. The rest is decided once:
    re-deriving a slot while the instance's own services are listening would read them as
    a conflict and move the instance's URLs out from under whoever was using them.
    """
    name = name or worktree_name()
    home = root / name
    state = home / STATE_FILE
    stored_instance: DevInstance | None = None
    if state.is_file():
        stored = json.loads(state.read_text())
        stored_instance = DevInstance(
            name=stored.get("name", name),
            slot=int(stored["slot"]),
            root=home,
            password=stored["password"],
            seeded=stored.get("seeded", ""),
            auth=stored.get("auth", False),
            vm=stored.get("vm", False),
        )
        instance = stored_instance
    else:
        instance = DevInstance(
            name=name,
            slot=ports.allocate(claimed_slots(root)),
            root=home,
            # Generated rather than fixed: it derives the at-rest encryption key, and a
            # constant in the source would key every dev workspace on every machine
            # alike. Nothing needs to memorise it — it is recorded, and with auth off
            # nobody is asked for it.
            password=secrets.token_urlsafe(12),
        )
    if auth is not None:
        instance = replace(instance, auth=auth)
    if vm is not None:
        instance = replace(instance, vm=vm)
    # Written only when something actually changed. `status`, `doctor` and `stop` all
    # resolve an instance in order to read it, and a read that rewrites the file races
    # any concurrent writer over the one record holding the slot and the vault
    # passphrase — a torn write there leaves the workspace unopenable.
    if instance != stored_instance:
        instance.save()
    return instance
