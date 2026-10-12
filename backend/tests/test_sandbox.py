"""The execution-sandbox capability: fail-closed detection, container hardening,
copy in/out, and the deliberate host escape hatch."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from pathlib import Path

import pytest

import services.sandbox.container as container_mod
import services.sandbox.host as host_mod
from core.config import Settings
from services.sandbox import (
    ContainerSandbox,
    HostConfinement,
    HostExecutionError,
    SandboxError,
    SandboxFile,
    SandboxResult,
    SandboxSpec,
    detect_sandbox,
    host_scratch_dir,
    run_on_host,
)
from services.sandbox.container import (
    discover_runtime,
    hardened_flags,
    prepare_workspace,
    runtime_fault_line,
    workspace_env_defaults,
)
from services.sandbox.host import _configure, confine


def _runtime_ready() -> bool:
    """A runtime binary on PATH *and* a reachable daemon — the real gate for the
    integration tests (a present CLI with a dead daemon must still skip)."""
    runtime = discover_runtime()
    if runtime is None:
        return False
    try:
        proc = subprocess.run([runtime, "version"], capture_output=True, timeout=10, check=False)
    except OSError, subprocess.SubprocessError:
        return False
    return proc.returncode == 0


# --- result/value types ------------------------------------------------------
def test_result_ok_only_when_clean_exit():
    assert SandboxResult(exit_code=0, stdout="", stderr="").ok
    assert not SandboxResult(exit_code=1, stdout="", stderr="").ok
    assert not SandboxResult(exit_code=0, stdout="", stderr="", timed_out=True).ok


# --- runtime faults are the environment's failure, not the code's ------------
def test_runtime_fault_line_matches_the_runtimes_own_errors():
    oci = (
        "OCI runtime exec failed: exec failed: unable to start container process: "
        "current working directory is outside of container mount namespace root "
        "-- possible container breakout detected"
    )
    assert runtime_fault_line(128, oci, "") == oci  # CLI printed it to stdout
    assert runtime_fault_line(128, "", oci) == oci  # or to stderr
    daemon = "Error response from daemon: No such container: odysseus-sbx-x"
    assert runtime_fault_line(125, "", f"pulled layer\n{daemon}\n") == daemon


def test_runtime_fault_line_ignores_ordinary_code_failures():
    assert runtime_fault_line(0, "OCI runtime exec failed", "") is None  # clean exit
    assert runtime_fault_line(1, "", "NameError: x is not defined") is None
    assert runtime_fault_line(127, "", "bash: line 1: nope: command not found") is None


async def test_run_in_raises_sandbox_error_on_a_runtime_fault(tmp_path, monkeypatch):
    # A daemon-level failure of the one-shot path must surface as an environment
    # problem (SandboxError → "your computer could not run the code"), never as a
    # pseudo code-failure result the model would try to fix by editing its code.
    async def fake_run_subprocess(argv, **_kwargs):
        return (
            False,
            125,
            b"",
            b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        )

    monkeypatch.setattr(container_mod, "run_subprocess", fake_run_subprocess)
    backend = ContainerSandbox(runtime="docker")

    with pytest.raises(SandboxError, match="container runtime failed"):
        await backend.run_in(tmp_path, SandboxSpec(command=["python", "-c", "1"], timeout_s=5))


# --- fail-closed detection (the one safety rule) -----------------------------
async def test_detect_disabled_returns_none():
    assert await detect_sandbox(Settings(sandbox_enabled=False)) is None


async def test_detect_returns_none_when_no_runtime(monkeypatch):
    # No backend available ⇒ capability absent, never a host fallback.
    monkeypatch.setattr(ContainerSandbox, "available", lambda self: _false())
    assert await detect_sandbox(Settings(sandbox_enabled=True)) is None


async def test_detect_returns_backend_when_available(monkeypatch):
    monkeypatch.setattr(ContainerSandbox, "available", lambda self: _true())
    sandbox = await detect_sandbox(Settings(sandbox_enabled=True))
    assert isinstance(sandbox, ContainerSandbox)


async def _false() -> bool:
    return False


async def _true() -> bool:
    return True


# --- container command construction (host shut out by construction) ----------
def test_run_argv_is_locked_down_by_default(tmp_path):
    sandbox = ContainerSandbox(runtime="docker", image="img:1")
    argv = sandbox._run_argv("docker", SandboxSpec(command=["echo", "hi"]), tmp_path)
    joined = " ".join(argv)
    # The one-shot path belongs to no workspace, so there is no allowlist to fence it
    # against and no sidecar to route through: it gets no interface at all.
    assert "--network none" in joined
    assert "--cap-drop ALL" in joined
    # Runs as the workspace's host owner, not the image's root: with all caps
    # dropped an in-container root can't write the uid-owned /work, so installs
    # fall back to the /tmp tmpfs and nothing the agent installs survives.
    assert f"--user {os.getuid()}:{os.getgid()}" in joined
    assert "--security-opt no-new-privileges" in joined
    # No read-only root. It bought nothing `--user` does not already buy — the image's
    # tree is root-owned and this box is not root — while turning every world-writable
    # scratch path into an error the agent could not act on.
    assert "--read-only" not in joined
    assert "--tmpfs /tmp:rw,size=1g" in joined
    assert "--pids-limit 1024" in joined
    assert f"{tmp_path}:/work" in joined  # copies mounted, not host files
    # The command is preserved verbatim at the tail, wrapped by the in-container
    # timeout, which the image precedes.
    assert argv[-2:] == ["echo", "hi"]
    assert argv[argv.index("img:1") + 1] == "timeout"  # enforced inside the box
    assert "--kill-after=5" in joined


def test_hardened_flags_take_a_network_name_never_a_switch(tmp_path):
    # There is no spelling of these flags that reaches the open web: `None` is no
    # interface at all, and a name is one of a workspace's `--internal` networks, whose
    # only exit is the proxy sidecar sharing it.
    def flags(network):
        return " ".join(
            hardened_flags(
                network=network,
                memory="512m",
                cpus="1.0",
                pids_limit=256,
                workdir="/work",
                mount=tmp_path,
                env={},
            )
        )

    assert "--network none" in flags(None)
    assert "--network odysseus-net-s1" in flags("odysseus-net-s1")
    assert "bridge" not in flags("odysseus-net-s1")


def test_env_is_explicit_only(tmp_path):
    sandbox = ContainerSandbox(runtime="docker")
    spec = SandboxSpec(command=["x"], env={"FOO": "bar"})
    argv = sandbox._run_argv("docker", spec, tmp_path)
    assert "--env FOO=bar" in " ".join(argv)


# --- package installs redirect to the writable workspace ---------------------
def test_workspace_env_defaults_point_under_the_workdir():
    env = workspace_env_defaults("/work")
    # Installs, caches, build temp, and HOME all land in the writable bind-mount.
    assert env["PIP_USER"] == "1"
    assert env["PYTHONUSERBASE"] == "/work/.local"
    assert env["PIP_CACHE_DIR"].startswith("/work/")
    assert env["TMPDIR"].startswith("/work/")
    # HOME is a seal-excluded scratch subdir, not the workdir itself, so $HOME
    # caches/config don't bloat the sealed archive.
    assert env["HOME"] == "/work/.home"
    # PATH is left to the configured image — forcing it would break a non-Debian
    # image's binary layout.
    assert "PATH" not in env


def test_hardened_flags_inject_install_redirects(tmp_path):
    joined = " ".join(
        hardened_flags(
            network=None,
            memory="512m",
            cpus="1.0",
            pids_limit=256,
            workdir="/work",
            mount=tmp_path,
            env={},
        )
    )
    assert "--env PIP_USER=1" in joined
    assert "--env PYTHONUSERBASE=/work/.local" in joined


def test_explicit_env_overrides_a_default(tmp_path):
    flags = hardened_flags(
        network=None,
        memory="512m",
        cpus="1.0",
        pids_limit=256,
        workdir="/work",
        mount=tmp_path,
        env={"PIP_USER": "0"},
    )
    joined = " ".join(flags)
    assert "PIP_USER=0" in joined
    assert "PIP_USER=1" not in joined  # the caller's value wins, not duplicated


def test_prepare_workspace_creates_the_scratch_dirs(tmp_path):
    # TMPDIR/HOME must pre-exist or mktemp fails (tempfile falls back to the small
    # tmpfs) and some tools refuse to start; the env points them at "<workdir>/.tmp"
    # and "<workdir>/.home", so those subdirs must be created.
    env = workspace_env_defaults("/work")
    assert env["TMPDIR"] == "/work/.tmp"
    assert env["HOME"] == "/work/.home"
    prepare_workspace(tmp_path)
    assert (tmp_path / ".tmp").is_dir()
    assert (tmp_path / ".home").is_dir()
    # Idempotent — a second call over an existing workspace is fine.
    prepare_workspace(tmp_path)
    assert (tmp_path / ".tmp").is_dir()
    assert (tmp_path / ".home").is_dir()


# --- copy in / copy out ------------------------------------------------------
def test_write_and_read_files_round_trip(tmp_path):
    spec = SandboxSpec(
        command=["x"],
        files=[SandboxFile(path="in.txt", content=b"hello")],
        outputs=["out.txt"],
    )
    ContainerSandbox._write_inputs(tmp_path, spec)
    assert (tmp_path / "in.txt").read_bytes() == b"hello"
    # Only a file that exists is read back; a named-but-missing output is skipped.
    assert ContainerSandbox._read_outputs(tmp_path, spec) == {}
    (tmp_path / "out.txt").write_bytes(b"result")
    assert ContainerSandbox._read_outputs(tmp_path, spec) == {"out.txt": b"result"}


def test_input_path_cannot_escape_the_box(tmp_path):
    spec = SandboxSpec(command=["x"], files=[SandboxFile(path="../escape", content=b"x")])
    with pytest.raises(SandboxError):
        ContainerSandbox._write_inputs(tmp_path, spec)


# --- a cancelled runtime command takes its child with it ---------------------
async def test_cancelling_a_runtime_command_kills_the_child(monkeypatch):
    # The runtime client is a child process: it does not exit because the task awaiting
    # it was cancelled. Without the kill, a bring-up cancelled mid-pull (app shutdown, a
    # test tearing its app down) leaves `docker pull` running for its full five-minute
    # budget — and they accumulate, one per cancellation, all holding the network open.
    spawned: list[asyncio.subprocess.Process] = []
    real_exec = asyncio.create_subprocess_exec

    async def capturing_exec(*argv, **kwargs):
        proc = await real_exec(*argv, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing_exec)

    task = asyncio.create_task(container_mod.run_subprocess(["sleep", "30"], timeout_s=30))
    # Bounded, and watching the task as well: a spawn that fails outright (`run_subprocess`
    # raises `SandboxError` when the exec does) never appends, and waiting on `spawned`
    # alone would hang the suite forever instead of reporting that failure.
    async with asyncio.timeout(10):
        while not spawned:
            if task.done():
                await task  # re-raises whatever run_subprocess failed with
                pytest.fail("run_subprocess returned without spawning a child")
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [child] = spawned
    # Reaped promptly rather than outliving the run — `wait` would hang out to the
    # sleep's full 30s if the child had merely been abandoned.
    await asyncio.wait_for(child.wait(), timeout=5)
    assert child.returncode is not None


async def test_force_remove_gives_up_on_a_daemon_that_never_answers(monkeypatch):
    # Removal is what boot reconciliation and the reaper both run, and a container whose
    # mount is wedged makes the daemon take the call and never answer. Unbounded, that
    # parks app startup with nothing in the log; bounded, boot moves on and the next one
    # tries again.
    spawned: list[asyncio.subprocess.Process] = []
    real_exec = asyncio.create_subprocess_exec

    async def hanging_exec(*_argv, **kwargs):
        proc = await real_exec("sleep", "30", **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", hanging_exec)

    async with asyncio.timeout(10):  # the assertion is that this returns at all
        await container_mod.force_remove_container("docker", "odysseus-sbx-wedged", timeout_s=0.1)

    [child] = spawned
    # Killed, not merely abandoned — an orphaned client per stuck container would
    # outlive every boot that hit one.
    await asyncio.wait_for(child.wait(), timeout=5)
    assert child.returncode is not None


# --- the deliberate host escape hatch ----------------------------------------
async def test_run_on_host_executes_and_reports_exit():
    ok = await run_on_host("echo hostran")
    assert ok.ok and "hostran" in ok.stdout
    bad = await run_on_host("exit 3")
    assert bad.exit_code == 3 and not bad.ok


async def test_a_host_command_runs_where_the_caller_says(tmp_path):
    # Without a `cwd` a subprocess inherits the *server process's* working directory —
    # wherever the backend was started — and every relative path in the command means
    # something among this application's own files instead.
    (tmp_path / "here.txt").write_text("found")
    result = await run_on_host("cat here.txt", cwd=tmp_path)
    assert result.ok and "found" in result.stdout


async def test_the_scratch_directory_exists_and_is_not_the_application_itself():
    # It has to be usable the moment a command is approved (nothing else creates it), and
    # it must not be the directory the server was started in, which is the source tree the
    # relative paths in an approved command used to land in.
    scratch = host_scratch_dir()
    assert scratch.is_dir()
    assert scratch != Path(os.getcwd())
    result = await run_on_host("pwd", cwd=scratch)
    assert str(scratch.resolve()) in result.stdout


def test_the_scratch_directory_is_this_user_s_alone():
    # On Linux `gettempdir()` is the shared `/tmp`, so a fixed name under it is a path any
    # local account can reach. The uid in the name keeps two accounts from contending for
    # one directory, and 0700 keeps the output of an approved command — which is the
    # operator's — from being readable by everyone else on the host.
    scratch = host_scratch_dir()
    assert str(os.getuid()) in scratch.name
    assert stat.S_IMODE(scratch.lstat().st_mode) == 0o700


def test_a_scratch_path_someone_else_planted_is_refused(monkeypatch, tmp_path):
    # `Path.mkdir(exist_ok=True)` swallows the already-there case whenever `is_dir()` is
    # true, and `is_dir()` follows symlinks — so before this, a link planted at the scratch
    # path was silently adopted, and it is both the cwd of every approved host command and
    # an entry in the fence's write allowlist.
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    planted = tmp_path / "tmproot"
    planted.mkdir()
    monkeypatch.setattr(host_mod.tempfile, "gettempdir", lambda: str(planted))
    link = host_mod.scratch_path()
    link.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(HostExecutionError, match="symlink"):
        host_scratch_dir()


def test_a_scratch_directory_this_process_owns_is_tightened_rather_than_refused(
    monkeypatch, tmp_path
):
    # The ordinary case for an existing install: our own directory, created before it was
    # created narrowly. Refusing it would break the tool over a mode we can simply fix.
    root = tmp_path / "tmproot"
    root.mkdir()
    monkeypatch.setattr(host_mod.tempfile, "gettempdir", lambda: str(root))
    host_mod.scratch_path().mkdir(mode=0o755)
    assert stat.S_IMODE(host_scratch_dir().lstat().st_mode) == 0o700


async def test_a_host_command_takes_the_environment_it_is_given(tmp_path):
    result = await run_on_host("echo $ODYSSEUS_TEST_MARK", env={"ODYSSEUS_TEST_MARK": "pinned"})
    assert "pinned" in result.stdout


async def test_host_timeout_kills_the_whole_process_group(tmp_path):
    # A child the command backgrounds must die with the timeout, not orphan.
    marker = tmp_path / "orphan-ran"
    cmd = f"( sleep 2; touch {marker} ) & wait"
    result = await run_on_host(cmd, timeout_s=0.4)
    assert result.timed_out and result.exit_code == 124
    await asyncio.sleep(2.5)  # past when an orphaned child would have written
    assert not marker.exists()  # the whole group was killed — no survivor


async def test_confinement_is_off_when_the_operator_disables_it():
    settings = Settings(host_command_sandbox_enabled=False)
    confinement = await _configure(settings)
    assert not confinement.active and "disabled" in confinement.reason


async def test_a_disabled_confinement_still_runs_the_command():
    # The inverse of the sandbox rule, on purpose: the operator approved *this* command,
    # so a missing (or switched-off) fence means it runs unconfined and says so — not
    # that an approved command silently refuses. (The project-folder shell is the other way
    # round; see `test_shell_fence.py`.)
    result = await run_on_host("echo ran", confinement=HostConfinement(False, "disabled"))
    assert result.ok and "ran" in result.stdout


async def test_the_hatch_runs_where_and_with_what_it_was_told(tmp_path):
    # `cwd` and `env` exist so the project-folder shell can share this spawn; the hatch itself
    # passes neither, and a regression that dropped them would be silent there.
    result = await run_on_host(
        "pwd; echo $ODYSSEUS_TEST_MARKER",
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "ODYSSEUS_TEST_MARKER": "carried",
        },
    )
    assert result.ok
    assert str(tmp_path.resolve()) in result.stdout
    assert "carried" in result.stdout


async def test_the_hatch_does_not_hand_the_operators_model_keys_to_the_command(monkeypatch):
    # The fence denies the paths those keys are *stored* at and says nothing about the
    # environment, so an inherited one puts them in the transcript of the single command
    # the operator approved without ever being shown that it could read them. The
    # project-folder shell already filters; the hatch is the more guarded path of the two.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-for-the-command")
    monkeypatch.setenv("ODYSSEUS_TEST_MARKER", "kept")
    result = await run_on_host("echo [$OPENAI_API_KEY] [$ODYSSEUS_TEST_MARKER]")
    assert result.ok
    assert "sk-not-for-the-command" not in result.stdout
    assert "kept" in result.stdout  # only the credentials go, not the whole environment


async def test_confining_one_command_leaves_what_every_other_one_reaches_alone(monkeypatch):
    # One proxy serves every confined process here, and it filters each request against
    # the *process-global* configuration. So teaching it a caller's domains would widen
    # the fence around everything else running at that moment — another conversation's
    # background server included — and leave it widened after this command exited: a
    # per-call approval turned into a process-wide standing grant.
    from sandbox_runtime import (
        FilesystemConfig,
        NetworkConfig,
        SandboxManager,
        SandboxRuntimeConfig,
    )
    from sandbox_runtime import manager as runtime_manager

    seen: list = []

    async def wrap(command, *, custom_config=None, **_kwargs):
        # Stubbed rather than run: what a real wrap produces is seatbelt's or bubblewrap's
        # business, and only on the machines that have them.
        seen.append(custom_config)
        return command

    monkeypatch.setattr(SandboxManager, "wrap_with_sandbox", wrap)
    before = SandboxManager.get_config()
    SandboxManager.update_config(
        SandboxRuntimeConfig(
            network=NetworkConfig(allowed_domains=["pypi.org"], allow_local_binding=True),
            filesystem=FilesystemConfig(deny_read=["/keys"], allow_write=["/tmp"]),
        )
    )
    try:
        await confine(
            "curl https://files.example.com",
            allowed_domains=["files.example.com"],
            allow_write=["/work"],
            deny_read=["/keys"],
        )
        # The filesystem half is per-call, because the OS wrapper is what enforces it...
        [config] = seen
        assert config.filesystem.allow_write == ["/work"]
        # ...and the installation's allowlist is still the only thing the proxy will
        # filter against for anything else on this host.
        assert SandboxManager.get_config().network.allowed_domains == ["pypi.org"]
    finally:
        # Restored through the module global rather than `update_config`, which has no way
        # to say "there was no configuration here".
        runtime_manager._config = before


async def test_confinement_denies_reading_the_data_directory(tmp_path):
    # The one path whose exposure would undo at-rest encryption wholesale: the vault,
    # the sealed workspaces and the database all live under it.
    settings = Settings(data_dir=tmp_path)
    confinement = await _configure(settings)
    if not confinement.active:
        pytest.skip(f"no host sandbox primitive here: {confinement.reason}")
    secret = tmp_path / "keyfile.json"
    secret.write_text("SEALEDKEYMATERIAL")
    result = await run_on_host(f"cat {secret}", confinement=confinement)
    assert "SEALEDKEYMATERIAL" not in result.stdout


# --- container integration (only when a real runtime is present) -------------
@pytest.mark.container
@pytest.mark.skipif(not _runtime_ready(), reason="no usable container runtime")
async def test_container_runs_python_in_isolation():
    sandbox = ContainerSandbox()
    result = await sandbox.run(SandboxSpec(command=["python", "-c", "print(6 * 7)"], timeout_s=60))
    assert result.ok
    assert result.stdout.strip() == "42"


@pytest.mark.container
@pytest.mark.skipif(not _runtime_ready(), reason="no usable container runtime")
async def test_the_one_shot_path_has_no_interface_at_all():
    # This backend's own path is a throwaway run over a throwaway directory: no network
    # object, so not even a name to resolve. A workspace that needs an exit gets one
    # through a session and its proxy — see `tests/test_sandbox_session.py`.
    sandbox = ContainerSandbox()
    probe = "import socket; socket.gethostbyname('pypi.org'); print('reached')"
    result = await sandbox.run(SandboxSpec(command=["python", "-c", probe], timeout_s=60))
    assert not result.ok  # no route, no DNS — the lookup raises and exits non-zero
