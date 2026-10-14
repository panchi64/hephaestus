"""The type fence on `POST /host/open` — `services/host_open_policy`.

The attack these tests stand against, end to end: the agent writes a file into a project
root with an ordinary workspace-write (no approval gate on that), names it in its answer
behind anchor text of its choosing, and the operator's click hands it to the host's
"open with whatever handles this" program — which *runs* it. Containment is no defence,
because the file is genuinely inside the operator's own project.

So the refusals are the subject here, and they are written per class of executable thing
rather than per suffix: what must never regress is the deny-by-default posture, not the
particular spelling of any one entry in the allowlist.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

import pytest

from core.exceptions import PermissionDeniedError
from services.host_open_policy import ensure_openable


def _file(path: Path, *, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text("x")
    if mode is not None:
        path.chmod(mode)
    return path


class TestWhatOpens:
    @pytest.mark.parametrize(
        "name",
        [
            "app.py",  # the file a code thread just wrote — the whole point of this
            "NOTES.md",
            "report.MD",  # the extension is the host's, and the host doesn't mind case
            "data.json",
            "diagram.svg",
            "Makefile",  # no extension for a handler to dispatch on, and no exec bit
            ".gitignore",  # a dotfile is a name, not an extension-less free pass
        ],
    )
    def test_something_to_read_opens(self, tmp_path: Path, name: str):
        ensure_openable(_file(tmp_path / name))

    def test_a_plain_folder_opens(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        ensure_openable(tmp_path / "src")

    def test_a_hidden_folder_opens(self, tmp_path: Path):
        # `.github` is a folder every repo carries; reading its leading dot as an
        # extension would refuse the ordinary case to catch nothing.
        (tmp_path / ".github").mkdir()
        ensure_openable(tmp_path / ".github")


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "name",
        [
            "build-results.command",  # macOS: Terminal runs it
            "report.desktop",  # Linux: an entry whose Exec= line is anything at all
            "report.bat",
            "report.cmd",
            "report.com",
            "report.exe",
            "report.lnk",  # a pointer at another program
            "report.ps1",
            "report.scr",
            "report.vbs",
            "report.msi",
            "report.reg",
            "report.scpt",
            "report.terminal",
            "report.sh",  # the registered handler is a shell; running it *is* opening it
            "report.webloc",
            "report.docm",  # the macro-bearing twin of an allowlisted document
        ],
    )
    def test_a_file_the_host_would_run_is_refused(self, tmp_path: Path, name: str):
        with pytest.raises(PermissionDeniedError, match="only text, source and documents"):
            ensure_openable(_file(tmp_path / name))

    def test_the_execute_bit_is_refused_whatever_the_extension_says(self, tmp_path: Path):
        # `.py` is allowlisted, and the file is still a script the shell will run: an
        # extension says what a file is called, not what the host does with it.
        with pytest.raises(PermissionDeniedError, match="marked executable"):
            ensure_openable(_file(tmp_path / "app.py", mode=0o755))

    def test_an_extension_less_file_with_the_execute_bit_is_refused(self, tmp_path: Path):
        # Nothing to dispatch on, so the exec bit is the *only* thing standing here.
        with pytest.raises(PermissionDeniedError, match="marked executable"):
            ensure_openable(_file(tmp_path / "run", mode=0o755))

    def test_an_application_bundle_is_a_directory_and_is_still_refused(self, tmp_path: Path):
        # The one executable thing a `.is_file()` check would wave straight through.
        (tmp_path / "Evil.app" / "Contents" / "MacOS").mkdir(parents=True)
        with pytest.raises(PermissionDeniedError, match="folder with an extension"):
            ensure_openable(tmp_path / "Evil.app")

    @pytest.mark.parametrize("name", ["report.md.", "report.md "])
    def test_a_spelling_the_shell_normalises_away_is_refused(self, tmp_path: Path, name: str):
        # Windows drops a trailing dot or space when it resolves a name, so these open
        # `report.md`'s neighbour rather than themselves. An allowlist matches exactly,
        # which is why it has no hole here for a denylist to miss.
        with pytest.raises(PermissionDeniedError, match="only text, source and documents"):
            ensure_openable(_file(tmp_path / name))

    def test_a_pipe_is_not_a_file_to_open(self, tmp_path: Path):
        # An opener pointed at a fifo blocks on it; there is nothing to show either way.
        fifo = tmp_path / "pipe.md"
        os.mkfifo(fifo)
        with pytest.raises(PermissionDeniedError, match="isn't a regular file"):
            ensure_openable(fifo)


class TestTheHostDecides:
    """One file, two meanings: `.js` is source everywhere and a program on Windows,
    where the shell hands it to Windows Script Host. A single global list would have to
    pick one host to be wrong on."""

    def test_javascript_opens_where_it_is_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        ensure_openable(_file(tmp_path / "index.js"))

    def test_javascript_is_refused_where_the_shell_runs_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        with pytest.raises(PermissionDeniedError, match="only text, source and documents"):
            ensure_openable(_file(tmp_path / "index.js"))
