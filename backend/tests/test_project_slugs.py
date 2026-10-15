"""A project's folder name in the agent's home: derived once, unique, path-safe."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlmodel import Session

from core.db import ALEMBIC_INI, make_engine
from services.projects.slugs import slug_for


def test_a_name_becomes_a_path_segment() -> None:
    assert slug_for("My API", set()) == "my-api"
    assert slug_for("  Recipes & Meals!  ", set()) == "recipes-meals"
    assert slug_for("odysseus.backend_v2", set()) == "odysseus.backend_v2"
    assert slug_for("!!!", set()) == "project"


def test_a_clash_is_numbered_and_stays_inside_the_length() -> None:
    assert slug_for("api", {"api"}) == "api-2"
    assert slug_for("api", {"api", "api-2"}) == "api-3"
    long = "x" * 60
    first = slug_for(long, set())
    assert len(first) == 40
    second = slug_for(long, {first})
    assert len(second) == 40 and second.endswith("-2")


def test_the_migration_gives_existing_projects_unique_slugs_oldest_first() -> None:
    engine = make_engine("sqlite:///:memory:")
    config = Config(str(ALEMBIC_INI))
    config.attributes["connection"] = engine
    command.upgrade(config, "f2a9d4c7b1e6")
    with Session(engine) as session:
        _ = session.connection().execute(
            text(
                "INSERT INTO projects (id, owner_id, name, root_path_enc, git_initialized, "
                + "base_ref, archived, created_at, last_opened_at) VALUES "
                + "('new', 'op', 'API', 'x', 0, 'HEAD', 0, '2026-02-01', '2026-02-01'), "
                + "('old', 'op', 'api', 'x', 0, 'HEAD', 0, '2026-01-01', '2026-01-01'), "
                + "('other', 'op2', 'API', 'x', 0, 'HEAD', 0, '2026-03-01', '2026-03-01')"
            )
        )
        session.commit()

    command.upgrade(config, "head")

    with Session(engine) as session:
        rows = cast(
            Sequence[tuple[str, str]],
            session.connection().execute(text("SELECT id, slug FROM projects")).all(),
        )
    assert dict(rows) == {
        "old": "api",
        "new": "api-2",
        "other": "api",
    }
