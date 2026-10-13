"""dropped the service credentials nothing calls any more

Three outbound services existed only for surfaces that have been removed. Artificial
Analysis and llm-stats supplied model-quality rankings to the Cookbook's
recommendations; the HuggingFace token lifted the anonymous rate limit while downloading
weights. Hephaestus works off endpoints now — it ranks no models and downloads no weights —
so nothing reads any of the three, and their rows in `KNOWN_SERVICES` are gone.

Dropping the catalog entry alone would leave any stored key sealed in this table and
**unreachable**: `status()` returns whatever rows exist, but the Service Keys section
renders one row per *declared* service, so an operator could neither see the key nor
clear it. A secret you cannot find is worse than one you can, so the rows go with the
declaration.

This deletes data, and the downgrade cannot bring it back — the plaintext was never
recoverable from this table by design (it is sealed with the vault key), and re-adding
empty rows would only be a lie about what is stored. An operator who wants one of these
keys back re-enters it, against whatever declares it next.

Revision ID: b7ad41c9e208
Revises: e41b7c95d0a3
Create Date: 2026-08-29 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7ad41c9e208"
down_revision: Union[str, None] = "e41b7c95d0a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RETIRED_SERVICES = ("artificial_analysis", "llm_stats", "huggingface")


def upgrade() -> None:
    # Parameterised rather than interpolated, and scoped to exactly these three ids —
    # a broad "delete anything not in KNOWN_SERVICES" would couple this migration to a
    # constant that keeps changing, and would silently eat a row belonging to a service
    # added after it.
    op.execute(
        sa.text(
            "DELETE FROM service_credentials WHERE service IN (:a, :b, :c)"
        ).bindparams(
            a=RETIRED_SERVICES[0], b=RETIRED_SERVICES[1], c=RETIRED_SERVICES[2]
        )
    )


def downgrade() -> None:
    # Nothing to restore. The keys were sealed, and this migration deleted the only
    # rows that held them; re-creating empty rows would claim a credential is stored
    # when none is. The schema is unchanged either way, so the chain still unwinds.
    pass
