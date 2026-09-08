"""计划版本与训练执行关联。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plans", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("plans", sa.Column("parent_plan_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("plans", sa.Column("adjustment_reason", sa.Text(), nullable=True))
    op.create_foreign_key("fk_plans_parent", "plans", "plans", ["parent_plan_id"], ["id"], ondelete="SET NULL")
    op.add_column("workout_logs", sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("workout_logs", sa.Column("plan_week", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_workout_logs_plan", "workout_logs", "plans", ["plan_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_workout_logs_plan_id", "workout_logs", ["plan_id"])


def downgrade() -> None:
    op.drop_index("ix_workout_logs_plan_id", table_name="workout_logs")
    op.drop_constraint("fk_workout_logs_plan", "workout_logs", type_="foreignkey")
    op.drop_column("workout_logs", "plan_week")
    op.drop_column("workout_logs", "plan_id")
    op.drop_constraint("fk_plans_parent", "plans", type_="foreignkey")
    op.drop_column("plans", "adjustment_reason")
    op.drop_column("plans", "parent_plan_id")
    op.drop_column("plans", "version")
