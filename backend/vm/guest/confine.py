"""Runs one command for one workspace, confined to that workspace.

Every command runs as the unprivileged sandbox user, with no capabilities, inside
bubblewrap with fresh PID, IPC, UTS, cgroup and network namespaces. It sees the system
read-only, its own workspace at ``/work``, and nothing of any other workspace, the
platform services, or the relay's pool port. Its only network is the egress proxy that
``enter.py`` puts on its loopback. Where the guest has cgroup v2, a workspace's commands
share one cgroup, capped at its memory, CPU and process limits as far as the guest's
controllers allow. A plugin's call also sees that plugin version's directory at
``/plugin``, read-only except while its packages install.

``run`` waits and exits with the command's status (124 on a timeout); ``start`` runs it
as a named background job, whose output, process and exit status are files under
``run/hephaestus/jobs`` that ``stop`` and ``alive`` act on and the host reads.

Standard library only; it is copied into the guest at boot.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys

_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
_PLUGIN = re.compile(r"^[a-z][a-z0-9_]{0,39}/[0-9a-f]{16,64}$")
_PATH = "/usr/local/bin:/usr/bin:/bin"
_CA_STORE = "/etc/ssl/certs/ca-certificates.crt"
_TIMEOUT_EXIT = 124
_KILL_GRACE_S = 5.0


class _Args(argparse.Namespace):
    action: str = ""
    root: str = "/"
    key: str = ""
    name: str = ""
    uid: int = 1100
    workdir: str = ""
    timeout: float = 0
    stdin: str = ""
    publish: int = 0
    memory: str = ""
    cpus: str = ""
    pids: int = 0
    cgroup_root: str = "/sys/fs/cgroup"
    out_offset: int = 0
    err_offset: int = 0
    limit: int = 1 << 20
    cwd_file: str = ""
    plugin: str = ""
    plugin_writable: bool = False

    def __init__(self) -> None:
        super().__init__()
        self.env: list[str] = []
        self.argv: list[str] = []


def _layout(root: str) -> dict[str, str]:
    run = os.path.join(root, "run/hephaestus")
    return {
        "work": os.path.join(root, "work"),
        "sock": os.path.join(run, "egress.sock"),
        "previews": os.path.join(run, "previews"),
        "jobs": os.path.join(run, "jobs"),
        "enter": os.path.join(root, "opt/hephaestus/enter.py"),
        "plugins": os.path.join(root, "plugins"),
    }


def _bwrap(args: _Args, paths: dict[str, str]) -> list[str]:
    workspace = os.path.join(paths["work"], args.key)
    workdir = os.path.normpath(os.path.join("/work", args.workdir or "."))
    if workdir != "/work" and not workdir.startswith("/work/"):
        raise SystemExit("workdir escapes the workspace")
    argv = [
        "bwrap",
        # Every namespace but the user one, set up as root: no user namespace means this
        # works on a kernel built without them, and the command still runs unprivileged
        # because it drops to the sandbox user before anything of its own runs.
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--new-session",
        "--hostname",
        "sandbox",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/etc",
        "/etc",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--perms",
        "1777",
        "--tmpfs",
        "/tmp",
        "--perms",
        "1777",
        "--tmpfs",
        "/dev/shm",
        "--tmpfs",
        "/run",
        "--ro-bind",
        paths["enter"],
        "/run/enter.py",
        "--bind",
        paths["sock"],
        "/run/egress.sock",
        "--bind",
        workspace,
        "/work",
    ]
    if args.publish:
        preview = os.path.join(paths["previews"], args.key)
        argv += ["--bind", preview, "/run/preview"]
    if args.cwd_file:
        argv += ["--bind", args.cwd_file, "/run/cwd"]
    if args.plugin:
        bind = "--bind" if args.plugin_writable else "--ro-bind"
        argv += [bind, _plugin_dir(args, paths), "/plugin"]
    env = {
        "PATH": _PATH,
        "HOME": "/work",
        "LANG": "C.UTF-8",
        "TMPDIR": "/tmp",
        # The system store, which holds any extra CA the image was built to trust;
        # Python and Node tools otherwise read their own bundles.
        "PIP_CERT": _CA_STORE,
        "SSL_CERT_FILE": _CA_STORE,
        "REQUESTS_CA_BUNDLE": _CA_STORE,
        "NODE_EXTRA_CA_CERTS": _CA_STORE,
    }
    for pair in args.env:
        name, _, value = pair.partition("=")
        if name:
            env[name] = value
    argv += ["--clearenv"]
    for name, value in env.items():
        argv += ["--setenv", name, value]
    if args.action == "run":
        # A background command outlives this script; a foreground one must not.
        argv.append("--die-with-parent")
    argv += ["--chdir", workdir, "--", *_drop(args.uid), "python3", "/run/enter.py"]
    if args.publish:
        argv += ["--publish", str(args.publish)]
    return [*argv, "--", *args.argv]


def _drop(uid: int) -> list[str]:
    """Become the sandbox user with no capabilities and no way to regain any."""
    return [
        "setpriv",
        f"--reuid={uid}",
        f"--regid={uid}",
        "--clear-groups",
        "--no-new-privs",
        "--inh-caps=-all",
        "--bounding-set=-all",
        "--",
    ]


def _plugin_dir(args: _Args, paths: dict[str, str]) -> str:
    """A plugin version's directory: its files and its virtualenv, bound at ``/plugin``."""
    if not _PLUGIN.match(args.plugin):
        raise SystemExit("not a plugin version")
    return os.path.join(paths["plugins"], args.plugin)


