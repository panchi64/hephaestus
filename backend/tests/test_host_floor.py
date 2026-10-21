"""The deny floor, the fence state, and the host shell running inside the fence."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

import services.sandbox.fence as fence_mod
from core.config import Settings, get_settings
from services.projects.repo import commit_worktree
from services.sandbox import (
    ConfinementState,
    confinement_state,
    denied_read_paths,
    is_protected_write,
    shutdown_confinement,
)
from services.sandbox.floor import hooks_floor, source_root
from services.sandbox.host_shell import HostShell

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="seatbelt globs are macOS-only")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


@pytest.fixture
def unfenced(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("HEPHAESTUS_HOST_COMMAND_SANDBOX_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def fenced(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Path]:
    monkeypatch.setenv("HEPHAESTUS_HOST_COMMAND_SANDBOX_ENABLED", "true")
    monkeypatch.setenv("HEPHAESTUS_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    await shutdown_confinement()
    root = tmp_path / "probe"
    root.mkdir()
    probe = await _shell(root).run("true")
    if confinement_state(get_settings()).state != "active" or not probe.ok:
        reason = confinement_state(get_settings()).reason or probe.stderr
        await shutdown_confinement()
        get_settings.cache_clear()
        pytest.skip(f"no working host fence here: {reason}")
    yield home
    await shutdown_confinement()
    get_settings.cache_clear()


def _shell(root: Path) -> HostShell:
    return HostShell(root, default_timeout=30, max_output_chars=10_000)


def _work(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    return root


# --- the floor ------------------------------------------------------------------------


def test_a_settings_list_cannot_remove_a_floor_entry(home: Path, tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", host_command_deny_read=())
    denied = denied_read_paths(settings)
    for entry in (".git-credentials", ".npmrc", ".pypirc", ".cargo/credentials"):
        assert str(home / entry) in denied
    assert str(home / ".config/git/credentials") in denied
    assert str(home / ".config/google-chrome") in denied
    assert str((tmp_path / "data").resolve()) in denied
    assert str(home / "Library/Application Support/Google/Chrome") in denied


def test_a_settings_list_adds_to_the_floor(home: Path, tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", host_command_deny_read=("/extra",))
    denied = denied_read_paths(settings)
    assert "/extra" in denied
    assert str(home / ".npmrc") in denied


def test_the_read_floor_names_the_dotenv_files_and_an_outside_database(tmp_path: Path):
    outside = tmp_path / "elsewhere" / "app.db"
    settings = Settings(data_dir=tmp_path / "data", db_url=f"sqlite:///{outside}")
    denied = denied_read_paths(settings)
    assert str(Path.cwd() / ".env") in denied
    assert str(source_root() / ".env") in denied
    assert str(outside.resolve()) in denied
    assert str(source_root()) not in denied


def test_a_database_inside_the_data_dir_adds_nothing(tmp_path: Path):
    inside = tmp_path / "data" / "app.db"
    denied = denied_read_paths(Settings(data_dir=tmp_path / "data", db_url=f"sqlite:///{inside}"))
    assert str(inside.resolve()) not in denied


@pytest.mark.parametrize(
    "rel",
    [
        ".zshrc",
        ".bash_profile",
        "Library/LaunchAgents/x.plist",
        ".claude/settings.json",
        ".gitconfig",
        ".config/git/config",
    ],
)
def test_floor_paths_are_protected_writes(home: Path, rel: str):
    assert is_protected_write(home / rel, home=home) is not None


def test_git_hooks_and_config_are_protected_anywhere(home: Path, tmp_path: Path):
    assert is_protected_write(tmp_path / "repo/.git/hooks/pre-commit", home=home) is not None
    assert is_protected_write(tmp_path / "repo/.git/config", home=home) is not None


def test_ordinary_paths_are_not_protected(home: Path, tmp_path: Path):
    assert is_protected_write(home / ".config/tool/x", home=home) is None
    assert is_protected_write(tmp_path / "repo/.git/HEAD", home=home) is None
    assert is_protected_write("relative/path", home=home) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="case-insensitive volumes are macOS's")
def test_protection_ignores_case_on_macos(home: Path, tmp_path: Path):
    assert is_protected_write(home / ".ZSHENV", home=home) is not None
    assert is_protected_write(tmp_path / "repo/.GIT/HOOKS/x", home=home) is not None


def test_nested_globs_cover_repositories_below_a_root(tmp_path: Path):
    roots = (str(tmp_path),)
    assert str(tmp_path / "**/.git/hooks/**") in hooks_floor(roots, nested=True)
    assert str(tmp_path / "**/.git/config") in hooks_floor(roots, nested=True)
    assert hooks_floor(roots, nested=False) == (
        str(tmp_path / ".git/hooks"),
        str(tmp_path / ".git/config"),
    )


# --- the state ------------------------------------------------------------------------


def test_the_state_reports_each_of_its_three_values(monkeypatch: pytest.MonkeyPatch):
    from services.sandbox.fence import HostConfinement

    settings = Settings(host_command_sandbox_enabled=True)
    monkeypatch.setattr(fence_mod, "_resolved", HostConfinement(True))
    assert confinement_state(settings) == ConfinementState("active")
    monkeypatch.setattr(fence_mod, "_resolved", HostConfinement(False, "off", disabled=True))
    assert confinement_state(settings) == ConfinementState("disabled_by_setting", "off")
    monkeypatch.setattr(fence_mod, "_resolved", HostConfinement(False, "no bwrap; install it"))
    assert confinement_state(settings) == ConfinementState("unavailable", "no bwrap; install it")


def test_an_unresolved_fence_follows_the_given_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(fence_mod, "_resolved", None)
    off = confinement_state(Settings(host_command_sandbox_enabled=False))
    assert off.state == "disabled_by_setting"
    assert confinement_state(Settings(host_command_sandbox_enabled=True)).state == "unavailable"


async def test_a_missing_dependency_resolves_as_unavailable_with_a_fix(
    monkeypatch: pytest.MonkeyPatch,
):
    from sandbox_runtime import SandboxManager

    def unavailable(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(SandboxManager, "check_dependencies", unavailable)
    resolved = await fence_mod.configure(Settings(host_command_sandbox_enabled=True))
    assert not resolved.active and not resolved.disabled
    assert "Settings" in resolved.reason


# --- the host shell under the fence ----------------------------------------------------


@pytest.mark.usefixtures("fenced")
async def test_the_fenced_shell_cannot_append_to_an_rc_file(home: Path, tmp_path: Path):
    rc = home / ".zshrc"
    _ = rc.write_text("original\n")
    result = await _shell(_work(tmp_path)).run("echo x >> ~/.zshrc")
    assert not result.ok
    assert rc.read_text() == "original\n"


@pytest.mark.usefixtures("fenced")
async def test_the_fenced_shell_cannot_plant_a_launch_agent_or_touch_claude_settings(
    home: Path, tmp_path: Path
):
    shell = _shell(_work(tmp_path))
    agents = await shell.run("mkdir -p ~/Library/LaunchAgents/x")
    claude = await shell.run("mkdir -p ~/.claude/x")
    assert not agents.ok and not claude.ok
    assert not (home / "Library/LaunchAgents/x").exists()
    assert not (home / ".claude/x").exists()


@pytest.mark.usefixtures("fenced")
async def test_the_fenced_shell_cannot_read_what_the_floor_denies(home: Path, tmp_path: Path):
    _ = (home / ".npmrc").write_text("NPM-TOKEN-VALUE")
    result = await _shell(_work(tmp_path)).run("cat ~/.npmrc")
    assert not result.ok
    assert "NPM-TOKEN-VALUE" not in result.stdout


@pytest.mark.usefixtures("fenced")
async def test_the_shell_reads_ssh_but_not_the_floor_and_cannot_write_ssh(
    home: Path, tmp_path: Path
):
    (home / ".ssh").mkdir()
    _ = (home / ".ssh" / "known_hosts").write_text("host key")
    _ = (home / ".npmrc").write_text("NPM-TOKEN-VALUE")
    shell = _shell(_work(tmp_path))
    ssh = await shell.run("cat ~/.ssh/known_hosts")
    npm = await shell.run("cat ~/.npmrc")
    write = await shell.run("echo k >> ~/.ssh/authorized_keys")
    assert ssh.ok and "host key" in ssh.stdout
    assert not npm.ok and "NPM-TOKEN-VALUE" not in npm.stdout
    assert not write.ok


@pytest.mark.usefixtures("fenced")
async def test_the_fenced_shell_can_write_elsewhere_under_home_and_keep_its_cwd(
    home: Path, tmp_path: Path
):
    root = _work(tmp_path)
    (root / "sub").mkdir()
    shell = _shell(root)
    result = await shell.run("mkdir -p ~/.config/tmpdir && cd sub")
    assert result.ok, result.stderr
    assert (home / ".config/tmpdir").is_dir()
    assert shell.cwd == (root / "sub").resolve()


@pytest.mark.usefixtures("fenced")
async def test_temp_directories_work_inside_the_fence(tmp_path: Path):
    result = await _shell(_work(tmp_path)).run(
        'f=$(mktemp) && echo "$f" && rm "$f" && touch /tmp/heph-floor-$$ && rm /tmp/heph-floor-$$'
    )
    assert result.ok, result.stderr
    assert result.stdout.strip()


@darwin_only
@pytest.mark.usefixtures("fenced")
async def test_a_nested_repository_keeps_its_hooks_and_config_closed(home: Path, tmp_path: Path):
    repo = home / "proj"
    (repo / ".git" / "hooks").mkdir(parents=True)
    _ = (repo / ".git" / "config").write_text("[core]\n")
    shell = _shell(_work(tmp_path))
    hook = await shell.run("echo 'touch /tmp/pwned' > ~/proj/.git/hooks/pre-commit")
    config = await shell.run("echo '[core]' >> ~/proj/.git/config")
    other = await shell.run("echo ref > ~/proj/.git/HEAD")
    assert not hook.ok and not config.ok
    assert other.ok, other.stderr
    assert not (repo / ".git/hooks/pre-commit").exists()
    assert (repo / ".git/config").read_text() == "[core]\n"


@pytest.mark.usefixtures("fenced")
async def test_a_fenced_background_command_runs_and_is_denied_like_a_foreground_one(
    home: Path, tmp_path: Path
):
    shell = _shell(_work(tmp_path))
    ok_id = await shell.start("echo background-ran")
    bad_id = await shell.start("echo x >> ~/.zshrc")
    for command_id in (ok_id, bad_id):
        for _ in range(100):
            status = await shell.check(command_id)
            assert status is not None
            if status.status == "finished":
                break
            await asyncio.sleep(0.1)
    ok = await shell.check(ok_id)
    bad = await shell.check(bad_id)
    assert ok is not None and bad is not None
    assert "background-ran" in ok.stdout and ok.exit_code == 0
    assert bad.exit_code != 0
    assert not (home / ".zshrc").exists()
    await shell.shutdown()


# --- cwd tracking --------------------------------------------------------------------


@pytest.mark.usefixtures("unfenced")
async def test_a_cd_through_a_symlink_out_of_the_root_is_not_tracked(tmp_path: Path):
    root = _work(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    shell = _shell(root)
    result = await shell.run("cd link")
    assert result.ok
    assert shell.cwd == root


# --- backend git is pinned ------------------------------------------------------------


async def test_the_backend_does_not_run_a_hook_a_repository_names(tmp_path: Path):
    repo = tmp_path / "repo"
    hooks = repo / "evil-hooks"
    hooks.mkdir(parents=True)
    marker = tmp_path / "ran"
    hook = hooks / "pre-commit"
    _ = hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    for args in (["init", "-q"], ["config", "core.hooksPath", str(hooks)]):
        _ = subprocess.run(["git", *args], cwd=repo, check=True)
    _ = (repo / "f.txt").write_text("x")
    assert await commit_worktree(repo)
    assert not marker.exists()
