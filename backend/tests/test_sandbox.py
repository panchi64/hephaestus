"""The execution-sandbox value types and path helpers, and the deliberate host escape
hatch."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

import services.sandbox.host as host_mod
from core.config import Settings
from services.sandbox import (
    HostConfinement,
    HostExecutionError,
    SandboxError,
    SandboxResult,
    host_scratch_dir,
    run_on_host,
)
from services.sandbox.base import contained_file, contained_path
from services.sandbox.host import _configure, confine


# --- paths stay inside the workspace ----------------------------------------
def test_contained_path_resolves_inside_the_root(tmp_path):
    assert contained_path(tmp_path, "a/b.txt") == tmp_path.resolve() / "a" / "b.txt"


@pytest.mark.parametrize("relpath", ["../escape", "a/../../escape", "/etc/passwd"])
def test_contained_path_refuses_an_escape(tmp_path, relpath):
    with pytest.raises(SandboxError, match="escapes"):
        contained_path(tmp_path, relpath)


def test_contained_path_refuses_a_symlink_out_of_the_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(SandboxError):
        contained_path(root, "link/x")


def test_contained_file_answers_none_instead_of_raising(tmp_path):
    (tmp_path / "here.txt").write_text("x")
    (tmp_path / "dir").mkdir()
    assert contained_file(tmp_path, "here.txt") == tmp_path.resolve() / "here.txt"
    assert contained_file(tmp_path, "dir") is None
    assert contained_file(tmp_path, "missing.txt") is None
    assert contained_file(tmp_path, "../here.txt") is None
    assert contained_file(tmp_path, "/etc/passwd") is None
    assert contained_file(tmp_path, "") is None
    assert contained_file(tmp_path, "a\0b") is None


# --- result/value types ------------------------------------------------------
def test_result_ok_only_when_clean_exit():
    assert SandboxResult(exit_code=0, stdout="", stderr="").ok
    assert not SandboxResult(exit_code=1, stdout="", stderr="").ok
    assert not SandboxResult(exit_code=0, stdout="", stderr="", timed_out=True).ok


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
    result = await run_on_host("echo $HEPHAESTUS_TEST_MARK", env={"HEPHAESTUS_TEST_MARK": "pinned"})
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
        "pwd; echo $HEPHAESTUS_TEST_MARKER",
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "HEPHAESTUS_TEST_MARKER": "carried",
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
    monkeypatch.setenv("HEPHAESTUS_TEST_MARKER", "kept")
    result = await run_on_host("echo [$OPENAI_API_KEY] [$HEPHAESTUS_TEST_MARKER]")
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
