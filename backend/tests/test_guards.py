"""The guard: which calls always ask, and which look-alikes pass.

The table is the contract. A caught row names the shape of the mistake it stands for; a
passing row is a neighbour that has to keep working.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from core.auth import AuthManager
from core.config import Settings
from core.exceptions import PermissionDeniedError
from services.approval_grants import grantable
from services.home.backend import Mount
from services.home.paths import SCRATCH, Place
from services.home.table import RunHome
from services.permissions.guards import GUARD_KEY, guard, is_guarded, require_operator, unconfined

from .guard_support import FenceState, set_fence

API = Place("projects", "api")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def home(tmp_path: Path) -> RunHome:
    scratch = tmp_path / "scratch"
    api = tmp_path / "api"
    scratch.mkdir()
    api.mkdir()
    return RunHome(
        mounts=(Mount(SCRATCH, scratch, writable=True), Mount(API, api, writable=True)),
        default=SCRATCH,
        key="t",
    )


def shell(command: str, *, reach: str = "offline") -> dict[str, object]:
    return {"command": command, "reach": reach}


def reason_for(
    command: str, settings: Settings, home: RunHome | None, *, reach: str = "offline"
) -> str | None:
    return guard("shell_run_command", shell(command, reach=reach), home=home, settings=settings)


CRITICAL_REMOVALS = [
    "rm -rf /",
    "rm -rf /*",
    "rm -rf ~",
    "rm -rf ~/",
    "rm -rf ~/*",
    "rm -r -f /",
    "rm -rf -- /",
    'rm -rf "$HOME"',
    "rm -rf ${HOME}/",
    "rm -rf '~'",
    "sudo rm -rf /",
    "sudo -u root rm -rf ~",
    "env FOO=1 rm -rf ~",
    "command rm -rf ~",
    "nohup rm -rf ~",
    "cd /tmp && rm -rf ~",
    "ls; rm -rf /",
    "bash -c 'rm -rf ~'",
    'sh -c "sudo rm -rf /"',
    "rmdir ~",
    "unlink ~",
    "shred -u /",
    "find / -delete",
    "find ~ -delete",
    "find -L ~ -type f -delete",
    "find ~ -name x -exec rm -rf {} +",
    "git clean -fdx",
    "git clean -x -d -f",
    "git -C /tmp clean -fdx",
    "git clean -fdx ~",
    "rm -rf /home/agent",
    "rm -rf ~/projects",
    "rm -rf ~/projects/api",
    "rm -rf ~/scratch",
    "rm -rf /work",
]

PASSING_REMOVALS = [
    "rm -rf ./build",
    "rm -rf build",
    "rm -rf ~/proj/tmp",
    "rm -rf /tmp/x",
    "rm -rf ~/projects/api/build",
    "rm -rf ~/scratch/out/*",
    "rm file.txt",
    "rm -rf $SOMETHING",
    "git clean -fd",
    "git clean -fdxn",
    "git clean -fdx --dry-run",
    "git clean -fdx build",
    "find . -delete",
    "find ./build -delete",
    "find / -name x",
    "find ~ -name '*.pyc'",
    "ls ~",
    "ls /",
    "echo rm -rf /",
    "grep -r 'rm -rf ~' .",
]

PROTECTED_WRITES = [
    "echo x >> ~/.zshrc",
    "echo x > ~/.zprofile",
    'echo x > "$HOME/.bashrc"',
    "echo x > ~/.gitconfig",
    "echo x > ~/.claude/settings.json",
    "echo x > ~/Library/LaunchAgents/a.plist",
    "echo x > .git/hooks/pre-commit",
    "echo x > /repo/.git/config",
    "echo x >| /repo/.git/hooks/post-commit",
    "tee ~/.bashrc",
    "echo a | tee -a ~/.zshenv",
    "cp a ~/.zshrc",
    "mv a ~/.profile",
    "dd if=a of=~/.zshrc",
    "touch ~/.zshrc",
    "rm ~/.zshrc",
    "bash -c 'echo x > ~/.zshrc'",
    "cat <<EOF > ~/.zshrc\nx\nEOF",
]

PASSING_WRITES = [
    "echo x > ~/.zshrc.bak",
    "echo x > ~/.config/heph-test",
    "echo x > notes.md",
    "echo x > ./out/.zshrc",
    "cat ~/.zshrc",
    "cp ~/.zshrc ./backup",
    "cp a b",
    "mkdir ~/.config/heph-test",
    "echo x > /tmp/y",
    "git config user.name x",
    "git commit -m 'edit .git/hooks docs'",
]


class TestCriticalRemoval:
    @pytest.mark.parametrize("command", CRITICAL_REMOVALS)
    def test_caught(self, command: str, settings: Settings, home: RunHome):
        assert reason_for(command, settings, home) is not None, command

    @pytest.mark.parametrize("command", PASSING_REMOVALS)
    def test_passing(self, command: str, settings: Settings, home: RunHome):
        assert reason_for(command, settings, home) is None, command

    def test_a_project_folder_by_its_host_path(
        self, settings: Settings, home: RunHome, tmp_path: Path
    ):
        assert reason_for(f"rm -rf {tmp_path / 'api'}", settings, home) is not None
        assert reason_for(f"rm -rf {tmp_path / 'api'}/*", settings, home) is not None
        assert reason_for(f"rm -rf {tmp_path / 'api'}/build", settings, home) is None

    def test_the_data_folder(self, settings: Settings):
        assert reason_for(f"rm -rf {settings.data_dir}", settings, None) is not None
        assert reason_for(f"rm -rf {settings.data_dir}/*", settings, None) is not None

    def test_the_real_home_folder(self, settings: Settings):
        assert reason_for(f"rm -rf {Path.home()}", settings, None) is not None

    def test_no_home_still_catches_the_unambiguous_ones(self, settings: Settings):
        assert reason_for("rm -rf ~", settings, None) is not None
        assert reason_for("rm -rf ./build", settings, None) is None

    def test_the_reason_names_the_path(self, settings: Settings, home: RunHome):
        reason = reason_for("rm -rf ~/", settings, home)
        assert reason is not None
        assert "home folder" in reason
        assert "no standing approval" in reason

    def test_every_executing_shell_tool_is_covered(self, settings: Settings, home: RunHome):
        for tool in ("shell_run_command", "shell_start_command", "code_run_host_command"):
            args: dict[str, object] = {"command": "rm -rf ~", "explanation": "x"}
            assert guard(tool, args, home=home, settings=settings) is not None, tool

    def test_other_tools_are_not_commands(self, settings: Settings, home: RunHome):
        args: dict[str, object] = {"command": "rm -rf ~"}
        assert guard("mail_send", args, home=home, settings=settings) is None


class TestProtectedWrites:
    @pytest.mark.parametrize("command", PROTECTED_WRITES)
    def test_caught(self, command: str, settings: Settings, home: RunHome):
        assert reason_for(command, settings, home) is not None, command

    @pytest.mark.parametrize("command", PASSING_WRITES)
    def test_passing(self, command: str, settings: Settings, home: RunHome):
        assert reason_for(command, settings, home) is None, command

    def test_the_data_folder_is_protected_but_a_mount_inside_it_is_not(self, tmp_path: Path):
        settings = Settings(data_dir=tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        home = RunHome(mounts=(Mount(SCRATCH, scratch, writable=True),), default=SCRATCH, key="t")
        assert reason_for(f"echo x > {tmp_path}/app.db", settings, home) is not None
        assert reason_for(f"echo x > {scratch}/notes.md", settings, home) is None

    def test_git_internals_are_never_exempt(self, settings: Settings, home: RunHome):
        assert reason_for("git config --local x y > .git/config", settings, home) is not None


class TestFileTools:
    def args(self, path: str) -> dict[str, object]:
        return {"path": path, "content": "x"}

    @pytest.mark.parametrize("tool", ["files_write_file", "files_edit_file"])
    def test_a_hook_or_a_repo_config_is_caught(self, tool: str, settings: Settings, home: RunHome):
        for path in (
            "~/projects/api/.git/hooks/pre-commit",
            "~/projects/api/.git/config",
            "projects/api/.git/hooks/x",
        ):
            assert guard(tool, self.args(path), home=home, settings=settings) is not None, path

    def test_a_relative_path_lands_in_the_default_place(self, settings: Settings, home: RunHome):
        args = self.args(".git/config")
        assert guard("files_write_file", args, home=home, settings=settings) is not None

    def test_a_directory_in_a_hooks_folder_is_caught(self, settings: Settings, home: RunHome):
        for path in ("~/projects/api/.git/hooks", "~/projects/api/.git/hooks/sub"):
            args = self.args(path)
            assert guard("files_create_directory", args, home=home, settings=settings), path
        other = self.args("~/projects/api/.git/refs")
        assert guard("files_create_directory", other, home=home, settings=settings) is None

    def test_ordinary_files_and_reads_pass(self, settings: Settings, home: RunHome):
        for path in ("~/projects/api/src/a.py", "~/scratch/notes.md", "notes.md", ".gitignore"):
            assert guard("files_write_file", self.args(path), home=home, settings=settings) is None
        read = self.args("~/projects/api/.git/config")
        assert guard("files_read_file", read, home=home, settings=settings) is None

    def test_a_path_outside_the_home_is_not_this_guards_to_judge(
        self, settings: Settings, home: RunHome
    ):
        assert guard("files_write_file", self.args("/etc/x"), home=home, settings=settings) is None

    def test_no_home_means_no_verdict(self, settings: Settings):
        args = self.args("~/projects/api/.git/config")
        assert guard("files_write_file", args, home=None, settings=settings) is None


class TestTheFence:
    def fence(self, monkeypatch: pytest.MonkeyPatch, state: FenceState) -> None:
        set_fence(monkeypatch, state, "install bubblewrap")

    def test_an_unavailable_fence_stops_a_host_command(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings, home: RunHome
    ):
        self.fence(monkeypatch, "unavailable")
        reason = reason_for("make install", settings, home, reach="host")
        assert reason is not None
        assert "install bubblewrap" in reason
        started = guard(
            "shell_start_command", shell("make", reach="host"), home=home, settings=settings
        )
        assert started is not None
        host: dict[str, object] = {"command": "ls", "explanation": "look"}
        assert guard("code_run_host_command", host, home=home, settings=settings) is not None

    def test_a_vm_command_does_not_depend_on_the_host_fence(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings, home: RunHome
    ):
        self.fence(monkeypatch, "unavailable")
        assert reason_for("make install", settings, home, reach="offline") is None
        assert reason_for("make install", settings, home, reach="network") is None

    @pytest.mark.parametrize("state", ["active", "disabled_by_setting"])
    def test_a_fence_that_is_up_or_switched_off_is_not_a_guard(
        self, state: FenceState, monkeypatch: pytest.MonkeyPatch, settings: Settings, home: RunHome
    ):
        self.fence(monkeypatch, state)
        assert reason_for("make install", settings, home, reach="host") is None
        host: dict[str, object] = {"command": "ls", "explanation": "look"}
        assert guard("code_run_host_command", host, home=home, settings=settings) is None


class TestTheWarning:
    def test_a_host_command_without_a_fence_carries_the_reason(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ):
        set_fence(monkeypatch, "unavailable", "no bwrap")
        host: dict[str, object] = {"command": "ls"}
        assert unconfined("code_run_host_command", host, settings=settings) == "no bwrap"
        assert unconfined("shell_run_command", shell("ls", reach="host"), settings=settings)

    def test_a_disabled_fence_warns_too(self, monkeypatch: pytest.MonkeyPatch, settings: Settings):
        set_fence(monkeypatch, "disabled_by_setting", "disabled by configuration")
        assert unconfined("code_run_host_command", {}, settings=settings) == (
            "disabled by configuration"
        )

    def test_nothing_to_warn_about_when_active_or_in_the_vm(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ):
        set_fence(monkeypatch, "active")
        assert unconfined("code_run_host_command", {}, settings=settings) is None
        set_fence(monkeypatch, "unavailable", "x")
        assert unconfined("shell_run_command", shell("ls"), settings=settings) is None


class TestOnceOnly:
    def test_a_guarded_call_offers_no_grant(self):
        assert not grantable("shell_run_command", {GUARD_KEY: "deletes your home folder"})
        assert is_guarded({GUARD_KEY: "x"})

    def test_an_ordinary_call_is_grantable(self):
        assert grantable("shell_run_command", {})
        assert grantable("shell_run_command")
        assert not is_guarded({"explanation": "x"})
        assert not is_guarded(None)

    def test_the_tools_that_were_always_once_only_still_are(self):
        assert not grantable("plan_submit")
        assert not grantable("code_request_egress", {})


def _request(app: FastAPI, authorization: str | None = None, cookie: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if cookie is not None:
        headers.append((b"cookie", f"hephaestus_session={cookie}".encode()))
    return Request({"type": "http", "headers": headers, "app": app})


class TestRequireOperator:
    def app(self, *, auth: bool) -> tuple[FastAPI, str]:
        app = FastAPI()
        manager = AuthManager()
        app.state.settings = SimpleNamespace(auth_enabled=auth)
        app.state.auth_manager = manager
        return app, manager.issue()

    def test_the_operator_session_passes(self):
        app, token = self.app(auth=True)
        require_operator(_request(app, authorization=f"Bearer {token}"))

    def test_an_api_token_is_refused(self):
        app, _ = self.app(auth=True)
        with pytest.raises(PermissionDeniedError):
            require_operator(_request(app, authorization="Bearer odyt_abc123"))

    def test_no_credential_is_refused(self):
        app, _ = self.app(auth=True)
        with pytest.raises(PermissionDeniedError):
            require_operator(_request(app))

    def test_everyone_is_the_operator_when_auth_is_off(self):
        app, _ = self.app(auth=False)
        require_operator(_request(app, authorization="Bearer odyt_abc123"))
