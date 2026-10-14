"""The command surface — what a session actually types.

Written for a reader that arrives with no context and one turn to spend. Two rules
follow from that, and they are why this module exists rather than a handful of scripts:

**Every command takes ``--json``.** A session that has to parse prose to learn a port
number will eventually parse it wrong, and be confidently wrong afterwards. The human
form is a convenience over the machine form, never the only form.

**Output ends with the next action, spelled out.** Knowing the state and knowing what to
do about it are different things, and a message that reports only the first leaves the
reader to guess the second.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import TypedDict

from devkit import doctor, launch, ports, processes, surfaces
from devkit.instance import DevInstance, resolve
from devkit.processes import Supervisor
from services.workspace_reset import reset_workspace


def _no_command(_args: argparse.Namespace) -> int:
    return 2


class _Args(argparse.Namespace):
    """The parsed command line; ``handler`` is replaced by the chosen subcommand's."""

    handler: Callable[[_Args], int] = _no_command
    json: bool = False
    detach: bool = False
    auth: bool | None = None
    with_vm: bool | None = None
    reseed: bool = False
    force: bool = False
    yes: bool = False


class StatusPayload(TypedDict):
    name: str
    slot: int
    root: str
    data_dir: str
    urls: dict[str, str]
    ports: dict[str, int]
    running: dict[str, bool]
    ready: bool
    seeded: str | None
    auth: bool
    vm: bool
    password: str


def _service_states(instance: DevInstance) -> dict[str, bool]:
    """Which of the three services are listening. A port that will not bind is one
    something already holds — this instance's own service, in the ordinary case."""
    return {
        "backend": not ports.is_free(instance.backend_port),
        "frontend": not ports.is_free(instance.frontend_port),
        "stub": not ports.is_free(instance.stub_port),
    }


def status_payload(instance: DevInstance) -> StatusPayload:
    """Everything about an instance, in the shape ``--json`` prints.

    The single source for ports: the skill tells a session to read them from here rather
    than assume slot 0's, because a parallel worktree is on a different slot and a
    hardcoded 5273 would quietly drive the wrong instance.
    """
    running = _service_states(instance)
    return {
        "name": instance.name,
        "slot": instance.slot,
        "root": str(instance.root),
        "data_dir": str(instance.data_dir),
        "urls": {
            "frontend": instance.frontend_url,
            "backend": instance.backend_url,
            "stub": instance.stub_url,
        },
        "ports": {
            "frontend": instance.frontend_port,
            "backend": instance.backend_port,
            "stub": instance.stub_port,
        },
        "running": running,
        "ready": all(running.values()),
        "seeded": instance.seeded or None,
        "auth": instance.auth,
        "vm": instance.vm,
        # Printed because with auth off nobody is asked for it and it would otherwise
        # look like a secret that had gone missing when someone turns auth on.
        "password": instance.password,
    }


def _print_status(payload: StatusPayload) -> None:
    running = payload["running"]
    mark = {True: "up", False: "down"}
    print(f"dev instance '{payload['name']}'  (slot {payload['slot']})")
    print(f"  data      {payload['data_dir']}")
    for service in ("frontend", "backend", "stub"):
        print(f"  {service:<9} {payload['urls'][service]:<28} {mark[running[service]]}")
    print(f"  seeded    {payload['seeded'] or 'not yet'}")
    print(f"  auth      {'on — password below' if payload['auth'] else 'off — no login screen'}")
    print(f"  password  {payload['password']}")
    if not payload["ready"]:
        print("\nnext: uv run python dev_instance.py up")


def _cmd_status(args: _Args) -> int:
    instance = resolve()
    payload = status_payload(instance)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_status(payload)
    return 0


def _print_ready(payload: StatusPayload, outcomes: dict[str, str], *, detached: bool) -> None:
    """The block a session reads after ``up``. Ends with the next action, spelled out —
    knowing the state and knowing what to do about it are different things."""
    print(f"\ndev instance '{payload['name']}' ready  (slot {payload['slot']})\n")
    for service in ("frontend", "backend", "stub"):
        print(f"  {service:<9} {payload['urls'][service]:<28} {outcomes.get(service, '')}")
    print(f"  seed      {outcomes.get('seed', '')}")
    print(f"  data      {payload['data_dir']}")
    print(f"  logs      {payload['root']}/logs")
    if payload["auth"]:
        print(f"  password  {payload['password']}")
    else:
        print("  login     none — auth is off, the app opens straight up")
    print(f"\nnext: preview_start '{surfaces.LAUNCH_CONFIG}'")
    print(f"      or open {payload['urls']['frontend']}")
    print("      ports differ per worktree — read them from `dev_instance.py status --json`")
    print(
        "\nrunning in the background; stop it with `dev_instance.py stop`"
        if detached
        else "\nCtrl-C stops the services this command started."
    )


