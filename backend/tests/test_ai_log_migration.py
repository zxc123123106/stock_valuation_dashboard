from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text

from backend.app.database import _remove_legacy_ai_analysis_unique_constraint


class AIAnalysisLogMigrationTest(unittest.TestCase):
    def test_legacy_cache_constraint_is_removed_without_losing_rows(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE stocks (id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO stocks (id) VALUES (1)"))
            connection.execute(
                text(
                    """
                    CREATE TABLE stock_ai_analyses (
                        id INTEGER PRIMARY KEY,
                        stock_id INTEGER NOT NULL,
                        provider VARCHAR(24) NOT NULL,
                        model VARCHAR(120) NOT NULL,
                        analysis_mode VARCHAR(16) NOT NULL DEFAULT 'GENERAL',
                        prompt_version VARCHAR(40) NOT NULL DEFAULT 'v1',
                        analysis_date DATE NOT NULL,
                        input_hash VARCHAR(64) NOT NULL,
                        request_payload_json TEXT NOT NULL,
                        response_json TEXT NOT NULL,
                        raw_response_text TEXT,
                        provider_metadata_json TEXT,
                        validation_errors_json TEXT,
                        status VARCHAR(24) NOT NULL,
                        error_message VARCHAR(500),
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        CONSTRAINT uq_stock_ai_analysis_cache UNIQUE (
                            stock_id, provider, model, analysis_date, input_hash
                        )
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO stock_ai_analyses VALUES (
                        1, 1, 'openrouter', 'model', 'GENERAL', 'v1', '2026-06-18',
                        'same-input', '{}', '{}', NULL, NULL, NULL, 'success', NULL,
                        '2026-06-18', '2026-06-18'
                    )
                    """
                )
            )

            _remove_legacy_ai_analysis_unique_constraint(connection)
            connection.execute(
                text(
                    """
                    INSERT INTO stock_ai_analyses (
                        stock_id, provider, model, analysis_mode, prompt_version,
                        analysis_date, input_hash, request_payload_json, response_json,
                        status, created_at, updated_at
                    ) VALUES (
                        1, 'openrouter', 'model', 'UNHELD', 'v2-dual-mode',
                        '2026-06-18', 'same-input', '{}', '{}', 'success',
                        '2026-06-18', '2026-06-18'
                    )
                    """
                )
            )

            self.assertEqual(connection.scalar(text("SELECT COUNT(*) FROM stock_ai_analyses")), 2)
            table_sql = connection.scalar(
                text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'stock_ai_analyses'")
            )
            self.assertNotIn("uq_stock_ai_analysis_cache", table_sql)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
