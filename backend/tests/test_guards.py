"""The guard: which calls always ask, and which look-alikes pass.

The table is the contract. A caught row names the shape of the mistake it stands for; a
passing row is a neighbour that has to keep working. Rows run in the VM reading unless the
table says host, and from the home's default place unless a working directory is given.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import Settings
from services.approval_grants import grantable
from services.home.backend import Mount
from services.home.paths import SCRATCH, Place
from services.home.table import RunHome
from services.permissions.guards import (
    GUARD_KEY,
    guard,
    guard_of_approval,
    is_guarded,
    unconfined,
)

from .guard_support import FenceState, set_fence

API = Place("projects", "api")
IN_API = "/home/agent/projects/api"


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
    command: str,
    settings: Settings,
    home: RunHome | None,
    *,
    reach: str = "offline",
    cwd: str | None = None,
) -> str | None:
    args = shell(command, reach=reach)
    return guard("shell_run_command", args, home=home, settings=settings, cwd=cwd)


# Each row: the command, then the working directory it runs from (None: the default place).
REMOVALS_CAUGHT: list[tuple[str, str | None]] = [
    ("rm -rf /", None),
    ("rm -rf /*", None),
    ("rm -rf ~", None),
    ("rm -rf ~/", None),
    ("rm -rf ~/*", None),
    ("rm -rf ~//", None),
    ("rm -r -f /", None),
    ("rm -rf -- /", None),
    ('rm -rf "$HOME"', None),
    ("rm -rf ${HOME}/", None),
    ("rm -rf ${HOME:?}", None),
    ("rm -rf ${HOME%/}", None),
    ('rm -rf "${HOME}"', None),
    ("rm -rf '~'", None),
    ("sudo rm -rf /", None),
    ("sudo -u root rm -rf ~", None),
    ("env FOO=1 rm -rf ~", None),
    ("command rm -rf ~", None),
    ("nohup rm -rf ~", None),
    ("cd /tmp && rm -rf ~", None),
    ("ls; rm -rf /", None),
    ("rmdir ~", None),
    ("unlink ~", None),
    ("shred -u /", None),
    ("find / -delete", None),
    ("find ~ -delete", None),
    ("find -L ~ -type f -delete", None),
    ("find ~ -name x -exec rm -rf {} +", None),
    ("rm -rf /home/agent", None),
    ("rm -rf ~/projects", None),
    ("rm -rf ~/projects/api", None),
    ("rm -rf {~,/tmp/x}", None),
    # compound shapes: every command node counts, at any depth
    ("(rm -rf ~)", None),
    ("{ rm -rf ~; }", None),
    ("if true; then rm -rf ~; fi", None),
    ("while true; do rm -rf ~; done", None),
    ("for i in 1 2; do rm -rf ~; done", None),
    ("! rm -rf ~", None),
    ("f() { rm -rf ~; }; f", None),
    ("echo $(rm -rf ~)", None),
    ("true && (cd / && rm -rf *)", None),
    # quoted and backslashed command names
    ("\\rm -rf ~", None),
    ('"rm" -rf ~', None),
    ("'rm' -rf ~", None),
    ("r\\m -rf ~", None),
    ("/bin/rm -rf ~", None),
    # nested shells
    ("bash -c 'rm -rf ~'", None),
    ('sh -c "sudo rm -rf /"', None),
    ('bash -c -- "rm -rf ~"', None),
    ('eval "rm -rf ~"', None),
    ('bash <<< "rm -rf ~"', None),
    ("echo 'rm -rf ~' | sh", None),
    ("echo 'rm -rf ~' | bash -s", None),
    ("bash <<EOF\nrm -rf ~\nEOF", None),
    # a parent of a critical path is critical
    ("rm -rf ~/..", None),
    ("rm -rf /Users", None),
    ("rm -rf /home", None),
    ("rm -rf ~/../..", None),
    # relative targets against the working directory
    ("rm -rf *", IN_API),
    ("rm -rf .", IN_API),
    ("rm -rf ./", IN_API),
    ("rm -rf ./*", IN_API),
    ("rm -rf ..", IN_API),
    ("cd ~ && rm -rf *", IN_API),
    ("cd / && rm -rf *", IN_API),
    ("cd .. && rm -rf *", IN_API),
    ("cd /home/agent/projects && rm -rf *", IN_API),
    ("rm -rf *", "/home/agent"),
    # git clean in every spelling
    ("git clean -fdx", None),
    ("git clean -x -d -f", None),
    ("git clean -ffdx", None),
    ("git clean -fdx .", None),
    ("git clean -fdx -- .", None),
    ("git clean -fdx :/", None),
    ("git --no-pager clean -fdx", None),
    ("git -C . clean -fdx", None),
    ("git -c a=b clean -fdx", None),
    ("git --git-dir=/x/.git clean -fdx", None),
    ("git -C /tmp clean -fdx", None),
    ("git clean -fdx ~", None),
    # a part that cannot be read, in a command that deletes
    ("rm -rf $(echo /)", None),
    ("rm -rf $SOMETHING", None),
    ('bash -c "rm -rf $X"', None),
    ("rm -rf ~ &&&& x", None),
]

REMOVALS_PASSING: list[tuple[str, str | None]] = [
    ("rm -rf ./build", None),
    ("rm -rf build", None),
    ("rm -rf ~/proj/tmp", None),
    ("rm -rf /tmp/x", None),
    ("rm -rf ~/projects/api/build", None),
    ("rm file.txt", None),
    ("rm -rf $DIR/build", None),
    ("rm -rf node_modules dist", IN_API),
    ("rm -rf build/*", IN_API),
    ("cd src && rm -rf *", IN_API),
    ("rm -rf *", None),
    ("rm -rf .", None),
    ("git clean -fd", None),
    ("git clean -fdxn", None),
    ("git clean -fdx --dry-run", None),
    ("git clean -fdx build", None),
    ("find . -delete", None),
    ("find ./build -delete", IN_API),
    ("find / -name x", None),
    ("find ~ -name '*.pyc'", None),
    ("ls ~", None),
    ("ls /", None),
    ("echo rm -rf /", None),
    ("grep -r 'rm -rf ~' .", None),
    ("cat <<EOF\nrm -rf ~\nEOF", None),
    ("# rm -rf ~\nls", None),
    ("curl https://example.com/install | sh", None),
    ("echo ok | sh", None),
    ('bash -c "$SCRIPT"', None),
    # the agent's own scratch
    ("rm -rf ~/scratch", None),
    ("rm -rf ~/scratch/*", None),
    ("rm -rf /work/*", None),
    ("rm -rf /work", None),
    ("cd ~/scratch && rm -rf *", IN_API),
]

# Written to on the host. Each is a protected path or a way to reach one.
WRITES_CAUGHT: list[str] = [
    "echo x >> ~/.zshrc",
    "echo x > ~/.zprofile",
    'echo x > "$HOME/.bashrc"',
    "echo x > ${HOME:?}/.bashrc",
    "echo x > ~//.zshrc",
    "echo x > ~/.gitconfig",
    "echo x > ~/.claude/settings.json",
    "echo x > ~/Library/LaunchAgents/a.plist",
    "echo x > .git/hooks/pre-commit",
    "echo x > /repo/.git/config",
    "echo x >| /repo/.git/hooks/post-commit",
    "echo gitdir: /elsewhere > .git",
    "tee ~/.bashrc",
    "echo a | tee -a ~/.zshenv",
    "cp a ~/.zshrc",
    "cp -t ~/.claude a",
    "install --target-directory=~/.claude a",
    "mv -t ~/Library/LaunchAgents a",
    "mv a ~/.profile",
    "ln -s a ~/.zshrc",
    "dd if=a of=~/.zshrc",
    "touch ~/.zshrc",
    "rm ~/.zshrc",
    "sed -i s/a/b/ ~/.zshrc",
    "sed -i.bak s/a/b/ ~/.zshrc",
    "perl -pi -e 's/a/b/' ~/.zshrc",
    "curl -o ~/.zshrc https://example.com/x",
    "curl -sSo ~/.zshrc https://example.com/x",
    "curl --output ~/.zshrc https://example.com/x",
    "curl --output=~/.zshrc https://example.com/x",
    "wget -O ~/.zshrc https://example.com/x",
    "wget --output-document=~/.zshrc https://example.com/x",
    "git config --global user.name x",
    "git config --global --unset user.name",
    "git -C /tmp config --global user.name x",
    "git config core.hooksPath /tmp/hooks",
    "git config --local core.hooksPath /tmp/hooks",
    "git -C /tmp/repo config core.hooksPath /tmp/hooks",
    "git config --file ~/.gitconfig user.name x",
    "bash -c 'echo x > ~/.zshrc'",
    "cat <<EOF > ~/.zshrc\nx\nEOF",
    "(echo x > ~/.zshrc)",
    "if true; then echo x > ~/.zshrc; fi",
    "cd ~ && echo x > .zshrc",
]

WRITES_PASSING: list[str] = [
    "echo x > ~/.zshrc.bak",
    "echo x > ~/.config/heph-test",
    "echo x > notes.md",
    "echo x > ./out/.zshrc",
    "cat ~/.zshrc",
    "cp ~/.zshrc ./backup",
    "cp a b",
    "cp -t out a",
    "mkdir ~/.config/heph-test",
    "mkdir .git",
    "echo x > /tmp/y",
    "echo 'x > ~/.zshrc'",
    "echo x > /dev/null",
    "cat <<EOF\necho x > ~/.zshrc\nEOF",
    "# echo x > ~/.zshrc\nls",
    "git config user.name x",
    "git config --global user.name",
    "git config --global --get user.name",
    "git config --list",
    "git config --local user.name x",
    "git commit -m 'edit .git/hooks docs'",
    "sed -n 1p ~/.zshrc",
    "curl -O https://example.com/x",
    "curl -o out.html https://example.com/x",
    "wget -O out.html https://example.com/x",
    "rm -rf .git/refs/foo",
]


class TestCriticalRemoval:
    @pytest.mark.parametrize(("command", "cwd"), REMOVALS_CAUGHT)
    def test_caught(self, command: str, cwd: str | None, settings: Settings, home: RunHome):
        assert reason_for(command, settings, home, cwd=cwd) is not None, command

    @pytest.mark.parametrize(("command", "cwd"), REMOVALS_PASSING)
    def test_passing(self, command: str, cwd: str | None, settings: Settings, home: RunHome):
        assert reason_for(command, settings, home, cwd=cwd) is None, command

    @pytest.mark.parametrize("reach", ["offline", "network", "host"])
    def test_the_reading_does_not_depend_on_where_it_runs(
        self, reach: str, settings: Settings, home: RunHome
    ):
        assert reason_for("rm -rf ~", settings, home, reach=reach) is not None
        assert reason_for("rm -rf ./build", settings, home, reach=reach) is None

    def test_a_project_folder_by_its_host_path(
        self, settings: Settings, home: RunHome, tmp_path: Path
    ):
        assert reason_for(f"rm -rf {tmp_path / 'api'}", settings, home) is not None
        assert reason_for(f"rm -rf {tmp_path / 'api'}/*", settings, home) is not None
        assert reason_for(f"rm -rf {tmp_path / 'api'}/build", settings, home) is None

    def test_a_folder_holding_a_project_is_critical(self, settings: Settings):
        documents = Path.home() / "Documents"
        held = RunHome(
            mounts=(
                Mount(SCRATCH, Path("/nonexistent/scratch"), writable=True),
                Mount(API, documents / "proj", writable=True),
            ),
            default=SCRATCH,
            key="t",
        )
        assert reason_for(f"rm -rf {documents}", settings, held) is not None
        assert reason_for("rm -rf ~/Documents", settings, held, reach="host") is not None
        assert reason_for("rm -rf ~/Documents/other", settings, held, reach="host") is None
        assert reason_for("rm -rf ~/Documents", settings, None) is None

    def test_the_data_folder_and_its_parent(self, settings: Settings):
        assert reason_for(f"rm -rf {settings.data_dir}", settings, None) is not None
        assert reason_for(f"rm -rf {settings.data_dir}/*", settings, None) is not None
        assert reason_for(f"rm -rf {settings.data_dir.parent}", settings, None) is not None
        assert reason_for(f"rm -rf {settings.data_dir.parent}/other", settings, None) is None

    def test_the_real_home_folder_and_what_holds_it(self, settings: Settings):
        assert reason_for(f"rm -rf {Path.home()}", settings, None) is not None
        assert reason_for(f"rm -rf {Path.home().parent}", settings, None) is not None

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
    @pytest.mark.parametrize("command", WRITES_CAUGHT)
    def test_caught_on_the_host(self, command: str, settings: Settings, home: RunHome):
        assert reason_for(command, settings, home, reach="host") is not None, command

    @pytest.mark.parametrize("command", WRITES_PASSING)
    def test_passing_on_the_host(self, command: str, settings: Settings, home: RunHome):
        assert reason_for(command, settings, home, reach="host") is None, command

    def test_the_vms_home_is_not_the_operators(self, settings: Settings, home: RunHome):
        for command in ("echo x >> ~/.zshrc", "echo x > ~/.gitconfig", "cp a ~/.profile"):
            assert reason_for(command, settings, home) is None, command
        assert reason_for("git config --global user.name x", settings, home) is None

    def test_a_projects_hooks_and_config_are_protected_from_the_vm_too(
        self, settings: Settings, home: RunHome
    ):
        for command in (
            "echo x > ~/projects/api/.git/hooks/pre-commit",
            "echo x > /home/agent/projects/api/.git/config",
            "git config core.hooksPath /tmp/hooks",
        ):
            assert reason_for(command, settings, home) is not None, command
        assert reason_for("echo x > ~/projects/api/src/a.py", settings, home) is None

    def test_a_relative_target_follows_the_working_directory(
        self, settings: Settings, home: RunHome
    ):
        command = "echo x > hooks/pre-commit"
        assert reason_for(command, settings, home, cwd=f"{IN_API}/.git") is not None
        assert reason_for(command, settings, home, cwd=IN_API) is None
        assert reason_for("cd .git && echo x > hooks/a", settings, home, cwd=IN_API) is not None

    def test_the_data_folder_is_protected_but_a_mount_inside_it_is_not(self, tmp_path: Path):
        settings = Settings(data_dir=tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        home = RunHome(mounts=(Mount(SCRATCH, scratch, writable=True),), default=SCRATCH, key="t")
        host = {"reach": "host"}
        for command, expected in (
            (f"echo x > {tmp_path}/app.db", True),
            (f"echo x > {scratch}/notes.md", False),
        ):
            reason = guard(
                "shell_run_command", {"command": command, **host}, home=home, settings=settings
            )
            assert (reason is not None) is expected, command

    def test_a_target_known_only_at_run_time_is_not_judged(self, settings: Settings, home: RunHome):
        assert reason_for("echo x > $TARGET", settings, home, reach="host") is None
        assert reason_for("tee $TARGET", settings, home, reach="host") is None


class TestFileTools:
    def args(self, path: str) -> dict[str, object]:
        return {"path": path, "content": "x"}

    @pytest.mark.parametrize("tool", ["files_write_file", "files_edit_file"])
    def test_a_hook_or_a_repo_config_is_caught(self, tool: str, settings: Settings, home: RunHome):
        for path in (
            "~/projects/api/.git/hooks/pre-commit",
            "~/projects/api/.git/config",
            "projects/api/.git/hooks/x",
            "~/projects/api/.git",
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
        gitdir = self.args("~/projects/api/.git")
        assert guard("files_create_directory", gitdir, home=home, settings=settings) is None

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


class TestGuardOfApproval:
    """What the approve route asks: is this approval of a guarded call?"""

    def test_a_parked_call_marked_guarded_is_guarded(self, settings: Settings, home: RunHome):
        reason = guard_of_approval(
            "shell_run_command",
            {GUARD_KEY: "deletes your home"},
            None,
            home=home,
            settings=settings,
        )
        assert reason == "deletes your home"

    def test_the_marker_holds_whatever_arguments_replace_the_call(
        self, settings: Settings, home: RunHome
    ):
        harmless: dict[str, object] = {"command": "ls"}
        reason = guard_of_approval(
            "shell_run_command",
            {GUARD_KEY: "deletes your home"},
            harmless,
            home=home,
            settings=settings,
        )
        assert reason == "deletes your home"

    def test_replacement_arguments_that_are_guarded_make_the_approval_guarded(
        self, settings: Settings, home: RunHome
    ):
        reason = guard_of_approval(
            "shell_run_command", {}, shell("rm -rf ~"), home=home, settings=settings
        )
        assert reason is not None

    def test_an_ordinary_approval_is_not_guarded(self, settings: Settings, home: RunHome):
        assert (
            guard_of_approval("shell_run_command", None, shell("ls"), home=home, settings=settings)
            is None
        )
        assert (
            guard_of_approval("shell_run_command", {}, None, home=home, settings=settings) is None
        )

    def test_a_marker_with_no_text_still_guards(self, settings: Settings, home: RunHome):
        marked = {GUARD_KEY: True}
        assert guard_of_approval("x", marked, None, home=home, settings=settings) is not None
