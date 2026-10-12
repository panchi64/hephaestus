"""Suite-wide defaults that keep tests independent of the host they run on.

Settings are read fresh from the environment on every ``get_settings()`` call, so the
place to pin a test-only default is the environment, before any test imports a module
that reads one.

The two patches below are installed at *import* time rather than from a fixture. Both
targets are bound by name at module scope (``from core.db import init_db``, and
``core.crypto``'s module-level hasher), and this file is imported before any test module
is collected — so patching here is what reaches every caller, including the ones inside
``app.py``.
"""

from __future__ import annotations

import os

# Host-command confinement is a *process-global* singleton (`sandbox-runtime` configures
# one sandbox for the whole interpreter). Several tests drive `code_run_host_command`
# through to a real `echo`, which would otherwise initialize that singleton from whatever
# settings the test happened to have — and then only on machines where the platform
# primitive is actually available, since it needs `ripgrep` on PATH. That makes the suite
# behave differently on two developers' laptops for reasons unrelated to what is being
# tested. Off here, so the host path under test is the plain subprocess; the confinement
# logic itself is exercised explicitly in `test_sandbox.py`, which configures it directly.
os.environ.setdefault("ODYSSEUS_HOST_COMMAND_SANDBOX_ENABLED", "false")

import core.crypto  # noqa: E402 — must follow the environment default above
import core.db  # noqa: E402

from . import _schema  # noqa: E402

# Every test database otherwise replays the full Alembic chain; see `_schema`.
core.db.init_db = _schema.init_db

# Argon2id is deliberately costly — 64 MiB over three passes — and every booted test app
# pays two of them, a login verifier and a key-encryption key. That is real seconds per
# run spent re-proving that a slow KDF is slow. What these tests are about is the *shape*
# of the scheme (a DEK only the right passphrase unwraps, a verifier that rejects a wrong
# one, a stale verifier that gets re-minted), and none of that reads the work factors, so
# they go to Argon2's floor here. Production takes its parameters from `core.crypto`
# unchanged; nothing in this file is imported outside the suite.
core.crypto._KEK_TIME_COST = 1
core.crypto._KEK_MEMORY_COST = 8
core.crypto._KEK_PARALLELISM = 1
core.crypto._PASSWORD_HASHER = core.crypto.PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)

# Every agent carries `run_code` (`agent/code_mode.py`), and `TestModel`'s default of
# calling *every* tool it is offered would call it too — with the placeholder string it
# fills every string argument with, which is not a program, so each such turn would spend
# its retries on a `NameError` and die. The tests that use that default are about the
# catalog, not about scripts; the ones that are about scripts name `run_code` and write
# real code (`test_run_code.py`). So "call everything" leaves `run_code` out, and an
# explicit `call_tools=[...]` list is untouched.
from pydantic_ai.models.test import TestModel as _TestModel  # noqa: E402

_call_everything = _TestModel._get_tool_calls


def _call_everything_but_scripts(self, model_request_parameters):
    calls = _call_everything(self, model_request_parameters)
    if self.call_tools == "all":
        return [(name, tool) for name, tool in calls if name != "run_code"]
    return calls


_TestModel._get_tool_calls = _call_everything_but_scripts


def egress_policy(data_dir, domains: tuple[str, ...] = ("pypi.org",)):
    """A real :class:`~services.egress.EgressPolicy` over a throwaway database.

    Here rather than in each suite because four of them need one now — the session
    manager writes the allowlist file on every cold acquire, so it is a construction
    argument rather than a thing under test in most of them. Real rather than a stub: the
    file on disk *is* the interface the fences read, and a stub would only re-describe it.
    """
    from core.db import init_db, make_engine  # noqa: PLC0415 — after the patches above
    from services.egress import EgressPolicy  # noqa: PLC0415

    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return EgressPolicy(engine, data_dir, domains)


async def unfenced(command: str, *, profile=None) -> str:
    """A ``Confiner`` that rewrites nothing — the shell's fence, stood down for a test.

    The project-folder shell refuses to run anything unfenced, and the platform's confinement
    is switched off for the whole suite (above), so every test that wants a command to
    actually execute passes this. Wrapping in the real one instead would mean testing
    seatbelt, on whichever machines happen to have it.
    """
    return command
