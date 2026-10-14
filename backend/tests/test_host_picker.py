"""The native file/folder chooser — helper resolution, argv shape, and the contract the
routes above it depend on.

No real dialog is ever opened: `_run` is stubbed, or the helper is replaced with a small
script that prints a path. What matters here is that the module **degrades cleanly** (a
headless host reports unavailable rather than erroring), that a cancelled dialog is a
`None`, not a failure, and that a dialog nobody answers can't wedge the server.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import sys
from collections.abc import Callable
from typing import Literal

import pytest

from services import host_picker
from services.host_picker import Helper, PickMode

type _Kind = Literal["osascript", "zenity", "kdialog", "powershell"]


def _no_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_picker, "resolve", lambda: None)


def _helper(monkeypatch: pytest.MonkeyPatch, name: str, kind: _Kind) -> None:
    monkeypatch.setattr(host_picker, "resolve", lambda: Helper(name, kind))


def _which_everything(name: str) -> str:
    return f"/usr/bin/{name}"


def _which_nothing(name: str) -> None:
    del name


def _which_only(found: str) -> Callable[[str], str | None]:
    def which(name: str) -> str | None:
        return f"/usr/bin/{found}" if name == found else None

    return which


# --- availability -----------------------------------------------------------


def test_probe_reports_unavailable_with_a_reason_when_nothing_is_installed(
    monkeypatch: pytest.MonkeyPatch,
):
    # The headless-host case. It must be an answer, not an exception — the surface above
    # simply hides its BROWSE control and the typed path still works.
    _no_helper(monkeypatch)
    result = host_picker.probe()
    assert result.available is False
    assert result.reason and result.tool is None


def test_probe_names_the_tool_it_would_use(monkeypatch: pytest.MonkeyPatch):
    _helper(monkeypatch, "zenity", "zenity")
    result = host_picker.probe()
    assert result.available is True and result.tool == "zenity"


def test_resolution_prefers_osascript_on_macos(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(shutil, "which", _which_everything)
    helper = host_picker.resolve()
    assert helper is not None and helper.kind == "osascript"


def test_resolution_finds_a_linux_chooser_in_order(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(shutil, "which", _which_only("kdialog"))
    helper = host_picker.resolve()
    assert helper is not None and helper.name == "kdialog"


def test_a_linux_server_with_no_desktop_chooser_resolves_to_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(shutil, "which", _which_nothing)
    assert host_picker.resolve() is None


async def test_pick_raises_when_no_chooser_exists(monkeypatch: pytest.MonkeyPatch):
    # Callers probe first, so this is a race rather than the normal path — but it must
    # still be a clear error and not a silent None that reads as "cancelled".
    _no_helper(monkeypatch)
    with pytest.raises(RuntimeError):
        _ = await host_picker.pick("file")


# --- argv shape -------------------------------------------------------------


def _argv(
    kind: _Kind,
    mode: PickMode,
    *,
    title: str = "Choose",
    start_dir: str | None = None,
    extensions: list[str] | None = None,
) -> list[str]:
    return host_picker.build_argv(Helper("tool", kind), mode, title, start_dir, extensions)


def test_osascript_chooses_a_folder_or_a_file_by_mode():
    assert "choose folder" in _argv("osascript", "directory")[-1]
    assert "choose file" in _argv("osascript", "file")[-1]


def test_osascript_renders_the_result_as_a_posix_path():
    # The dialog returns an alias; without this the caller would get an AppleScript
    # colon-separated path that no filesystem call accepts.
    assert _argv("osascript", "file")[-1].startswith("POSIX path of")


def test_osascript_quotes_are_escaped_not_interpolated_raw():
    script = _argv("osascript", "file", title='say "hi"')[-1]
    assert '\\"hi\\"' in script


def test_zenity_asks_for_a_directory_only_in_directory_mode():
    assert "--directory" in _argv("zenity", "directory")
    assert "--directory" not in _argv("zenity", "file")


def test_zenity_turns_extensions_into_a_glob_filter():
    argv = _argv("zenity", "file", extensions=["gguf"])
    assert "--file-filter=*.gguf" in argv


def test_kdialog_uses_the_matching_getter_per_mode():
    assert "--getexistingdirectory" in _argv("kdialog", "directory")
    assert "--getopenfilename" in _argv("kdialog", "file")


def test_powershell_picks_the_dialog_class_per_mode():
    assert "FolderBrowserDialog" in _argv("powershell", "directory")[-1]
    assert "OpenFileDialog" in _argv("powershell", "file")[-1]


def test_powershell_single_quotes_are_doubled():
    script = _argv("powershell", "file", title="it's here")[-1]
    assert "it''s here" in script


def test_a_leading_dot_on_an_extension_is_tolerated():
    # The caller may pass ".gguf" or "gguf"; neither should produce "*..gguf".
    assert "--file-filter=*.gguf" in _argv("zenity", "file", extensions=[".gguf"])


# --- running the helper -----------------------------------------------------


def _script_helper(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Point the picker at a python one-liner standing in for a dialog program."""

    def script_argv(*_args: object, **_kwargs: object) -> list[str]:
        return [sys.executable, "-c", body]

    monkeypatch.setattr(host_picker, "resolve", lambda: Helper(sys.executable, "zenity"))
    monkeypatch.setattr(host_picker, "build_argv", script_argv)


