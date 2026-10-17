"""Reading a path the model wrote onto the home's layout, as text."""

from __future__ import annotations

import pytest

from services.home.paths import HOME, SCRATCH, HomePath, HomePathError, Place, parse

API = Place("projects", "api")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("~/scratch/notes.md", HomePath(SCRATCH, "notes.md")),
        ("/work/notes.md", HomePath(SCRATCH, "notes.md")),
        ("/work", HomePath(SCRATCH, "")),
        ("~/projects/api", HomePath(API, "")),
        ("~/projects/api/src/x.py", HomePath(API, "src/x.py")),
        (f"{HOME}/projects/api/./src//x.py", HomePath(API, "src/x.py")),
        ("~/projects/api/src/../README.md", HomePath(API, "README.md")),
        ("~/worktrees/api-fix/a.txt", HomePath(Place("worktrees", "api-fix"), "a.txt")),
        ("~/shared/recipes/chili.md", HomePath(Place("shared", "recipes"), "chili.md")),
    ],
)
def test_a_path_names_its_place_and_what_is_in_it(raw: str, expected: HomePath) -> None:
    assert parse(raw) == expected


def test_a_relative_path_reads_from_the_working_directory() -> None:
    assert parse("notes.md") == HomePath(SCRATCH, "notes.md")
    assert parse("src/x.py", cwd=f"{HOME}/projects/api") == HomePath(API, "src/x.py")
    assert parse("../x.py", cwd=f"{HOME}/projects/api/src") == HomePath(API, "x.py")


@pytest.mark.parametrize(
    "raw",
    [
        "/etc/passwd",
        "~/../root",
        "~",
        "~/projects",
        "~/projects/",
        "~/elsewhere/x",
        "~/projects/API",
        "~/projects/.hidden",
        "~/scratch/../projects/api/x",
        "~/projects/api/../web/x",
        "",
        "a\0b",
    ],
)
def test_a_path_that_names_no_place_or_climbs_out_is_refused(raw: str) -> None:
    with pytest.raises(HomePathError):
        _ = parse(raw)


def test_climbing_out_of_the_working_directory_is_refused_too() -> None:
    with pytest.raises(HomePathError, match="climbs out"):
        _ = parse("../web/x.py", cwd=f"{HOME}/projects/api")


def test_a_refusal_says_what_to_do_instead() -> None:
    with pytest.raises(HomePathError) as caught:
        _ = parse("/etc/passwd")
    assert caught.value.hint is not None and "~/scratch" in caught.value.hint


def test_a_place_and_a_path_render_as_the_model_sees_them() -> None:
    assert SCRATCH.path == f"{HOME}/scratch"
    assert HomePath(API, "src/x.py").path == f"{HOME}/projects/api/src/x.py"
    assert HomePath(API).path == f"{HOME}/projects/api"
