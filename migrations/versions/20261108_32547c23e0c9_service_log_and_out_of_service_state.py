"""서비스 기록 테이블, 운행 중지 상태(OUT_OF_SERVICE) 추가

Revision ID: 32547c23e0c9
Revises: 079d7254b041
Create Date: 2026-11-08 22:21:13.865694

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "32547c23e0c9"
down_revision: str | Sequence[str] | None = "079d7254b041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATES = "'IDLE', 'MOVING_UP', 'MOVING_DOWN', 'DOORS_OPENING', 'DOORS_OPEN', 'DOORS_CLOSING'"
NEW_STATES = OLD_STATES + ", 'OUT_OF_SERVICE'"


def upgrade() -> None:
    op.create_table(
        "service_log",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("time_ms", sa.BigInteger(), nullable=False),
        sa.Column("car", sa.SmallInteger(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("detail", sa.String(length=32), server_default="", nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=True),
        sa.Column("message", sa.String(length=200), server_default="", nullable=False),
        sa.CheckConstraint(
            "kind IN ('FAULT', 'ALERT', 'OUT_OF_SERVICE', 'MAINTENANCE_REQUEST',"
            " 'REPAIR', 'RETURN')",
            name=op.f("ck_service_log_kind"),
        ),
        sa.CheckConstraint(
            "severity IN ('CRITICAL', 'WARNING')", name=op.f("ck_service_log_severity")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["simulation_runs.id"],
            name=op.f("fk_service_log_run_id_simulation_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_service_log")),
    )
    # 상태 목록에 OUT_OF_SERVICE를 더한다. autogenerate는 CHECK의 내용 변화를 감지하지 못해서
    # 직접 쓴다. MySQL은 CHECK를 고칠 수 없으므로 지우고 다시 만든다.
    # op.f(): 이미 완성된 이름이라는 표시. 없으면 metadata의 이름 규칙이 접두어를 한 번 더 붙인다.
    op.drop_constraint(op.f("ck_elevator_events_state"), "elevator_events", type_="check")
    op.create_check_constraint(
        op.f("ck_elevator_events_state"), "elevator_events", f"state IN ({NEW_STATES})"
    )


def downgrade() -> None:
    # 예전 CHECK를 다시 만들면 MySQL이 기존 행을 모두 검사한다. OUT_OF_SERVICE 행이 남아 있으면
    # 제약을 만들 수 없으므로 먼저 지운다 (되돌리면 운행 중지 기록은 사라진다).
    op.drop_constraint(op.f("ck_elevator_events_state"), "elevator_events", type_="check")
    op.execute("DELETE FROM elevator_events WHERE state = 'OUT_OF_SERVICE'")
    op.create_check_constraint(
        op.f("ck_elevator_events_state"), "elevator_events", f"state IN ({OLD_STATES})"
    )
    op.drop_table("service_log")