def _prepare(args: _Args, paths: dict[str, str]) -> None:
    workspace = os.path.join(paths["work"], args.key)
    os.makedirs(workspace, exist_ok=True)
    os.chown(workspace, args.uid, args.uid)
    os.chmod(workspace, 0o700)
    if args.plugin and args.plugin_writable:
        plugin = _plugin_dir(args, paths)
        os.makedirs(plugin, exist_ok=True)
        os.chown(plugin, args.uid, args.uid)
    if args.cwd_file:
        with open(args.cwd_file, "a"):
            pass
        os.chown(args.cwd_file, args.uid, args.uid)
    if args.publish:
        preview = os.path.join(paths["previews"], args.key)
        os.makedirs(preview, exist_ok=True)
        os.chown(preview, args.uid, args.uid)
        try:
            os.unlink(os.path.join(preview, "sock"))
        except FileNotFoundError:
            pass


_UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3}


def _bytes(value: str) -> int:
    value = value.strip().lower()
    if value and value[-1] in _UNITS:
        return int(float(value[:-1]) * _UNITS[value[-1]])
    return int(value)


def _cgroup(args: _Args) -> str | None:
    """The workspace's cgroup with whatever caps the guest's controllers allow, or
    ``None`` where the guest has no cgroup v2. A cap whose controller is missing is
    skipped rather than taking the others with it."""
    root = args.cgroup_root
    try:
        with open(os.path.join(root, "cgroup.controllers")) as handle:
            available = set(handle.read().split())
    except OSError:
        return None
    wanted = {
        "memory": ("memory.max", str(_bytes(args.memory)) if args.memory else ""),
        "pids": ("pids.max", str(args.pids) if args.pids else ""),
        "cpu": ("cpu.max", f"{int(float(args.cpus) * 100000)} 100000" if args.cpus else ""),
    }
    parent = os.path.join(root, "hephaestus")
    group = os.path.join(parent, args.key)
    try:
        os.makedirs(group, exist_ok=True)
    except OSError:
        return None
    for name in wanted:
        if name not in available:
            continue
        for directory in (root, parent):
            try:
                with open(os.path.join(directory, "cgroup.subtree_control"), "w") as handle:
                    _ = handle.write(f"+{name}")
            except OSError:
                break
    for name, (limit, value) in wanted.items():
        if name not in available or not value:
            continue
        try:
            with open(os.path.join(group, limit), "w") as handle:
                _ = handle.write(value)
        except OSError:
            continue
    return group


def _enter(group: str | None):
    """Moves the child into the workspace's cgroup before it runs anything."""

    def join() -> None:
        if group is not None:
            with open(os.path.join(group, "cgroup.procs"), "w") as handle:
                _ = handle.write(str(os.getpid()))

    return join