def _cmd_up(args: _Args) -> int:
    instance = resolve(auth=args.auth, vm=args.with_vm)
    supervisor = Supervisor()
    try:
        instance, outcomes = launch.bring_up(instance, supervisor, reseed=args.reseed)
    except launch.LaunchError as failure:
        supervisor.stop_all()
        print(f"error: {failure}", file=sys.stderr)
        return 1
    except Exception as failure:  # noqa: BLE001 — see below
        # Everything, not just LaunchError: `bring_up` also seeds, and seeding raises
        # ordinary HTTP and JSON errors. One escaping here would leave the services this
        # run started alive and unrecorded — running, and beyond the reach of `stop`.
        supervisor.stop_all()
        print(f"error: bringing the instance up failed: {failure!r}", file=sys.stderr)
        print(f"       logs: {instance.logs_dir}", file=sys.stderr)
        return 1

    _ = supervisor.record(instance.root)
    payload = status_payload(instance)
    if args.json:
        print(json.dumps({**payload, "outcomes": outcomes, "detached": args.detach}, indent=2))
    else:
        _print_ready(payload, outcomes, detached=args.detach)

    if args.detach:
        # Each child has its own process group, so they outlive this command. `stop`
        # reads the pidfile just recorded and signals those groups.
        return 0
    try:
        launch.wait_for_signal()
    finally:
        supervisor.stop_all()
        # Only what this run started. A foreground `up` that converged onto an already
        # running detached instance owns nothing, and clearing the record on its way out
        # would strand the detached services with no way to stop them.
        supervisor.forget(instance.root)
    return 0


def _cmd_seed(args: _Args) -> int:
    instance = resolve()
    if not launch.backend_healthy(instance):
        print(
            "error: the backend is not running — start it with `uv run python dev_instance.py up`",
            file=sys.stderr,
        )
        return 1
    instance, summary = launch.ensure_seeded(instance, force=args.force)
    print(json.dumps({"seed": summary}) if args.json else summary)
    return 0


def _cmd_reset(args: _Args) -> int:
    instance = resolve()
    if not args.yes:
        print(
            f"this deletes everything under {instance.data_dir}.\nre-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 1
    _ = processes.stop_recorded(instance.root)
    summary = reset_workspace(instance.data_dir)
    # `reset_workspace` deliberately preserves the live database — it is written for a
    # running app that holds the file open. Nothing holds it now, and leaving it would
    # keep every seeded row behind the key that was just deleted.
    removed = list(summary.removed)
    for leftover in instance.data_dir.glob("app.db*"):
        leftover.unlink(missing_ok=True)
        removed.append(leftover.name)
    instance.with_seeded("").save()
    payload = {"removed": removed, "failed": summary.failed, "bytes_freed": summary.bytes_freed}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"removed {len(removed)} entries from {instance.data_dir}")
        if summary.failed:
            print(f"could not remove: {', '.join(summary.failed)}")
        print("next: uv run python dev_instance.py up   (it will re-seed)")
    return 0


def _cmd_doctor(args: _Args) -> int:
    findings = doctor.run(resolve())
    if args.json:
        print(json.dumps([finding.as_dict() for finding in findings], indent=2))
        return 0
    for finding in findings:
        mark = {doctor.OK: " ok ", doctor.WARN: "warn", doctor.FAIL: "FAIL"}[finding.status]
        print(f"[{mark}] {finding.name:<18} {finding.detail}")
        if finding.remedy:
            print(f"         → {finding.remedy}")
    return 1 if any(f.status == doctor.FAIL for f in findings) else 0


def _cmd_stop(args: _Args) -> int:
    instance = resolve()
    stopped = processes.stop_recorded(instance.root)
    if args.json:
        print(json.dumps({"stopped": stopped}))
    elif stopped:
        print(f"stopped: {', '.join(stopped)}")
    else:
        print("nothing recorded as running for this worktree's instance")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dev_instance",
        description=(
            "Run an isolated Hephaestus for development — its own data, ports and "
            "platform VM, seeded and safe to break."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    up = subcommands.add_parser(
        "up",
        help="bring the instance up (safe to run twice — it fills in what is missing)",
    )
    _ = up.add_argument(
        "--detach",
        action="store_true",
        help="return once everything is up, leaving it running; stop it with `stop`",
    )
    # Both are tri-state on purpose: given, they change the instance and stick, so a
    # session need not repeat them; omitted, they leave the stored choice alone. Without
    # the `--no-` half a flag could only ever be turned on, and an instance that once ran
    # `--with-vm` would keep booting a VM on every later `up`.
    _ = up.add_argument(
        "--auth",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="put the real login screen in front of the app (default: off, and remembered)",
    )
    _ = up.add_argument(
        "--with-vm",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="also run the platform VM: sandbox, SearXNG and web fetch (remembered)",
    )
    _ = up.add_argument(
        "--reseed",
        action="store_true",
        help="re-run the fixtures even if the pack has not changed",
    )
    up.set_defaults(handler=_cmd_up)

    status = subcommands.add_parser(
        "status", help="what this worktree's instance is and whether it is running"
    )
    status.set_defaults(handler=_cmd_status)

    stop = subcommands.add_parser("stop", help="stop the services a previous `up` started")
    stop.set_defaults(handler=_cmd_stop)

    seed = subcommands.add_parser("seed", help="run the fixture pack against the running instance")
    _ = seed.add_argument(
        "--force", action="store_true", help="re-run even if the pack has not changed"
    )
    seed.set_defaults(handler=_cmd_seed)

    reset = subcommands.add_parser(
        "reset", help="wipe this instance's workspace so the next `up` starts clean"
    )
    _ = reset.add_argument("--yes", action="store_true", help="confirm the deletion")
    reset.set_defaults(handler=_cmd_reset)

    check = subcommands.add_parser("doctor", help="what is wrong and what to do about it")
    check.set_defaults(handler=_cmd_doctor)

    for sub in subcommands.choices.values():
        _ = sub.add_argument(
            "--json", action="store_true", help="machine-readable output (prefer this)"
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv, namespace=_Args())
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