async def test_a_chosen_path_comes_back_stripped(monkeypatch: pytest.MonkeyPatch):
    _script_helper(monkeypatch, "print('/models/qwen.gguf')")
    assert await host_picker.pick("file") == "/models/qwen.gguf"


async def test_the_cancel_exit_code_is_a_cancellation_not_an_error(monkeypatch: pytest.MonkeyPatch):
    # Every one of these helpers reports "the operator closed the dialog" as exit 1, so
    # it must read as None rather than surfacing as a failure.
    _script_helper(monkeypatch, "import sys; sys.exit(1)")
    assert await host_picker.pick("file") is None


async def test_osascripts_cancel_error_is_a_cancellation(monkeypatch: pytest.MonkeyPatch):
    # osascript spells cancellation as an *error* (-128) rather than a distinct code.
    _script_helper(
        monkeypatch,
        "import sys; sys.stderr.write('execution error: User canceled. (-128)'); sys.exit(1)",
    )
    assert await host_picker.pick("file") is None


async def test_a_chooser_that_cannot_open_is_an_error_not_a_silent_cancel(
    monkeypatch: pytest.MonkeyPatch,
):
    # The case this exists for: a macOS host with no GUI session (SSH, launchd) has
    # osascript on PATH, so the picker advertises itself as available — but the dialog
    # can't be shown. Reporting that as "cancelled" leaves a BROWSE button that appears
    # to do nothing at all.
    _script_helper(
        monkeypatch,
        "import sys; sys.stderr.write('No user interaction allowed.'); sys.exit(2)",
    )
    with pytest.raises(RuntimeError, match="couldn't be opened"):
        _ = await host_picker.pick("file")


async def test_empty_output_is_a_cancellation_too(monkeypatch: pytest.MonkeyPatch):
    _script_helper(monkeypatch, "print('')")
    assert await host_picker.pick("file") is None


async def test_a_dialog_nobody_answers_is_killed_rather_than_held(monkeypatch: pytest.MonkeyPatch):
    # The invariant that keeps a forgotten dialog from holding the single-flight lock
    # (and a process) for the life of the backend.
    monkeypatch.setattr(host_picker, "_DIALOG_TIMEOUT_S", 0.2)
    _script_helper(monkeypatch, "import time; time.sleep(30)")
    assert await host_picker.pick("file") is None


async def test_only_one_dialog_opens_at_a_time(monkeypatch: pytest.MonkeyPatch):
    # Two native choosers racing for focus is worse than a queue, and a wedged one would
    # otherwise stack up invisibly.
    live = 0
    peak = 0

    async def fake_run(_argv: list[str]) -> str | None:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1
        return "/tmp/x"

    monkeypatch.setattr(host_picker, "resolve", lambda: Helper("zenity", "zenity"))
    monkeypatch.setattr(host_picker, "_run", fake_run)
    _ = await asyncio.gather(*(host_picker.pick("file") for _ in range(4)))
    assert peak == 1
