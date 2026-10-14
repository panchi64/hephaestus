"""The development reloader must ignore runtime state — and say so when it can't.

The exclusion tests run through **uvicorn's own filter** rather than a reimplementation of
it, because the failure mode being guarded is precisely that a plausible-looking exclusion
is accepted and then quietly ignored: a file the app writes under ``data/`` restarts the
server mid-request, and the operator sees the work die with no error attached to it.
"""

from __future__ import annotations

from pathlib import Path

from uvicorn.config import Config
from uvicorn.supervisors.watchfilesreload import FileFilter

from core.devserver import reload_excludes, reload_watches_runtime_state


def _watches(excludes: list[str], path: Path) -> bool:
    """Whether uvicorn's reloader would restart the server for a change to ``path``."""
    return FileFilter(Config("app:app", reload=True, reload_excludes=excludes))(path)


def _laid_out(tmp_path: Path) -> tuple[Path, list[str]]:
    """A backend tree as `dev.main` leaves it: data directory created, venv present."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (tmp_path / ".venv").mkdir()
    return data_dir, reload_excludes(data_dir, tmp_path)


# --- what dev.py excludes ---------------------------------------------------


def test_runtime_writes_do_not_restart_the_server(tmp_path: Path):
    """The bug this file exists for: the app writing ``*.py`` under its own data
    directory — an upload, a file the agent creates in its sandbox workspace — used to
    reload the server in the middle of the request that wrote it."""
    data_dir, excludes = _laid_out(tmp_path)

    written = data_dir / "sandbox/workspace/scratch.py"
    assert not _watches(excludes, written)


def test_dependency_installs_do_not_restart_the_server(tmp_path: Path):
    """`uv sync` rewrites the project virtualenv, which uvicorn otherwise watches — its
    default excludes only cover *dot-prefixed filenames*, never a dot-prefixed dir."""
    _, excludes = _laid_out(tmp_path)

    assert not _watches(excludes, tmp_path / ".venv/lib/site-packages/httpx/_client.py")


def test_source_edits_still_restart_the_server(tmp_path: Path):
    """The exclusions must not be so broad that the reloader stops being useful."""
    _, excludes = _laid_out(tmp_path)

    assert _watches(excludes, tmp_path / "app.py")
    assert _watches(excludes, tmp_path / "services/registry.py")


def test_exclusions_are_absolute(tmp_path: Path):
    """Uvicorn matches an exclusion against a changed file's parents only when the entry
    resolves to a directory; a *relative* entry is kept as a filename glob and matches
    nothing, so `--reload-exclude data` reads as correct and silently does nothing.
    Guarding the property directly keeps a future edit from regressing to the quiet form."""
    _, excludes = _laid_out(tmp_path)

    assert excludes
    assert all(Path(entry).is_absolute() for entry in excludes)


def test_excludes_the_configured_data_dir_not_a_hardcoded_one(tmp_path: Path):
    """`HEPHAESTUS_DATA_DIR` moves where the app writes, so it has to move the exclusion
    too — otherwise the trap returns for anyone who relocates their data."""
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()

    excludes = reload_excludes(elsewhere, tmp_path)

    assert not _watches(excludes, elsewhere / "sandbox/workspace/scratch.py")


def test_missing_directories_are_dropped_rather_than_passed_through(tmp_path: Path):
    """A directory that doesn't exist yet can't be excluded as one — uvicorn falls back
    to treating the entry as a glob, and an *absolute* glob makes `Path.match` raise on
    Python 3.14. So the reloader wouldn't merely ignore the entry, it would blow up on
    the first source edit. Passing only real directories is what keeps that from
    reaching the operator."""
    excludes = reload_excludes(tmp_path / "never-created", tmp_path)

    assert excludes == []
    assert _watches(excludes, tmp_path / "app.py")  # and the filter still runs at all


# --- warning when the guard was bypassed ------------------------------------


def test_the_bare_reload_flag_is_reported(tmp_path: Path):
    """`uvicorn app:app --reload` is muscle memory and lives in shell history. Nothing in
    the app can stop the reloader — it is the parent process — so the least it can do is
    name the cause, since the symptom (a run that dies silently) points nowhere near."""
    argv = ["uvicorn", "app:app", "--reload", "--port", "8000"]

    assert reload_watches_runtime_state(argv, tmp_path / "data")


def test_dev_py_does_not_warn(tmp_path: Path):
    """`dev.py` turns reload on programmatically, so the flag never reaches argv — which
    is exactly what makes its presence a reliable sign the guard was bypassed."""
    assert not reload_watches_runtime_state(["python", "dev.py"], tmp_path / "data")


def test_production_does_not_warn(tmp_path: Path):
    assert not reload_watches_runtime_state(["uvicorn", "app:app"], tmp_path / "data")


def test_an_absolute_exclusion_covering_the_data_dir_clears_the_warning(tmp_path: Path):
    """Someone who did the work by hand shouldn't be nagged — and a parent directory
    covers it, since uvicorn matches an exclusion against every parent of a changed file."""
    data_dir = tmp_path / "data"

    for excluded in (str(data_dir), str(tmp_path)):
        assert not reload_watches_runtime_state(
            ["uvicorn", "app:app", "--reload", "--reload-exclude", excluded], data_dir
        )
        assert not reload_watches_runtime_state(
            ["uvicorn", "app:app", "--reload", f"--reload-exclude={excluded}"], data_dir
        )


def test_a_relative_exclusion_still_warns(tmp_path: Path):
    """The trap this whole file guards: `--reload-exclude data` protects nothing, because
    uvicorn keeps it relative while watchfiles reports absolute paths. Treating it as
    protection would silence the warning in the one case that most needs it."""
    argv = ["uvicorn", "app:app", "--reload", "--reload-exclude", "data"]

    assert reload_watches_runtime_state(argv, tmp_path / "data")


def test_an_unrelated_exclusion_still_warns(tmp_path: Path):
    """Excluding some other directory says nothing about the data directory."""
    argv = ["uvicorn", "app:app", "--reload", "--reload-exclude", str(tmp_path / "logs")]

    assert reload_watches_runtime_state(argv, tmp_path / "data")
