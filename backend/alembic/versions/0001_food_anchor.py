"""food-db anchor: foods.barcode + pg_trgm + dedup unique constraints

Revision ID: 0001_food_anchor
Revises:
Create Date: 2026-05-20

Eerste Alembic-migratie. Het schema bestond al via Base.metadata.create_all();
deze migratie voegt alleen de delta toe die het database-anker nodig heeft.
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_food_anchor"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # foods.barcode — voor Open Food Facts barcode-lookup + lokale cache
    op.add_column("foods", sa.Column("barcode", sa.String(length=50), nullable=True))
    op.create_index("ix_foods_barcode", "foods", ["barcode"])
    # pg_trgm + trigram-index voor fuzzy food-matching op naam
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX IF NOT EXISTS ix_foods_name_trgm "
               "ON foods USING gin (name gin_trgm_ops)")
    # idempotency / dedup — race-proof maken (review-bevinding)
    op.create_unique_constraint(
        "uq_intake_user_client", "intake_entries", ["user_id", "client_entry_id"])
    op.create_unique_constraint(
        "uq_dto_user_date", "daily_target_overrides", ["user_id", "date"])


def downgrade():
    op.drop_constraint("uq_dto_user_date", "daily_target_overrides", type_="unique")
    op.drop_constraint("uq_intake_user_client", "intake_entries", type_="unique")
    op.execute("DROP INDEX IF EXISTS ix_foods_name_trgm")
    op.drop_index("ix_foods_barcode", table_name="foods")
    op.drop_column("foods", "barcode")
