"""The OS fence a cleared worktree command runs inside.

Three layers, and they fail for three different reasons. The **profile** is a pure
translation of a checkout into two lists of paths — it is wrong when a path a commit needs
is missing, or when one a repository must not choose for us is present. **`GitDirs`** reads
a real linked worktree, which is the one shape that cannot be reasoned about from a string:
`.git` is a *file* there, and the objects and refs live somewhere else entirely. And the
**wrapper** is the shim that keeps `cd` working across calls, which is our code sitting
between two pieces of somebody else's.

The last test actually runs commands under the fence. It is the only one that can fail for
a reason nothing here wrote — a missing `sandbox-exec`, a missing `bwrap`, a missing
`ripgrep` — so it skips rather than failing, and everything above it is host-independent.

**What the fence does and does not bound, stated once.** Writes are deny-by-default with an
allowlist and egress is allowlist-only; *reads* are allow-by-default with a denylist, and
``sandbox_runtime`` has no read allowlist to change that. So a fenced command can still read
the operator's files outside the worktree, and nothing here can be written to prove
otherwise — which is why the plan's `cat ../x` case is pinned in
``tests/test_shell_judge.py`` against the *judge* instead, and why the judge's containment
carries the whole weight for reads. What the fence is the backstop for is what a command
*writes* or *sends* beyond what it named — a program the repository configured, a build
script, a postinstall hook — which is exactly the class no reading of the command line
could have caught.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from core.config import Settings
from services.sandbox.fence import GitDirs, annotate, workspace_profile, wrap
from services.sandbox.host import _configure, scratch_path
from services.sandbox.shell_runner import FencedShell

BRANCH = "odysseus/thread-1"


def _allow(profile) -> list[str]:
    return profile.filesystem.allow_write


def _deny(profile) -> list[str]:
    return profile.filesystem.deny_write


class TestTheProfileIsTwoListsOfPaths:
    """The builder, on its own — no git, no host, no runtime."""

    def test_a_workspace_with_no_repository_is_fenced_to_itself(self, tmp_path):
        profile = workspace_profile(tmp_path, None, None)
        assert str(tmp_path) in _allow(profile)
        assert tempfile.gettempdir() in _allow(profile)
        assert profile.network.allowed_domains == []
        # The only denial a repository-less workspace carries is the host hatch's scratch
        # directory (below); nothing about git applies.
        assert _deny(profile) == [str(scratch_path())]

    def test_the_directory_approved_host_commands_run_in_is_not_writable(self, tmp_path):
        # It sits under the OS temp root, which the profile has to allow — a build, a test
        # runner and the tool's own `cd` capture all write there. So it is denied by name:
        # it is the cwd every operator-approved host command starts in and resolves its
        # relative paths against, and a command that needed no approval at all must not be
        # able to leave a script in it for one that did.
        profile = workspace_profile(tmp_path, None, None)
        assert str(scratch_path()) in _deny(profile)
        assert str(scratch_path()).startswith(tempfile.gettempdir())

    def test_what_may_not_be_read_may_not_be_written_either(self, tmp_path):
        # Mirrors the host profile: read denial alone would still let a command clobber
        # the vault or a key it could not read.
        profile = workspace_profile(tmp_path, None, None, deny_read=("~/.ssh", "/data"))
        for path in ("~/.ssh", "/data"):
            assert path in _deny(profile)

    def test_a_commit_can_land_and_nothing_around_it_can(self, tmp_path):
        git = GitDirs(private=tmp_path / "private", common=tmp_path / "common")
        profile = workspace_profile(tmp_path, git, BRANCH)
        allowed = _allow(profile)
        # The four places a commit writes: the object store, the worktree's own metadata
        # (HEAD, the index, its reflog), and this thread's branch ref and reflog.
        assert str(git.common / "objects") in allowed
        assert str(git.private) in allowed
        assert str(git.common / "refs" / "heads" / BRANCH) in allowed
        assert f"{git.common / 'refs' / 'heads' / BRANCH}.lock" in allowed
        assert str(git.common / "logs" / "refs" / "heads" / BRANCH) in allowed
        # ...and nothing that would let a command choose what a later command runs, or
        # rewrite history this branch has no business touching.
        for name in ("config", "hooks", "packed-refs", "info", "refs/tags", "refs/remotes"):
            assert str(git.common / name) in _deny(profile)

    def test_another_branch_is_out_of_reach_without_a_rule_saying_so(self, tmp_path):
        # In a linked worktree — the shape a worktree always has — the common directory
        # is outside every allow, so the operator's other branches need no deny entry: they
        # need only not be allowed.
        git = GitDirs(private=tmp_path / "private", common=tmp_path / "common")
        allowed = _allow(workspace_profile(tmp_path, git, BRANCH))
        assert str(git.common / "refs" / "heads" / "main") not in allowed
        assert str(git.common / "refs") not in allowed

    def test_a_plain_checkouts_other_branches_are_inside_the_worktree_allow(self, tmp_path):
        # The shape the docstring has to be honest about. With `.git` a *directory* under
        # the root, every ref sits inside the worktree allow, and the one ref a commit must
        # write lives under `refs/heads` beside all the others — deny beats allow in both
        # runtimes, so denying the parent would deny this thread's own branch too. Tags and
        # remotes can be denied, and are; another branch's ref cannot, and is not.
        profile = workspace_profile(tmp_path, GitDirs(tmp_path / ".git", tmp_path / ".git"), BRANCH)
        denied = _deny(profile)
        assert str(tmp_path / ".git" / "refs" / "tags") in denied
        assert str(tmp_path / ".git" / "refs" / "heads") not in denied
        assert str(tmp_path) in _allow(profile)

    def test_the_pointers_the_profile_was_built_from_are_not_writable(self, tmp_path):
        # The escape this closes takes two commands, each of which stays inside the
        # worktree and clears structurally: write a line, copy it over `.git`. Nothing has
        # escaped yet — but the *next* command's profile is built by reading that file, so
        # the third command gets an allow on whatever the second one named. A fence whose
        # own inputs are writable by what it fences is not one.
        git = GitDirs(private=tmp_path / "private", common=tmp_path / "common")
        denied = _deny(workspace_profile(tmp_path, git, BRANCH))
        assert str(tmp_path / ".git") in denied
        assert str(git.private / "commondir") in denied

    def test_a_plain_checkout_has_no_pointer_to_hold(self, tmp_path):
        # There `.git` *is* the metadata directory, written on every commit, and a denial
        # is a subpath in both runtimes — so denying it would deny the commit with it.
        # Nothing points anywhere in that shape, so there is nothing to protect.
        git = GitDirs(private=tmp_path / ".git", common=tmp_path / ".git")
        assert str(tmp_path / ".git") not in _deny(workspace_profile(tmp_path, git, BRANCH))

    def test_a_thread_with_no_branch_names_no_ref_at_all(self, tmp_path):
        git = GitDirs(private=tmp_path / "private", common=tmp_path / "common")
        allowed = _allow(workspace_profile(tmp_path, git, None))
        # No branch ref and no branch reflog — `packed-refs.lock` is allowed regardless,
        # since every ref update takes it and a commit is not the only thing that does.
        assert not [path for path in allowed if "refs/heads" in path]

    def test_linux_allows_the_directory_because_bubblewrap_binds_what_exists(self, tmp_path):
        # `bwrap` skips a bind whose source is missing, so the `.lock` git has not created
        # yet cannot be allowed by name there. The directory holding it is — wider than on
        # macOS, and the platform's price for a `git commit` that works at all.
        git = GitDirs(private=tmp_path / "private", common=tmp_path / "common")
        allowed = _allow(workspace_profile(tmp_path, git, BRANCH, linux=True))
        assert str((git.common / "refs" / "heads" / BRANCH).parent) in allowed
        assert f"{git.common / 'refs' / 'heads' / BRANCH}.lock" not in allowed

    def test_the_network_list_is_the_callers_and_defaults_to_none(self, tmp_path):
        assert workspace_profile(tmp_path, None, None).network.allowed_domains == []
        networked = workspace_profile(tmp_path, None, None, allowed_domains=("pypi.org",))
        assert networked.network.allowed_domains == ["pypi.org"]

    def test_the_operators_own_write_additions_are_carried(self, tmp_path):
        # A separate setting from the host hatch's `host_command_allow_write`, and that is
        # the point: a host command is one the operator read and approved, so its list may
        # be as broad as `~`; a `workspace` command is one nobody was asked about, so the
        # same breadth here would be the fence dissolved.
        allowed = _allow(workspace_profile(tmp_path, None, None, allow_write=("~/.cache",)))
        assert "~/.cache" in allowed
        assert "~" not in allowed

    def test_the_read_denials_are_restated_rather_than_inherited(self, tmp_path):
        # A per-call profile *replaces* the global one the host hatch configured, so the
        # credential paths and the data directory have to be named again or a worktree
        # command would be fenced more loosely than an approved host command.
        profile = workspace_profile(tmp_path, None, None, deny_read=("~/.ssh", "/data"))
        assert profile.filesystem.deny_read == ["~/.ssh", "/data"]

    def test_the_seed_names_the_operators_other_credentials_too(self):
        # The list a fenced command is actually held to, and the fence has no read
        # *allowlist* — so this is the whole of what stops a contained command reading a
        # standing credential and a networked one sending it. Pinned because it is a
        # security seed, and a seed nothing reads back is one that quietly shrinks.
        from core.config import Settings
        from services.sandbox import denied_read_paths

        denied = denied_read_paths(Settings())
        for path in (
            "~/.ssh",
            "~/.aws",
            "~/.gnupg",
            "~/.config/gh",
            "~/.netrc",
            "~/.docker",
            "~/.kube",
            "~/.config/gcloud",
            "~/.azure",
            "~/Library/Keychains",
            "~/.zsh_history",
            "~/.bash_history",
        ):
            assert path in denied, path


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
class TestGitDirsReadsARealCheckout:
    """The one shape that cannot be reasoned about from a string."""

    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        run = lambda *args: subprocess.run(  # noqa: E731 — one line, one meaning
            args, cwd=repo, check=True, capture_output=True
        )
        run("git", "init", "-b", "main")
        run("git", "config", "user.email", "t@example.com")
        run("git", "config", "user.name", "Test")
        (repo / "a.txt").write_text("x")
        run("git", "add", "-A")
        run("git", "commit", "-m", "first")
        return repo

    def _worktree(self, tmp_path: Path) -> Path:
        repo = self._repo(tmp_path)
        worktree = tmp_path / "wt"
        subprocess.run(
            ("git", "worktree", "add", "-b", BRANCH, str(worktree)),
            cwd=repo,
            check=True,
            capture_output=True,
        )
        return worktree

    def test_a_plain_checkout_has_one_directory_serving_as_both(self, tmp_path):
        repo = self._repo(tmp_path)
        dirs = GitDirs.read(repo)
        assert dirs is not None
        assert dirs.private == dirs.common == repo / ".git"

    def test_a_linked_worktree_has_two_and_neither_is_under_the_checkout(self, tmp_path):
        worktree = self._worktree(tmp_path)
        repo = tmp_path / "repo"
        dirs = GitDirs.read(worktree)
        assert dirs is not None
        # `.git` is a file pointing elsewhere: a profile fenced to the worktree alone would
        # have left the operator's own repository entirely outside it.
        assert (worktree / ".git").is_file()
        assert dirs.common == repo / ".git"
        assert dirs.private == repo / ".git" / "worktrees" / "wt"
        assert (dirs.common / "objects").is_dir()

    def test_a_pointer_that_does_not_describe_this_repository_reads_as_none(self, tmp_path):
        # The second half of the same escape: even where the pointer file *can* be
        # rewritten (an earlier command, an unfenced host command, an operator's own
        # mistake), what it says is only believed when the two halves agree about which
        # repository this is — git writes a linked worktree's metadata at
        # `<common>/worktrees/<name>`, and forging that shape for a directory we may not
        # already write means first creating a directory inside it.
        worktree = self._worktree(tmp_path)
        target = tmp_path / "target"
        target.mkdir()
        (worktree / ".git").write_text(f"gitdir: {target}\n")
        assert GitDirs.read(worktree) is None
        # ...and the same attempt wearing git's own layout, forged where it is writable.
        forged = tmp_path / "forged" / "worktrees" / "wt"
        forged.mkdir(parents=True)
        (forged / "commondir").write_text(f"{target}\n")
        (worktree / ".git").write_text(f"gitdir: {forged}\n")
        assert GitDirs.read(worktree) is None

    def test_a_pointer_that_is_not_even_text_reads_as_none(self, tmp_path):
        # Declining to believe a file must not mean *raising* on it: this runs on every
        # shell call of a thread and nothing above it catches, so a `commondir` of
        # arbitrary bytes would break the tool itself rather than the forgery.
        worktree = self._worktree(tmp_path)
        dirs = GitDirs.read(worktree)
        assert dirs is not None
        (dirs.private / "commondir").write_bytes(b"\xff\xfe\x00../..")
        assert GitDirs.read(worktree) is None

    def test_a_directory_that_is_not_a_checkout_reads_as_none(self, tmp_path):
        # Not an error: a workspace that is not a repository is fenced to itself, which is
        # the right answer and needs no git paths at all.
        assert GitDirs.read(tmp_path) is None


class TestTheWrapperOnlyWraps:
    """`wrap` puts the sandbox launcher in front of a command and does nothing else.

    It is worth a test rather than being obvious, because it did once do more. While the
    shell tool was built on a harness that spawned its own processes, the working-directory
    capture landed *outside* the fence and this function grew a `case`-guarded `cd` to carry
    it back in — a shell shim, generated as a string, holding a containment property. Owning
    the spawn removed the need for it: the capture is appended by
    `shell_runner.FencedShell` before the command reaches here, so it happens inside the
    fence, and the recorded directory is contained in Python afterwards.
    """

    async def _wrapped(self, monkeypatch, root: Path = Path("/tmp/x")) -> tuple[str, list[str]]:
        inner: list[str] = []

        async def fake(command, bin_shell=None, custom_config=None):
            inner.append(command)
            return f"SANDBOXED[{command}]"

        from sandbox_runtime import SandboxManager

        monkeypatch.setattr(SandboxManager, "wrap_with_sandbox", fake)
        profile = workspace_profile(root, None, None)
        return await wrap("make build", profile), inner

    async def test_the_command_reaches_the_launcher_unaltered(self, monkeypatch):
        command, inner = await self._wrapped(monkeypatch)
        assert inner == ["make build"]
        assert command == "SANDBOXED[make build]"

    async def test_nothing_is_appended_after_the_fence(self, monkeypatch):
        # The shim used to append a `cd` here, outside the sandboxed command. Anything
        # emitted after the launcher runs unfenced, so the rule is that nothing is.
        command, _inner = await self._wrapped(monkeypatch)
        assert "cd " not in command
        assert "__odysseus" not in command


class TestTheDenialNoteTheModelReads:
    """What a denied write reads as by the time it reaches the model.

    Written by us rather than read off ``sandbox_runtime``'s violation store, and that is
    the point of these two: the store is filled by a macOS-only kernel-log monitor which
    has to be enabled at global initialisation and did not record an ordinary denied write
    when it was tried, so a note conditioned on it would never have appeared at all.
    """

    def test_a_permission_failure_is_named_as_the_fence(self):
        annotated = annotate("/bin/bash: ../nope.txt: Operation not permitted")
        assert "[fence]" in annotated
        assert "declare the reach" in annotated
        # ...and the command's own output is still the first thing in it.
        assert annotated.startswith("/bin/bash: ../nope.txt: Operation not permitted")

    def test_an_ordinary_failure_is_left_alone(self):
        # A failing test suite is not a fence denial, and telling the model to redeclare
        # its reach would send it to fix the wrong thing.
        for output in ("2 tests failed", "exit code 1", ""):
            assert annotate(output) == output


async def _fence_or_skip(tmp_path: Path):
    """The real primitive, or a skip naming what is missing on this host.

    `host_command_sandbox_enabled` is set here rather than left to the environment because
    the suite switches it *off* (``conftest``) so that no other test depends on the machine
    it runs on. These tests are the ones that do depend on it, and saying so explicitly is
    what keeps them from silently skipping on a host that could have run them.
    """
    confinement = await _configure(
        Settings(data_dir=tmp_path / "data", host_command_sandbox_enabled=True)
    )
    if not confinement.active:
        pytest.skip(f"no host sandbox primitive here: {confinement.reason}")


async def _run(command: str, cwd: Path) -> tuple[int, str]:
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    return process.returncode or 0, (out + err).decode(errors="replace")


@pytest.mark.fence
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
class TestTheFenceActuallyHolds:
    """What the profile means once a real command runs under it.

    Everything above tests strings. This is the one that would catch a profile that reads
    correctly and permits the wrong thing — the failure mode a list of paths is most prone
    to, since a path allowed one level too high looks identical in a test that only asserts
    membership.
    """

    async def _worktree(self, tmp_path: Path) -> tuple[Path, GitDirs]:
        repo = tmp_path / "repo"
        repo.mkdir()

        def run(*args: str) -> None:
            subprocess.run(args, cwd=repo, check=True, capture_output=True)

        run("git", "init", "-b", "main")
        run("git", "config", "user.email", "t@example.com")
        run("git", "config", "user.name", "Test")
        (repo / "a.txt").write_text("x")
        run("git", "add", "-A")
        run("git", "commit", "-m", "first")
        worktree = tmp_path / "wt"
        run("git", "worktree", "add", "-b", BRANCH, str(worktree))
        dirs = GitDirs.read(worktree)
        assert dirs is not None
        return worktree, dirs

    async def _fenced(self, command: str, worktree: Path, dirs: GitDirs) -> tuple[int, str]:
        profile = workspace_profile(worktree, dirs, BRANCH, linux=_linux())
        return await _run(await wrap(command, profile), worktree)

    async def test_the_work_a_thread_exists_to_do_still_works(self, tmp_path):
        await _fence_or_skip(tmp_path)
        worktree, dirs = await self._worktree(tmp_path)
        # `git commit` last, and named rather than folded into the loop: `_branch_writes`
        # is the most intricate part of the profile — a ref, its reflog, a `.lock` beside
        # each, and a directory-level widening on Linux — and every one of those paths is
        # otherwise asserted only as a *string*, which cannot catch one allowed a level too
        # high or too low.
        for command in (
            "git status",
            "echo hello > inside.txt",
            "git add -A",
            "git commit -m second",
        ):
            code, output = await self._fenced(command, worktree, dirs)
            assert code == 0, f"{command}: {output}"
            # Cleanly, too: a commit that lands but prints `error: Unable to create
            # packed-refs.lock` reads to the model as a commit that did not.
            assert "error:" not in output, f"{command}: {output}"
        assert (worktree / "inside.txt").read_text().strip() == "hello"
        code, output = await _run("git log --oneline", worktree)
        assert "second" in output, output

    async def test_a_command_cannot_move_the_tracked_directory_out_of_the_worktree(self, tmp_path):
        # The hole this containment closes, driven through the real shell. The fenced
        # command records where it ended and the session adopts that as the directory the
        # *next* command starts in — so `cd /etc` inside a structurally-clean command
        # (`source build.sh`) would leave every later relative path judged against the
        # worktree and resolved somewhere else. Reads are not fenced, so nothing
        # downstream would have caught it.
        await _fence_or_skip(tmp_path)
        worktree, dirs = await self._worktree(tmp_path)
        (worktree / "build.sh").write_text("cd /etc\n")
        (worktree / "sub").mkdir()
        profile = workspace_profile(worktree, dirs, BRANCH, linux=_linux())
        shell = FencedShell(worktree, confiner=wrap, default_timeout=30.0, max_output_chars=100_000)
        for command, expected in (("source build.sh", worktree), ("cd sub", worktree / "sub")):
            await shell.run(command, profile=profile)
            assert shell.cwd.resolve() == expected.resolve(), command

    async def test_a_denied_path_inside_an_allowed_one_still_loses(self, tmp_path):
        # The precedence claim the whole profile is written around: both runtimes apply
        # the denials *after* the allows, so a narrow allow inside a broad deny cannot be
        # expressed — and, the direction that matters here, a narrow deny inside a broad
        # allow holds. `hooks` sits under a repository this test can otherwise write to.
        #
        # Nothing here asserts a write *outside* the checkout, and deliberately: a test's
        # workspace lives under the OS temp root, which the profile allows (a build needs
        # it) and which macOS's own runtime allows a level higher again — so on that host
        # there is no path near a temp workspace that could stand in for "outside". What
        # is provable here is precedence, and precedence is the part a list of paths can
        # get silently wrong.
        await _fence_or_skip(tmp_path)
        worktree, dirs = await self._worktree(tmp_path)
        hook = dirs.common / "hooks" / "pre-commit"
        code, _output = await self._fenced(f"echo x > {hook}", worktree, dirs)
        assert code != 0
        assert not hook.exists()

    async def test_the_repositorys_own_config_is_denied(self, tmp_path):
        # The reproduction this whole design exists for: `git config core.fsmonitor …` in a
        # worktree writes the *main checkout's* `.git/config`, and the key it writes names
        # a program every later `git status` runs. No subcommand table stated that rule;
        # the path does.
        await _fence_or_skip(tmp_path)
        worktree, dirs = await self._worktree(tmp_path)
        code, _output = await self._fenced("git config core.fsmonitor /tmp/x", worktree, dirs)
        assert code != 0
        assert "fsmonitor" not in (dirs.common / "config").read_text()

    async def test_a_tag_the_operator_wrote_is_out_of_reach(self, tmp_path):
        # `refs/tags` is *denied*, where another branch's ref is merely unallowed — and
        # only the denial is provable here. A test repository lives under the OS temp root,
        # which the profile allows wholesale (a build needs it), so every unallowed path in
        # it is writable in this environment and in no real one: a project checkout sits
        # wherever the operator keeps it, and `~/.odysseus/worktrees` is not `/tmp`. The
        # deny list is the half a test can hold, so it is the half that is tested.
        await _fence_or_skip(tmp_path)
        worktree, dirs = await self._worktree(tmp_path)
        code, output = await self._fenced("git tag sneaky", worktree, dirs)
        assert code != 0, output
        assert not (dirs.common / "refs" / "tags" / "sneaky").exists()

    async def test_a_command_cannot_rewrite_what_the_next_fence_is_built_from(self, tmp_path):
        # Reproduced end to end before the denial existed: two commands that each stay
        # inside the worktree and clear structurally (`printf x > tpl`, `cp tpl .git`)
        # left `.git` naming any directory on the host, and the *next* cleared command ran
        # with that directory in its write allowlist. Both spellings are tried, because a
        # denial that only caught the redirect would leave `cp` as the whole escape.
        await _fence_or_skip(tmp_path)
        worktree, dirs = await self._worktree(tmp_path)
        pointer = (worktree / ".git").read_text()
        (worktree / "tpl").write_text(f"gitdir: {tmp_path}\n")
        for command in (f"cp tpl {worktree / '.git'}", f"printf x > {worktree / '.git'}"):
            code, output = await self._fenced(command, worktree, dirs)
            assert code != 0, output
        assert (worktree / ".git").read_text() == pointer
        commondir = dirs.private / "commondir"
        code, output = await self._fenced(f"printf x > {commondir}", worktree, dirs)
        assert code != 0, output

    async def test_the_directory_approved_host_commands_run_in_is_out_of_reach(self, tmp_path):
        # It is under the OS temp root, which the profile allows wholesale, so this is a
        # denial that exists only because the path is named — and the reason it is named is
        # that a later operator-approved host command starts there and resolves its
        # relative paths against it.
        await _fence_or_skip(tmp_path)
        worktree, dirs = await self._worktree(tmp_path)
        scratch = scratch_path()
        scratch.mkdir(mode=0o700, exist_ok=True)
        planted = scratch / "planted.sh"
        code, _output = await self._fenced(f"echo pwned > {planted}", worktree, dirs)
        assert code != 0
        assert not planted.exists()


class TestWhichFenceTheToolBuilds:
    """`tools/shell.py`'s own choice: the *declaration* picks the profile, and only two
    answers lift the fence — both of them somebody's explicit yes.

    The availability of a fence is deliberately not one of the inputs here. A host that
    cannot confine a process refuses the shell tools outright (`_guard`), so by the time
    this function runs there is always a fence to build — which is what keeps "cleared or
    not, the command is fenced" a statement with no exceptions in it.
    """

    async def _profile(self, tmp_path, *, reach, domains=()):
        from services.workspace import HostFiles, RunWorkspace
        from tools import shell as shell_tool

        workspace = RunWorkspace(
            root=tmp_path, kind="worktree", files=HostFiles(tmp_path), branch=BRANCH
        )
        boundary = await shell_tool._boundary(  # noqa: SLF001 — the unit under test
            "run_command", "uv sync", reach, workspace, domains, Settings()
        )
        # The boundary carries the reason beside the profile now, so that a command the
        # fence did not wrap can say so on its own result at any permission level. What
        # this class is about is still the profile.
        assert boundary.fenced is (boundary.profile is not None)
        assert (boundary.reason is None) is boundary.fenced
        return boundary.profile

    async def test_an_empty_allowed_list_still_means_no_network(self, tmp_path):
        # The setting reads as a tightening and had to behave as one. Read as "no list to
        # hold it to", an emptied list lifted the fence altogether for every approved
        # networked command — no write confinement, no read denials, and the full network
        # — so the operator who wanted less got the loosest execution path in the product.
        profile = await self._profile(tmp_path, reach="network")
        assert profile is not None
        assert profile.network.allowed_domains == []
        assert _allow(profile)  # ...and it is still confined to the worktree

    async def test_a_permitted_domain_is_what_the_egress_is_bound_to(self, tmp_path):
        profile = await self._profile(tmp_path, reach="network", domains=("pypi.org",))
        assert profile is not None
        assert profile.network.allowed_domains == ["pypi.org"]

    async def test_a_host_declaration_is_the_one_that_asks_for_the_machine(self, tmp_path):
        assert await self._profile(tmp_path, reach="host") is None


def _linux() -> bool:
    import sys

    return sys.platform.startswith("linux")
