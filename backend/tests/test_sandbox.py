"""The execution-sandbox value types and path helpers, and the deliberate host escape
hatch."""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from pathlib import Path

import pytest

import services.sandbox.fence as host_mod
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
from services.sandbox.fence import configure, confine


def test_contained_path_resolves_inside_the_root(tmp_path: Path):
    assert contained_path(tmp_path, "a/b.txt") == tmp_path.resolve() / "a" / "b.txt"


@pytest.mark.parametrize("relpath", ["../escape", "a/../../escape", "/etc/passwd"])
def test_contained_path_refuses_an_escape(tmp_path: Path, relpath: str):
    with pytest.raises(SandboxError, match="escapes"):
        _ = contained_path(tmp_path, relpath)


def test_contained_path_refuses_a_symlink_out_of_the_root(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(SandboxError):
        _ = contained_path(root, "link/x")


def test_contained_file_answers_none_instead_of_raising(tmp_path: Path):
    _ = (tmp_path / "here.txt").write_text("x")
    (tmp_path / "dir").mkdir()
    assert contained_file(tmp_path, "here.txt") == tmp_path.resolve() / "here.txt"
    assert contained_file(tmp_path, "dir") is None
    assert contained_file(tmp_path, "missing.txt") is None
    assert contained_file(tmp_path, "../here.txt") is None
    assert contained_file(tmp_path, "/etc/passwd") is None
    assert contained_file(tmp_path, "") is None
    assert contained_file(tmp_path, "a\0b") is None


def test_result_ok_only_when_clean_exit():
    assert SandboxResult(exit_code=0, stdout="", stderr="").ok
    assert not SandboxResult(exit_code=1, stdout="", stderr="").ok
    assert not SandboxResult(exit_code=0, stdout="", stderr="", timed_out=True).ok


async def test_run_on_host_executes_and_reports_exit():
    ok = await run_on_host("echo hostran")
    assert ok.ok and "hostran" in ok.stdout
    bad = await run_on_host("exit 3")
    assert bad.exit_code == 3 and not bad.ok


async def test_a_host_command_runs_where_the_caller_says(tmp_path: Path):
    # Without a `cwd` the subprocess inherits the server's working directory, so relative
    # paths would resolve among the application's own files.
    _ = (tmp_path / "here.txt").write_text("found")
    result = await run_on_host("cat here.txt", cwd=tmp_path)
    assert result.ok and "found" in result.stdout


async def test_the_scratch_directory_exists_and_is_not_the_application_itself():
    # Nothing else creates it, so it must be usable the moment a command is approved.
    scratch = host_scratch_dir()
    assert scratch.is_dir()
    assert scratch != Path(os.getcwd())
    result = await run_on_host("pwd", cwd=scratch)
    assert str(scratch.resolve()) in result.stdout


def test_the_scratch_directory_is_this_user_s_alone():
    # On Linux `gettempdir()` is the shared `/tmp`: the uid in the name avoids contention
    # between accounts, and 0700 keeps approved-command output unreadable to them.
    scratch = host_scratch_dir()
    assert str(os.getuid()) in scratch.name
    assert stat.S_IMODE(scratch.lstat().st_mode) == 0o700


def test_a_scratch_path_someone_else_planted_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # `mkdir(exist_ok=True)` accepts a symlink to a directory, and the scratch path is both
    # the cwd of every approved host command and a fence write-allowlist entry.
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    planted = tmp_path / "tmproot"
    planted.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(planted))
    link = host_mod.scratch_path()
    link.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(HostExecutionError, match="symlink"):
        _ = host_scratch_dir()


def test_a_scratch_directory_this_process_owns_is_tightened_rather_than_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # An existing install's own directory predates the 0700 mode; fix it rather than refuse.
    root = tmp_path / "tmproot"
    root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))
    host_mod.scratch_path().mkdir(mode=0o755)
    assert stat.S_IMODE(host_scratch_dir().lstat().st_mode) == 0o700


async def test_a_host_command_takes_the_environment_it_is_given():
    result = await run_on_host("echo $HEPHAESTUS_TEST_MARK", env={"HEPHAESTUS_TEST_MARK": "pinned"})
    assert "pinned" in result.stdout


async def test_host_timeout_kills_the_whole_process_group(tmp_path: Path):
    # A child the command backgrounds must die with the timeout, not orphan.
    marker = tmp_path / "orphan-ran"
    cmd = f"( sleep 2; touch {marker} ) & wait"
    result = await run_on_host(cmd, timeout_s=0.4)
    assert result.timed_out and result.exit_code == 124
    await asyncio.sleep(2.5)  # past when an orphaned child would have written
    assert not marker.exists()


