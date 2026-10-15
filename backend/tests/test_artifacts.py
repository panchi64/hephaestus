"""The artifact store and its REST surface: capture/serve round-trip, encryption
at rest, conversation scoping, and the sandboxing headers on served content."""

from __future__ import annotations

from pathlib import Path

from core.db import init_db, make_engine
from core.vault import Vault
from routes.deps import app_state
from services.artifacts import ArtifactStore, guess_content_type

from ._helpers import client_app


async def _store(tmp_path: Path) -> ArtifactStore:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return ArtifactStore(engine, vault)


# --- content-type + kind inference -------------------------------------------
def test_guess_content_type_and_kind():
    assert guess_content_type("report.html") == "text/html"
    assert guess_content_type("chart.png") == "image/png"
    assert guess_content_type("notes") == "text/plain"  # unknown → text


# --- store round-trip --------------------------------------------------------
async def test_publish_then_serve_round_trip(tmp_path: Path):
    store = await _store(tmp_path)
    view = await store.publish(
        "operator", "conv-1", filename="report.html", content=b"<h1>hi</h1>", title="Report"
    )
    assert view.kind == "html"
    assert view.content_type == "text/html"
    assert view.size == 11

    blob = await store.content("operator", view.id)
    assert blob.content == b"<h1>hi</h1>"
    assert blob.content_type == "text/html"


async def test_content_is_encrypted_at_rest(tmp_path: Path):
    from sqlmodel import Session, select

    from models.artifact import Artifact

    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "k.json")
    await vault.setup("pw")
    store = ArtifactStore(engine, vault)

    view = await store.publish(
        "operator",
        "conv-1",
        filename="q3-layoffs.txt",
        content=b"SECRET-XYZ",
        title="Q3 layoffs",
    )
    with Session(engine) as session:
        row = session.exec(select(Artifact).where(Artifact.id == view.id)).one()
    assert b"SECRET-XYZ" not in row.blob_enc  # raw bytes on disk are ciphertext
    # The title and the filename are content too — a filename says as much about the
    # operator as the document it names — so neither survives in the clear.
    assert row.title is None and row.filename is None
    assert "layoffs" not in (row.title_enc or "")
    assert "layoffs" not in (row.filename_enc or "")
    # ...and they still read back through the store.
    assert view.title == "Q3 layoffs"
    assert view.filename == "q3-layoffs.txt"


async def test_legacy_cleartext_rows_still_read_until_the_backfill_seals_them(tmp_path: Path):
    # A database written before these columns were sealed must read correctly from the
    # first request — the backfill runs after unlock, not during the migration.
    from sqlmodel import Session, select

    from models.artifact import Artifact
    from services.sealing import seal_legacy_column

    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "k.json")
    await vault.setup("pw")
    store = ArtifactStore(engine, vault)

    view = await store.publish("operator", "conv-1", filename="f.txt", content=b"x")
    with Session(engine) as session:  # rewind this row to its pre-sealing shape
        row = session.exec(select(Artifact).where(Artifact.id == view.id)).one()
        row.title, row.filename = "Old Title", "old.txt"
        row.title_enc = row.filename_enc = None
        session.add(row)
        session.commit()

    blob = await store.content("operator", view.id)
    assert blob.filename == "old.txt"  # reads through the legacy column

    for legacy, sealed in (("title", "title_enc"), ("filename", "filename_enc")):
        _ = await seal_legacy_column(
            engine=engine,
            vault=vault,
            model_cls=Artifact,
            legacy_attr=legacy,
            sealed_attr=sealed,
        )

    with Session(engine) as session:
        healed = session.exec(select(Artifact).where(Artifact.id == view.id)).one()
    assert healed.title is None and healed.filename is None  # cleartext is gone
    assert (await store.content("operator", view.id)).filename == "old.txt"


# --- REST surface ------------------------------------------------------------
async def test_content_route_serves_inert_with_sandbox_headers():
    async with client_app() as (client, app):
        view = await app_state(app).artifacts.publish(
            "operator", "conv-1", filename="r.html", content=b"<b>x</b>", title="R"
        )
        resp = await client.get(f"/views/{view.id}/content")
        assert resp.status_code == 200
        assert resp.content == b"<b>x</b>"
        assert resp.headers["content-type"].startswith("text/html")
        assert "sandbox" in resp.headers["content-security-policy"]
        assert resp.headers["x-content-type-options"] == "nosniff"

        assert (await client.get("/views/nope/content")).status_code == 404
