"""실행, 엘리베이터 상태 변화, 승객 기록 테이블 생성

Revision ID: 079d7254b041
Revises:
Create Date: 2026-11-08 21:53:58.148700

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "079d7254b041"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "simulation_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("strategy", sa.String(length=32), nullable=False),
        sa.Column("pattern", sa.String(length=16), nullable=True),
        sa.Column("rate_per_min", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("seed", sa.Integer(), nullable=True),
        sa.Column("num_floors", sa.SmallInteger(), nullable=False),
        sa.Column("num_elevators", sa.SmallInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("sim_ms", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "source IN ('experiment', 'live')", name=op.f("ck_simulation_runs_source")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_simulation_runs")),
    )
    op.create_index(
        op.f("ix_simulation_runs_source_strategy_pattern"),
        "simulation_runs",
        ["source", "strategy", "pattern"],
        unique=False,
    )
    op.create_table(
        "elevator_events",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("car", sa.SmallInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("time_ms", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("floor", sa.SmallInteger(), nullable=False),
        sa.Column("direction", sa.String(length=4), nullable=False),
        sa.CheckConstraint(
            "direction IN ('UP', 'DOWN', 'NONE')", name=op.f("ck_elevator_events_direction")
        ),
        sa.CheckConstraint(
            "state IN ('IDLE', 'MOVING_UP', 'MOVING_DOWN',"
            " 'DOORS_OPENING', 'DOORS_OPEN', 'DOORS_CLOSING')",
            name=op.f("ck_elevator_events_state"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["simulation_runs.id"],
            name=op.f("fk_elevator_events_run_id_simulation_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "car", "seq", name=op.f("pk_elevator_events")),
    )
    op.create_table(
        "passenger_trips",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("passenger_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("origin", sa.SmallInteger(), nullable=False),
        sa.Column("destination", sa.SmallInteger(), nullable=False),
        sa.Column("arrival_ms", sa.BigInteger(), nullable=False),
        sa.Column("board_ms", sa.BigInteger(), nullable=True),
        sa.Column("alight_ms", sa.BigInteger(), nullable=True),
        sa.Column("car", sa.SmallInteger(), nullable=True),
        sa.Column(
            "wait_ms",
            sa.BigInteger(),
            sa.Computed("board_ms - arrival_ms", persisted=True),
            nullable=True,
        ),
        sa.Column(
            "journey_ms",
            sa.BigInteger(),
            sa.Computed("alight_ms - arrival_ms", persisted=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "origin <> destination", name=op.f("ck_passenger_trips_different_floors")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["simulation_runs.id"],
            name=op.f("fk_passenger_trips_run_id_simulation_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "passenger_id", name=op.f("pk_passenger_trips")),
    )


def downgrade() -> None:
    # 인덱스는 테이블과 함께 지워진다. 자식 테이블(FK를 가진 쪽)부터 지운다.
    op.drop_table("passenger_trips")
    op.drop_table("elevator_events")
    op.drop_table("simulation_runs")
