"""sessieverslag: workout_sessions.report (JSONB)

Revision ID: 0002_workout_report
Revises: 0001_food_anchor
Create Date: 2026-08-31

Het verslag wordt bij het opslaan van een workout berekend en hier bevroren.
Bewust opgeslagen en niet live berekend: de schema-targets worden na elke sessie
opgehoogd, dus een verslag dat later opnieuw wordt uitgerekend zou de sessie
langs een doel leggen dat tijdens die sessie nog niet bestond.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_workout_report"
down_revision = "0001_food_anchor"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workout_sessions",
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade():
    op.drop_column("workout_sessions", "report")
