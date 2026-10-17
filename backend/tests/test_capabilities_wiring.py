"""Regression guard: every capability the agent resolves from the run's bag must be
assembled into the app's one agent-facing bag.

``ServiceContainer.get_optional`` returning ``None`` is the tools' *degrade* contract —
a tool whose capability isn't wired reports itself unavailable instead of crashing the
turn. The flip side is that a manifest which forgets its ``capabilities`` export (or an
app-assembly line that drops a core handle) still type-checks, still imports, and still
passes every unit test; the tool just goes silently unavailable at runtime. The old
hand-enumerated ``Capabilities`` dataclass had exactly this failure mode at every
construction site — the approval-resume path once shipped without the handles for the
very tools only it can execute. The bag has one assembly point, so one test can now
guard all of it: collect every type the tool and agent layers look up, boot the real app,
and demand each one is present.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

from routes.deps import app_state
from services.plugins import PluginRuntime
from services.sandbox import SandboxSessionManager
from services.sandbox.thread_shells import ThreadShells
from services.vm.project import ProjectCopies
from tests._helpers import client_app

_BACKEND = Path(__file__).resolve().parents[1]

# Capabilities that are *environment-conditional by design* — absent from the bag when
# the host lacks the facility, which is exactly how the test app boots. Each entry
# needs a reason; anything else missing is a wiring bug, not a condition.
_CONDITIONAL = {
    # Added only when the sandbox is enabled; tests run sandbox_enabled=False.
    SandboxSessionManager,
    # Added only when the platform VM is enabled; tests run vm_enabled=False.
    ProjectCopies,
    ThreadShells,
    PluginRuntime,
}


def _bag_lookups(path: Path) -> set[str]:
    """The type names a module resolves off a capability bag: the argument of every
    ``caps.get(...)`` / ``caps.get_optional(...)`` call (any receiver ending in
    ``.caps``, e.g. ``ctx.deps.caps``, counts)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("get", "get_optional"):
            continue
        receiver = node.func.value
        is_caps = (isinstance(receiver, ast.Name) and receiver.id == "caps") or (
            isinstance(receiver, ast.Attribute) and receiver.attr == "caps"
        )
        if is_caps and node.args and isinstance(node.args[0], ast.Name):
            names.add(node.args[0].id)
    return names


def _resolved_lookup_types() -> dict[type, str]:
    """Every concrete type the tool layer + the agent layer resolve from the bag, mapped
    to the module that looks it up — resolved through each module's own imports, so a
    renamed or moved class can't desynchronize the guard.

    Both packages are walked whole rather than named module by module: the engine's
    lookups have moved between its neighbours more than once, and a list of module names
    would drop a lookup out of the guard the moment one of them moved again."""
    import agent
    import tools

    modules = [f"tools.{m.name}" for m in pkgutil.iter_modules(tools.__path__)]
    modules += [f"agent.{m.name}" for m in pkgutil.iter_modules(agent.__path__)]
    lookups: dict[type, str] = {}
    for module_name in modules:
        module = importlib.import_module(module_name)
        source = _BACKEND / (module_name.replace(".", "/") + ".py")
        for name in _bag_lookups(source):
            lookups[getattr(module, name)] = module_name
    return lookups


async def test_every_bag_lookup_is_assembled() -> None:
    lookups = _resolved_lookup_types()
    # The scan itself must be alive — an AST drift that finds nothing would otherwise
    # pass vacuously while guarding nothing.
    assert len(lookups) >= 10, (
        f"bag-lookup scan found too few types: {sorted(t.__name__ for t in lookups)}"
    )

    async with client_app() as (_, app):
        bag = app_state(app).capabilities
        missing = {
            f"{t.__name__} (looked up by {module})"
            for t, module in lookups.items()
            if t not in _CONDITIONAL and bag.get_optional(t) is None
        }
        assert not missing, (
            f"agent-facing bag is missing {sorted(missing)} — a manifest's `capabilities` "
            "export (or an app-assembly add) was dropped, so the tool that resolves it "
            "would silently report itself unavailable at runtime. If the capability is "
            "genuinely environment-conditional, add it to _CONDITIONAL with a reason."
        )
        for t in _CONDITIONAL & set(lookups):
            assert bag.get_optional(t) is None, (
                f"{t.__name__} is in _CONDITIONAL but was present in the test app's bag — "
                "it is no longer conditional; guard it unconditionally instead."
            )
