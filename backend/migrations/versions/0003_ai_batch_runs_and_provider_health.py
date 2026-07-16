"""Add AI batch runs and persistent provider health."""

from alembic import op
import sqlalchemy as sa


revision = "0003_ai_batch_health"
down_revision = "0002_stock_cascades"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    tables = _tables()
    if "stock_ai_analysis_runs" not in tables:
        op.create_table(
            "stock_ai_analysis_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("stock_id", sa.Integer(), sa.ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("provider", sa.String(24), nullable=True),
            sa.Column("model", sa.String(120), nullable=True),
            sa.Column("prompt_version", sa.String(40), nullable=False),
            sa.Column("rule_version", sa.String(40), nullable=False),
            sa.Column("requested_modes_json", sa.Text(), nullable=False),
            sa.Column("analysis_snapshot_json", sa.Text(), nullable=False),
            sa.Column("snapshot_hash", sa.String(64), nullable=False),
            sa.Column("rule_results_json", sa.Text(), nullable=False),
            sa.Column("data_as_of_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("stale_items_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("request_strategy", sa.String(64), nullable=False, server_default="batch"),
            sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
            sa.Column("provider_metadata_json", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_stock_ai_analysis_runs_stock_id", "stock_ai_analysis_runs", ["stock_id"])
        op.create_index("ix_stock_ai_analysis_runs_provider", "stock_ai_analysis_runs", ["provider"])
        op.create_index("ix_stock_ai_analysis_runs_prompt_version", "stock_ai_analysis_runs", ["prompt_version"])
        op.create_index("ix_stock_ai_analysis_runs_rule_version", "stock_ai_analysis_runs", ["rule_version"])
        op.create_index("ix_stock_ai_analysis_runs_snapshot_hash", "stock_ai_analysis_runs", ["snapshot_hash"])
        op.create_index("ix_stock_ai_analysis_runs_status", "stock_ai_analysis_runs", ["status"])

    tables = _tables()
    if "ai_provider_health" not in tables:
        op.create_table(
            "ai_provider_health",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("provider", sa.String(24), nullable=False),
            sa.Column("model", sa.String(120), nullable=False),
            sa.Column("status", sa.String(24), nullable=False, server_default="HEALTHY"),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_http_status", sa.Integer(), nullable=True),
            sa.Column("last_error_summary", sa.Text(), nullable=True),
            sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("provider", "model", name="uq_ai_provider_health_identity"),
        )
        op.create_index("ix_ai_provider_health_provider", "ai_provider_health", ["provider"])
        op.create_index("ix_ai_provider_health_model", "ai_provider_health", ["model"])
        op.create_index("ix_ai_provider_health_status", "ai_provider_health", ["status"])
        op.create_index("ix_ai_provider_health_cooldown_until", "ai_provider_health", ["cooldown_until"])

    if "run_id" not in _columns("stock_ai_analyses"):
        with op.batch_alter_table("stock_ai_analyses") as batch_op:
            batch_op.add_column(sa.Column("run_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_stock_ai_analyses_run_id_stock_ai_analysis_runs",
                "stock_ai_analysis_runs",
                ["run_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch_op.create_index("ix_stock_ai_analyses_run_id", ["run_id"])


def downgrade() -> None:
    if "run_id" in _columns("stock_ai_analyses"):
        with op.batch_alter_table("stock_ai_analyses") as batch_op:
            batch_op.drop_index("ix_stock_ai_analyses_run_id")
            batch_op.drop_constraint("fk_stock_ai_analyses_run_id_stock_ai_analysis_runs", type_="foreignkey")
            batch_op.drop_column("run_id")
    tables = _tables()
    if "ai_provider_health" in tables:
        op.drop_table("ai_provider_health")
    if "stock_ai_analysis_runs" in tables:
        op.drop_table("stock_ai_analysis_runs")
