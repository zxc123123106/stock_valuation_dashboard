"""Use database cascades for stock-owned records."""

from alembic import op
import sqlalchemy as sa


revision = "0002_stock_cascades"
down_revision = "0001_current_schema"
branch_labels = None
depends_on = None


STOCK_CHILD_TABLES = (
    "stock_metrics",
    "stock_eps",
    "stock_valuations",
    "stock_positions",
    "stock_broker_trading",
    "stock_daily_prices",
    "stock_pe_history",
    "stock_monthly_revenues",
    "stock_financial_quarters",
    "stock_institutional_trading",
    "stock_data_quality_states",
    "stock_ai_analyses",
)


def _replace_foreign_key(table_name: str, column: str, target_table: str, target_column: str, ondelete: str | None) -> None:
    bind = op.get_bind()
    matching = next(
        (
            foreign_key
            for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
            if foreign_key["constrained_columns"] == [column]
            and foreign_key["referred_table"] == target_table
        ),
        None,
    )
    if matching is None:
        return
    current_ondelete = (matching.get("options") or {}).get("ondelete")
    if (current_ondelete or "").upper() == (ondelete or "").upper():
        return

    naming_convention = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
    constraint_name = matching.get("name") or f"fk_{table_name}_{column}_{target_table}"
    with op.batch_alter_table(table_name, naming_convention=naming_convention) as batch_op:
        batch_op.drop_constraint(constraint_name, type_="foreignkey")
        batch_op.create_foreign_key(
            f"fk_{table_name}_{column}_{target_table}",
            target_table,
            [column],
            [target_column],
            ondelete=ondelete,
        )


def upgrade() -> None:
    for table_name in STOCK_CHILD_TABLES:
        _replace_foreign_key(table_name, "stock_id", "stocks", "id", "CASCADE")
    _replace_foreign_key("stock_broker_trading_rows", "broker_trading_id", "stock_broker_trading", "id", "CASCADE")
    _replace_foreign_key("stock_ai_feedback", "analysis_id", "stock_ai_analyses", "id", "CASCADE")
    _replace_foreign_key("stock_ai_feedback", "stock_id", "stocks", "id", "CASCADE")


def downgrade() -> None:
    for table_name in STOCK_CHILD_TABLES:
        _replace_foreign_key(table_name, "stock_id", "stocks", "id", None)
    _replace_foreign_key("stock_broker_trading_rows", "broker_trading_id", "stock_broker_trading", "id", None)
    _replace_foreign_key("stock_ai_feedback", "analysis_id", "stock_ai_analyses", "id", None)
    _replace_foreign_key("stock_ai_feedback", "stock_id", "stocks", "id", None)