def _kill(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _job(paths: dict[str, str], name: str, suffix: str) -> str:
    return os.path.join(paths["jobs"], f"{name}.{suffix}")


def run(args: _Args, paths: dict[str, str]) -> int:
    """Run and wait. Under ``--name`` the output, the process and the exit status go to
    that job's files, which is how a background job is run."""
    _prepare(args, paths)
    with contextlib.ExitStack() as stack:
        stdin = stack.enter_context(open(args.stdin or os.devnull, "rb"))
        out = err = None
        if args.name:
            out = stack.enter_context(open(_job(paths, args.name, "out"), "wb"))
            err = stack.enter_context(open(_job(paths, args.name, "err"), "wb"))
        proc = subprocess.Popen(
            _bwrap(args, paths),
            stdin=stdin,
            stdout=out,
            stderr=err,
            start_new_session=True,
            preexec_fn=_enter(_cgroup(args)),
        )
        if args.name:
            _write(_job(paths, args.name, "pid"), str(proc.pid))
        try:
            code = proc.wait(timeout=args.timeout or None)
        except subprocess.TimeoutExpired:
            _kill(proc.pid, signal.SIGTERM)
            try:
                _ = proc.wait(timeout=_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                _kill(proc.pid, signal.SIGKILL)
                _ = proc.wait()
            code = _TIMEOUT_EXIT
        code = 128 - code if code < 0 else code
        if args.name:
            _write(_job(paths, args.name, "exit"), str(code))
        return code


def _write(path: str, text: str) -> None:
    with open(path, "w") as handle:
        _ = handle.write(text)


def start(args: _Args, paths: dict[str, str]) -> int:
    """Run the command as a detached job and return at once."""
    os.makedirs(paths["jobs"], exist_ok=True)
    _ = stop(args, paths)
    for suffix in ("out", "err", "exit", "pid"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(_job(paths, args.name, suffix))
    # Present before the job is, so `alive` never reads a job just started as gone.
    _write(_job(paths, args.name, "out"), "")
    argv = [sys.executable, os.path.abspath(__file__), "run", *sys.argv[2:]]
    _ = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return 0


def _pid(args: _Args, paths: dict[str, str]) -> int | None:
    try:
        with open(_job(paths, args.name, "pid")) as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def stop(args: _Args, paths: dict[str, str]) -> int:
    pid = _pid(args, paths)
    if pid is not None and not os.path.exists(_job(paths, args.name, "exit")):
        _kill(pid, signal.SIGKILL)
    return 0


def alive(args: _Args, paths: dict[str, str]) -> int:
    """0 while the job is starting or running, 1 once it has an exit status or never
    existed."""
    if os.path.exists(_job(paths, args.name, "exit")):
        return 1
    if _pid(args, paths) is not None:
        return 0
    return 0 if os.path.exists(_job(paths, args.name, "out")) else 1


def _text(data: bytes) -> tuple[str, int]:
    """``data`` decoded, leaving off a multi-byte character cut at the end, and how many
    bytes that consumed."""
    try:
        return data.decode(), len(data)
    except UnicodeDecodeError as exc:
        if exc.start >= len(data) - 3:
            return data[: exc.start].decode(errors="replace"), exc.start
        return data.decode(errors="replace"), len(data)


def _read_from(path: str, offset: int, limit: int) -> tuple[str, int]:
    try:
        with open(path, "rb") as handle:
            _ = handle.seek(offset)
            text, used = _text(handle.read(limit))
    except OSError:
        return "", offset
    return text, offset + used


def output(args: _Args, paths: dict[str, str]) -> int:
    """A job's state and its output after the given offsets, as JSON."""
    try:
        with open(_job(paths, args.name, "exit")) as handle:
            code: int | None = int(handle.read().strip())
    except (OSError, ValueError):
        code = None
    out, out_end = _read_from(_job(paths, args.name, "out"), args.out_offset, args.limit)
    err, err_end = _read_from(_job(paths, args.name, "err"), args.err_offset, args.limit)
    json.dump(
        {
            "running": code is None and alive(args, paths) == 0,
            "exit": code,
            "out": out,
            "err": err,
            "out_end": out_end,
            "err_end": err_end,
        },
        sys.stdout,
    )
    return 0


def _split(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" not in argv:
        return argv, []
    at = argv.index("--")
    return argv[:at], argv[at + 1 :]


def main() -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("action", choices=("run", "start", "stop", "alive", "output"))
    _ = parser.add_argument("--root", default="/")
    _ = parser.add_argument("--key", default="")
    _ = parser.add_argument("--name", default="")
    _ = parser.add_argument("--uid", type=int, default=1100)
    _ = parser.add_argument("--workdir", default="")
    _ = parser.add_argument("--timeout", type=float, default=0)
    _ = parser.add_argument("--stdin", default="")
    _ = parser.add_argument("--env", action="append", default=[])
    _ = parser.add_argument("--publish", type=int, default=0)
    _ = parser.add_argument("--memory", default="")
    _ = parser.add_argument("--cpus", default="")
    _ = parser.add_argument("--pids", type=int, default=0)
    _ = parser.add_argument("--cgroup-root", default="/sys/fs/cgroup")
    _ = parser.add_argument("--out-offset", type=int, default=0)
    _ = parser.add_argument("--err-offset", type=int, default=0)
    _ = parser.add_argument("--limit", type=int, default=1 << 20)
    # A file the command may write its final working directory to, as /run/cwd.
    _ = parser.add_argument("--cwd-file", default="")
    _ = parser.add_argument("--plugin", default="")
    _ = parser.add_argument("--plugin-writable", action="store_true")
    own, command = _split(sys.argv[1:])
    args = parser.parse_args(own, namespace=_Args())
    args.argv = command
    if args.action in ("run", "start") and (not _KEY.match(args.key) or not args.argv):
        parser.error("run and start need a valid --key and a command")
    if (args.action != "run" or args.name) and not _KEY.match(args.name):
        parser.error("start, stop and alive need a valid --name")
    paths = _layout(args.root)
    actions = {"run": run, "start": start, "stop": stop, "alive": alive, "output": output}
    return actions[args.action](args, paths)


if __name__ == "__main__":
    sys.exit(main())
