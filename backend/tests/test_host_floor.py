"""The deny floor, the fence state, and the host shell running inside the fence."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import services.sandbox.fence as fence_mod
from core.config import Settings, get_settings
from services.projects.repo import commit_worktree
from services.sandbox import (
    ConfinementState,
    SandboxResult,
    confinement_state,
    denied_read_paths,
    is_protected_write,
    resolve_confinement,
    run_on_host,
    shutdown_confinement,
)
from services.sandbox.floor import credential_read_floor, hooks_floor, source_root

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="seatbelt globs are macOS-only")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


@pytest.fixture
async def fenced(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Path]:
    monkeypatch.setenv("HEPHAESTUS_HOST_COMMAND_SANDBOX_ENABLED", "true")
    monkeypatch.setenv("HEPHAESTUS_DATA_DIR", str(tmp_path / "data"))
    Settings.settings_clear_cache()
    await shutdown_confinement()
    root = tmp_path / "probe"
    root.mkdir()
    probe = await _shell(root).run("true")
    if confinement_state(get_settings()).state != "active" or not probe.ok:
        reason = confinement_state(get_settings()).reason or probe.stderr
        await shutdown_confinement()
        Settings.settings_clear_cache()
        pytest.skip(f"no working host fence here: {reason}")
    yield home
    await shutdown_confinement()
    Settings.settings_clear_cache()


class _Host:
    """The approval-gated host command, run in ``root`` inside whatever fence is resolved."""

    def __init__(self, root: Path) -> None:
        self._root: Path = root

    async def run(self, command: str) -> SandboxResult:
        confinement = await resolve_confinement(get_settings())
        return await run_on_host(command, timeout_s=30, confinement=confinement, cwd=self._root)


def _shell(root: Path) -> _Host:
    return _Host(root)


def _work(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    return root


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
async def test_the_fenced_shell_cannot_write_ssh(home: Path, tmp_path: Path):
    (home / ".ssh").mkdir()
    write = await _shell(_work(tmp_path)).run("echo k >> ~/.ssh/authorized_keys")
    assert not write.ok
    assert not (home / ".ssh" / "authorized_keys").exists()


def _plant_credentials(home: Path, *, inline: bool) -> None:
    ssh = home / ".ssh"
    ssh.mkdir()
    for name in ("id_ed25519", "id_ed25519.pub", "known_hosts", "known_hosts.old", "config"):
        _ = (ssh / name).write_text(f"SECRET-{name}")
    (home / ".aws").mkdir()
    _ = (home / ".aws" / "credentials").write_text("SECRET-aws")
    _ = (home / ".aws" / "config").write_text("[default]\nregion=x\n")
    (home / ".config" / "gh").mkdir(parents=True)
    _ = (home / ".config/gh/config.yml").write_text("git_protocol: ssh\n")
    _ = (home / ".config/gh/hosts.yml").write_text(
        "github.com:\n  oauth_token: ghp_x\n" if inline else "github.com:\n  user: me\n"
    )
    (home / ".docker").mkdir()
    docker = (
        '{"auths": {"r.io": {"auth": "abc"}}}'
        if inline
        else '{"auths": {"r.io": {}}, "credsStore": "osxkeychain"}'
    )
    _ = (home / ".docker/config.json").write_text(docker)
    (home / ".kube").mkdir()
    kube = (
        "users:\n- name: u\n  user:\n    token: t\n"
        if inline
        else "users:\n- name: u\n  user:\n    exec:\n      command: aws\n"
    )
    _ = (home / ".kube/config").write_text(kube)


def test_the_credential_floor_denies_keys_and_inline_secrets(home: Path):
    _plant_credentials(home, inline=True)
    denied = credential_read_floor(home)
    for rel in (
        ".ssh/id_ed25519",
        ".aws/credentials",
        ".config/gh/hosts.yml",
        ".docker/config.json",
        ".kube/config",
    ):
        assert str(home / rel) in denied
    for rel in (
        ".ssh/id_ed25519.pub",
        ".ssh/known_hosts",
        ".ssh/known_hosts.old",
        ".ssh/config",
        ".aws/config",
        ".config/gh/config.yml",
    ):
        assert str(home / rel) not in denied


def test_the_credential_floor_leaves_helper_backed_configs_readable(home: Path):
    _plant_credentials(home, inline=False)
    denied = credential_read_floor(home)
    for rel in (".config/gh/hosts.yml", ".docker/config.json", ".kube/config"):
        assert str(home / rel) not in denied


def test_an_unparseable_config_is_denied(home: Path):
    (home / ".docker").mkdir()
    _ = (home / ".docker/config.json").write_text("{not json")
    (home / ".kube").mkdir()
    _ = (home / ".kube/config").write_text(": : :\n\t- [")
    denied = credential_read_floor(home)
    assert str(home / ".docker/config.json") in denied
    assert str(home / ".kube/config") in denied


@darwin_only
@pytest.mark.usefixtures("fenced")
async def test_the_keychain_daemon_stays_reachable_inside_the_fence(tmp_path: Path):
    result = await _shell(_work(tmp_path)).run("security list-keychains")
    assert result.ok, result.stderr


def test_the_keychain_folder_is_readable_for_the_shell_but_not_the_hatch(home: Path):
    keychains = str(home / "Library/Keychains")
    assert keychains not in credential_read_floor(home)
    assert "~/Library/Keychains" in Settings().host_command_deny_read


@darwin_only
@pytest.mark.usefixtures("fenced")
async def test_the_keychain_folder_is_unreadable_inside_the_fence(home: Path, tmp_path: Path):
    (home / "Library/Keychains").mkdir(parents=True)
    _ = (home / "Library/Keychains/login.keychain-db").write_text("CIPHERTEXT")
    result = await _shell(_work(tmp_path)).run("cat ~/Library/Keychains/login.keychain-db")
    assert not result.ok
    assert "CIPHERTEXT" not in result.stdout


@pytest.mark.usefixtures("fenced")
async def test_the_fenced_shell_can_write_elsewhere_under_home(home: Path, tmp_path: Path):
    result = await _shell(_work(tmp_path)).run("mkdir -p ~/.config/tmpdir")
    assert result.ok, result.stderr
    assert (home / ".config/tmpdir").is_dir()


@pytest.mark.usefixtures("fenced")
async def test_temp_directories_work_inside_the_fence(tmp_path: Path):
    result = await _shell(_work(tmp_path)).run('f=$(mktemp) && echo "$f" && rm "$f"')
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