async def test_confinement_is_off_when_the_operator_disables_it():
    settings = Settings(host_command_sandbox_enabled=False)
    confinement = await configure(settings)
    assert not confinement.active and "disabled" in confinement.reason


async def test_a_disabled_confinement_still_runs_the_command():
    # Unlike the sandbox, an approved host command runs unconfined when the fence is
    # missing or off. The project-folder shell refuses instead (`test_shell_fence.py`).
    result = await run_on_host("echo ran", confinement=HostConfinement(False, "disabled"))
    assert result.ok and "ran" in result.stdout


async def test_the_hatch_runs_where_and_with_what_it_was_told(tmp_path: Path):
    # The hatch itself passes neither, so dropping `cwd`/`env` would only break the shared
    # project-folder shell spawn.
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


async def test_the_hatch_does_not_hand_the_operators_model_keys_to_the_command(
    monkeypatch: pytest.MonkeyPatch,
):
    # The fence covers where keys are stored, not the environment, so an inherited key
    # would reach the transcript of the approved command.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-for-the-command")
    monkeypatch.setenv("HEPHAESTUS_TEST_MARKER", "kept")
    result = await run_on_host("echo [$OPENAI_API_KEY] [$HEPHAESTUS_TEST_MARKER]")
    assert result.ok
    assert "sk-not-for-the-command" not in result.stdout
    assert "kept" in result.stdout  # only the credentials go, not the whole environment


async def test_confining_one_command_leaves_what_every_other_one_reaches_alone(
    monkeypatch: pytest.MonkeyPatch,
):
    # One proxy filters every confined process against the process-global configuration,
    # so adding a caller's domains would turn a per-call approval into a standing grant.
    from sandbox_runtime import (
        FilesystemConfig,
        NetworkConfig,
        SandboxManager,
        SandboxRuntimeConfig,
    )

    seen: list[SandboxRuntimeConfig | None] = []
    # Stubbed so nothing leaks into the process-global configuration other tests share.
    installed = SandboxRuntimeConfig(
        network=NetworkConfig(allowed_domains=["pypi.org"], allow_local_binding=True),
        filesystem=FilesystemConfig(deny_read=["/keys"], allow_write=["/tmp"]),
    )
    replaced: list[SandboxRuntimeConfig] = []

    def get_config() -> SandboxRuntimeConfig:
        return installed

    def update_config(new_config: SandboxRuntimeConfig) -> None:
        replaced.append(new_config)

    async def initialize(runtime_config: SandboxRuntimeConfig, *_args: object) -> None:
        # The runtime's other writer of the installed configuration.
        replaced.append(runtime_config)

    async def wrap(
        command: str, *, custom_config: SandboxRuntimeConfig | None = None, **_kwargs: object
    ) -> str:
        # Stubbed: a real wrap needs seatbelt or bubblewrap.
        seen.append(custom_config)
        return command

    monkeypatch.setattr(SandboxManager, "wrap_with_sandbox", wrap)
    monkeypatch.setattr(SandboxManager, "get_config", get_config)
    monkeypatch.setattr(SandboxManager, "update_config", update_config)
    monkeypatch.setattr(SandboxManager, "initialize", initialize)

    _ = await confine(
        "curl https://files.example.com",
        allowed_domains=["files.example.com"],
        allow_write=["/work"],
        deny_read=["/keys"],
    )
    [config] = seen
    assert config is not None
    assert config.filesystem.allow_write == ["/work"]
    assert replaced == []
    assert SandboxManager.get_config() is installed
    assert installed.network.allowed_domains == ["pypi.org"]


async def test_confinement_denies_reading_the_data_directory(tmp_path: Path):
    # Enabled explicitly: conftest switches host confinement off for the rest of the suite.
    settings = Settings(data_dir=tmp_path, host_command_sandbox_enabled=True)
    from sandbox_runtime import SandboxManager

    try:
        confinement = await configure(settings)
        if not confinement.active:
            pytest.skip(f"no host sandbox primitive here: {confinement.reason}")
        secret = tmp_path / "keyfile.json"
        _ = secret.write_text("SEALEDKEYMATERIAL")
        result = await run_on_host(f"cat {secret}", confinement=confinement)
        assert "SEALEDKEYMATERIAL" not in result.stdout
    finally:
        # The runtime is process-global; reset it for the next test on this worker.
        await SandboxManager.reset()
