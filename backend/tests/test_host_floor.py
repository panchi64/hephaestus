"""The deny floor, the fence state, and the host shell running inside the fence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import services.sandbox.fence as fence_mod
from core.config import Settings
from services.sandbox import (
    ConfinementState,
    confinement_state,
    denied_read_paths,
    is_protected_write,
    shutdown_confinement,
)
from services.sandbox.host_shell import HostShell


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
    await shutdown_confinement()
    root = tmp_path / "probe"
    root.mkdir()
    probe = await HostShell(root, default_timeout=20, max_output_chars=10_000).run("true")
    if confinement_state().state != "active" or not probe.ok:
        await shutdown_confinement()
        pytest.skip(f"no working host fence here: {confinement_state().reason}")
    yield home
    await shutdown_confinement()


# --- the floor ------------------------------------------------------------------------


def test_a_settings_list_cannot_remove_a_floor_entry(home: Path, tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", host_command_deny_read=())
    denied = denied_read_paths(settings)
    for entry in (".git-credentials", ".npmrc", ".pypirc", ".cargo/credentials"):
        assert str(home / entry) in denied
    assert str((tmp_path / "data").resolve()) in denied
    assert str(home / "Library/Application Support/Google/Chrome") in denied


def test_a_settings_list_adds_to_the_floor(home: Path, tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", host_command_deny_read=("/extra",))
    denied = denied_read_paths(settings)
    assert "/extra" in denied
    assert str(home / ".npmrc") in denied


@pytest.mark.parametrize(
    "rel",
    [
        ".zshrc",
        ".bash_profile",
        "Library/LaunchAgents/x.plist",
        ".claude/settings.json",
        ".gitconfig",
    ],
)
def test_floor_paths_are_protected_writes(home: Path, rel: str):
    assert is_protected_write(home / rel, home=home) is not None


def test_git_hooks_are_protected_anywhere(home: Path, tmp_path: Path):
    assert is_protected_write(tmp_path / "repo/.git/hooks/pre-commit", home=home) is not None


def test_ordinary_paths_are_not_protected(home: Path, tmp_path: Path):
    assert is_protected_write(home / ".config/tool/x", home=home) is None
    assert is_protected_write(tmp_path / "repo/.git/config", home=home) is None
    assert is_protected_write("relative/path", home=home) is None


# --- the state ------------------------------------------------------------------------


def test_the_state_reports_each_of_its_three_values(monkeypatch: pytest.MonkeyPatch):
    from services.sandbox.fence import HostConfinement

    monkeypatch.setattr(fence_mod, "_resolved", HostConfinement(True))
    assert confinement_state() == ConfinementState("active")
    monkeypatch.setattr(fence_mod, "_resolved", HostConfinement(False, "off", disabled=True))
    assert confinement_state() == ConfinementState("disabled_by_setting", "off")
    monkeypatch.setattr(fence_mod, "_resolved", HostConfinement(False, "no bwrap; install it"))
    assert confinement_state() == ConfinementState("unavailable", "no bwrap; install it")


async def test_a_missing_dependency_resolves_as_unavailable_with_a_fix(
    monkeypatch: pytest.MonkeyPatch,
):
    from sandbox_runtime import SandboxManager

    def unavailable(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(SandboxManager, "check_dependencies", unavailable)
    monkeypatch.setattr(fence_mod, "_resolved", None)
    resolved = await fence_mod.configure(Settings(host_command_sandbox_enabled=True))
    assert not resolved.active and not resolved.disabled
    assert "Settings" in resolved.reason


# --- the host shell under the fence ----------------------------------------------------


@pytest.mark.usefixtures("fenced")
async def test_the_fenced_shell_cannot_append_to_an_rc_file(home: Path, tmp_path: Path):
    rc = home / ".zshrc"
    _ = rc.write_text("original\n")
    root = tmp_path / "work"
    root.mkdir()
    result = await HostShell(root, default_timeout=20, max_output_chars=10_000).run(
        "echo x >> ~/.zshrc"
    )
    assert not result.ok
    assert rc.read_text() == "original\n"


@pytest.mark.usefixtures("fenced")
async def test_the_fenced_shell_can_write_elsewhere_under_home_and_keep_its_cwd(
    home: Path, tmp_path: Path
):
    root = tmp_path / "work"
    (root / "sub").mkdir(parents=True)
    shell = HostShell(root, default_timeout=20, max_output_chars=10_000)
    result = await shell.run("mkdir -p ~/.config/tmpdir && cd sub")
    assert result.ok, result.stderr
    assert (home / ".config/tmpdir").is_dir()
    assert shell.cwd == (root / "sub").resolve()


# --- cwd tracking --------------------------------------------------------------------


async def test_a_cd_through_a_symlink_out_of_the_root_is_not_tracked(tmp_path: Path):
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    shell = HostShell(root, default_timeout=20, max_output_chars=10_000)
    result = await shell.run("cd link")
    assert result.ok
    assert shell.cwd == root
