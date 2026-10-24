"""A command that overwrites or rewrites a file needs a read of it behind it, as an edit does."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import get_settings
from core.container import ServiceContainer
from services.home.table import RunHome
from services.permissions.guards import HOST_COMMAND_TOOL
from tools.command_ledger import CommandCheck, ledger_check, ledger_refusal
from tools.deps import RunDeps
from tools.run_home import home_of

from .files_support import README, call_files, project_caps, run_deps


async def _setup(tmp_path: Path) -> tuple[ServiceContainer, Path, RunDeps, RunHome]:
    caps, root = await project_caps(tmp_path)
    deps = run_deps(caps)
    home = await home_of(deps)
    assert home is not None
    return caps, root, deps, home


async def _check(
    deps: RunDeps,
    home: RunHome,
    command: str,
    *,
    tool: str = "shell_run_command",
    reach: str = "offline",
    cwd: str | None = None,
) -> CommandCheck:
    check = await ledger_check(deps, tool, {"command": command, "reach": reach}, home, cwd)
    assert check is not None
    return check


@pytest.mark.parametrize(
    "command",
    [
        f"echo x > {README}",
        f"echo x >| {README}",
        f"sed -i s/api/API/ {README}",
        f"perl -pi -e s/api/API/ {README}",
        f"echo x | tee {README}",
        f"truncate -s 0 {README}",
        f"dd if=/dev/zero of={README} count=1",
        f"curl -o {README} https://example.com",
        f"cp ~/projects/api/other.md {README}",
        f"mv ~/projects/api/other.md {README}",
        "cp ~/projects/api/sub/README.md ~/projects/api/",
    ],
)
async def test_a_rewrite_of_an_unread_file_is_refused(tmp_path: Path, command: str):
    _, root, deps, home = await _setup(tmp_path)
    _ = (root / "other.md").write_text("other\n")
    (root / "sub").mkdir()
    _ = (root / "sub" / "README.md").write_text("sub\n")
    refusal = (await _check(deps, home, command)).refusal
    assert refusal is not None
    assert "before changing it." in refusal


@pytest.mark.parametrize(
    "command",
    [
        f"echo x >> {README}",
        f"echo x | tee -a {README}",
        f"touch {README}",
        f"rm {README}",
        f"mv {README} ~/projects/api/old.md",
        "echo x > ~/projects/api/new.md",
        "sed -i s/a/b/ README.md",
        'sed -i s/a/b/ "$TARGET"',
        f"cat {README} 2> {README}.log",
        f"cp -n ~/projects/api/other.md {README}",
        f"truncate -r {README} ~/projects/api/new.bin",
    ],
)
async def test_a_command_that_rewrites_no_existing_file_is_not_refused(
    tmp_path: Path, command: str
):
    _, root, deps, home = await _setup(tmp_path)
    _ = (root / "other.md").write_text("other\n")
    assert (await _check(deps, home, command)).refusal is None


async def test_every_unread_file_is_named_in_one_refusal(tmp_path: Path):
    _, root, deps, home = await _setup(tmp_path)
    _ = (root / "other.md").write_text("other\n")
    refusal = (await _check(deps, home, f"sed -i s/a/b/ {README} ~/projects/api/other.md")).refusal
    assert refusal is not None
    assert README in refusal and "other.md" in refusal
    assert "before changing them." in refusal


async def test_a_printed_file_counts_as_read(tmp_path: Path):
    _, _, deps, home = await _setup(tmp_path)
    await (await _check(deps, home, f"head -n 5 {README}")).record()
    assert (await _check(deps, home, f"sed -i s/api/API/ {README}")).refusal is None


async def test_a_file_read_with_the_file_tools_may_be_rewritten(tmp_path: Path):
    caps, _, deps, home = await _setup(tmp_path)
    _ = await call_files(caps, "read_file", {"path": README})
    assert (await _check(deps, home, f"sed -i s/api/API/ {README}")).refusal is None


@pytest.mark.parametrize(
    "command",
    [
        f"cat {README} | grep api",
        f"(cat {README}) | grep api",
        f"{{ cat {README}; }} | wc -l",
        f"cat {README} > ~/projects/api/copy.md",
        f"echo $(cat {README})",
        f"sed -i s/a/b/ {README}",
    ],
)
async def test_a_file_whose_contents_were_not_shown_is_not_read(tmp_path: Path, command: str):
    _, _, deps, home = await _setup(tmp_path)
    await (await _check(deps, home, command)).record()
    assert (await _check(deps, home, f"echo x > {README}")).refusal is not None


async def test_a_file_too_large_to_be_shown_whole_is_not_read(tmp_path: Path):
    _, root, deps, home = await _setup(tmp_path)
    big = "~/projects/api/big.txt"
    _ = (root / "big.txt").write_text("x" * (get_settings().tool_output_spill_chars + 1))
    await (await _check(deps, home, f"cat {big}")).record()
    assert (await _check(deps, home, f"echo x > {big}")).refusal is not None


async def test_a_file_changed_since_the_read_is_refused(tmp_path: Path):
    _, root, deps, home = await _setup(tmp_path)
    await (await _check(deps, home, f"cat {README}")).record()
    _ = (root / "README.md").write_text("moved on\n")
    refusal = (await _check(deps, home, f"sed -i s/api/API/ {README}")).refusal
    assert refusal is not None
    assert "changed since you read it." in refusal


async def test_a_read_is_recorded_at_the_contents_the_command_printed(tmp_path: Path):
    _, root, deps, home = await _setup(tmp_path)
    check = await _check(deps, home, f"cat {README} && ./migrate")
    _ = (root / "README.md").write_text("changed by the migration\n")
    await check.record()
    refusal = (await _check(deps, home, f"sed -i s/api/API/ {README}")).refusal
    assert refusal is not None
    assert "changed since you read it." in refusal


async def test_a_command_that_rewrites_a_read_file_records_its_new_contents(tmp_path: Path):
    caps, root, deps, home = await _setup(tmp_path)
    _ = await call_files(caps, "read_file", {"path": README})
    check = await _check(deps, home, f"sed -i s/api/API/ {README}")
    assert check.refusal is None
    _ = (root / "README.md").write_text("the API\n")
    await check.record()
    edited = await call_files(
        caps, "edit_file", {"path": README, "old_text": "the API", "new_text": "an API"}
    )
    assert edited.startswith("Edited")
    assert (await _check(deps, home, f"sed -i s/an/the/ {README}")).refusal is None


async def test_an_append_to_an_unread_file_does_not_count_as_a_read(tmp_path: Path):
    _, _, deps, home = await _setup(tmp_path)
    await (await _check(deps, home, f"echo x >> {README}")).record()
    await (await _check(deps, home, f"touch {README}")).record()
    assert (await _check(deps, home, f"sed -i s/api/API/ {README}")).refusal is not None


async def test_a_new_file_a_command_writes_may_be_rewritten_after(tmp_path: Path):
    _, root, deps, home = await _setup(tmp_path)
    created = "~/projects/api/new.md"
    check = await _check(deps, home, f"echo x > {created}")
    _ = (root / "new.md").write_text("x\n")
    await check.record()
    assert (await _check(deps, home, f"sed -i s/x/y/ {created}")).refusal is None


async def test_a_download_does_not_count_as_a_read(tmp_path: Path):
    _, root, deps, home = await _setup(tmp_path)
    page = "~/projects/api/page.html"
    check = await _check(deps, home, f"curl -o {page} https://example.com")
    _ = (root / "page.html").write_text("<html>\n")
    await check.record()
    assert (await _check(deps, home, f"sed -i s/a/b/ {page}")).refusal is not None


async def test_moving_an_unread_file_away_and_back_does_not_count_as_a_read(tmp_path: Path):
    _, root, deps, home = await _setup(tmp_path)
    away = await _check(deps, home, f"mv {README} ~/projects/api/away.md")
    _ = (root / "README.md").rename(root / "away.md")
    await away.record()
    back = await _check(deps, home, f"mv ~/projects/api/away.md {README}")
    assert back.refusal is None
    _ = (root / "away.md").rename(root / "README.md")
    await back.record()
    assert (await _check(deps, home, f"sed -i s/a/b/ {README}")).refusal is not None


async def test_a_copy_of_a_read_file_counts_as_read(tmp_path: Path):
    _, root, deps, home = await _setup(tmp_path)
    await (await _check(deps, home, f"cat {README}")).record()
    copy = "~/projects/api/copy.md"
    check = await _check(deps, home, f"cp {README} {copy}")
    _ = (root / "copy.md").write_bytes((root / "README.md").read_bytes())
    await check.record()
    assert (await _check(deps, home, f"sed -i s/a/b/ {copy}")).refusal is None


async def test_a_relative_path_is_read_from_where_the_command_moved(tmp_path: Path):
    _, _, deps, home = await _setup(tmp_path)
    moved = await _check(deps, home, "cd ~/projects/api && sed -i s/a/b/ README.md")
    assert moved.refusal is not None
    started = await _check(deps, home, "sed -i s/a/b/ README.md", cwd="~/projects/api")
    assert started.refusal is not None


async def test_a_host_command_is_gated_inside_the_home_only(tmp_path: Path):
    _, root, deps, home = await _setup(tmp_path)
    inside = f"sed -i s/a/b/ {root / 'README.md'}"
    assert (await _check(deps, home, inside, tool=HOST_COMMAND_TOOL)).refusal is not None
    assert (await _check(deps, home, inside, reach="host")).refusal is not None
    elsewhere = tmp_path / "elsewhere.txt"
    _ = elsewhere.write_text("x\n")
    outside = f"sed -i s/a/b/ {elsewhere}"
    assert (await _check(deps, home, outside, tool=HOST_COMMAND_TOOL)).refusal is None


async def test_a_read_in_one_conversation_does_not_count_in_another(tmp_path: Path):
    caps, _, deps, home = await _setup(tmp_path)
    await (await _check(deps, home, f"cat {README}")).record()
    other = run_deps(caps, "conv-2")
    assert (await _check(other, home, f"sed -i s/a/b/ {README}")).refusal is not None


async def test_a_run_with_no_ledger_or_no_command_is_not_checked(tmp_path: Path):
    _, _, deps, home = await _setup(tmp_path)
    bare = run_deps(ServiceContainer())
    command = {"command": f"> {README}"}
    assert await ledger_check(bare, "shell_run_command", command, home, None) is None
    assert await ledger_check(deps, "files_read_file", {"path": README}, home, None) is None


async def test_the_refusal_is_given_before_anyone_is_asked(tmp_path: Path):
    _, _, deps, home = await _setup(tmp_path)
    args: dict[str, object] = {"command": f"sed -i s/a/b/ {README}"}
    assert await ledger_refusal(deps, "shell_run_command", args, home, None) is not None
    write: dict[str, object] = {"path": README, "content": "x"}
    assert await ledger_refusal(deps, "files_write_file", write, home, None) is not None
