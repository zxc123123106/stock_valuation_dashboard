from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.ai_analysis import (
    AI_MODE_HELD,
    AI_MODE_UNHELD,
    PROMPT_VERSION,
    RULE_VERSION,
    AIAnalysisError,
    AIProviderBatchResult,
    AIProviderResult,
    OpenRouterProvider,
)
from backend.app.db.models import AIProviderHealth, Base, Stock, StockAIAnalysis, StockAIAnalysisRun
from backend.app.schema.ai import StockAIAnalysisContent, StockAIAnalysisEvidenceText
from backend.app.services.ai_batch_service import (
    _execute_analysis_run,
    _provider_order,
    _provider_specs,
    run_analysis_job_in_session,
)


def content(status: str, *, valid: bool = True) -> StockAIAnalysisContent:
    return StockAIAnalysisContent(
        overall_status=status,
        summary=StockAIAnalysisEvidenceText(
            text="目前資料顯示估值、基本面與技術訊號仍需共同確認。",
            evidence_keys=["quote.current_price_twd"],
        ),
        positive_points=[],
        risk_points=[],
        watch_points=[],
        disclaimer="僅供資料整理。",
        format_valid=valid,
    )


def provider_result(status: str, *, valid: bool = True) -> AIProviderResult:
    return AIProviderResult(
        analysis=content(status, valid=valid),
        raw_response_text="{}",
        provider_metadata={"test": True},
        validation_errors=[] if valid else ["Missing required field: summary"],
        quality_flags=[] if valid else ["format_issue"],
        grounding_errors=[],
    )


def make_run(session, modes: list[str]) -> StockAIAnalysisRun:
    stock = Stock(symbol="4958", name="臻鼎-KY", asset_type="STOCK", is_active=True)
    session.add(stock)
    session.commit()
    session.refresh(stock)
    summaries = {
        mode: {
            "symbol": "4958",
            "analysis_mode": mode,
            "analysis_context": {"analysis_requested_at": "2026-07-16T10:00:00+08:00"},
            "quote": {"current_price_twd": 590},
            "evidence": {"quote.current_price_twd": 590},
            "available_evidence_keys": ["quote.current_price_twd"],
        }
        for mode in modes
    }
    rules = {
        AI_MODE_UNHELD: content("等待").model_dump(mode="json"),
        AI_MODE_HELD: content("觀察").model_dump(mode="json"),
    }
    snapshot = {
        "requested_modes": modes,
        "modes": summaries,
        "rule_results": {mode: rules[mode] for mode in modes},
    }
    run = StockAIAnalysisRun(
        stock_id=stock.id,
        provider="openrouter",
        model="primary:free",
        prompt_version=PROMPT_VERSION,
        rule_version=RULE_VERSION,
        requested_modes_json=json.dumps(modes),
        analysis_snapshot_json=json.dumps(snapshot),
        snapshot_hash="snapshot",
        rule_results_json=json.dumps(snapshot["rule_results"]),
        data_as_of_json="[]",
        stale_items_json="[]",
        request_strategy="batch" if len(modes) > 1 else "single",
        status="queued",
        requested_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


class BatchProvider:
    provider_id = "openrouter"
    model = "primary:free"

    def __init__(self, *, invalid_held: bool = False):
        self.invalid_held = invalid_held
        self.batch_calls = 0
        self.single_calls: list[str] = []

    def analyze_stock_batch(self, stock_summaries, rule_statuses):
        self.batch_calls += 1
        return AIProviderBatchResult(
            analyses={
                mode: provider_result(rule_statuses[mode], valid=not (self.invalid_held and mode == AI_MODE_HELD))
                for mode in stock_summaries
            },
            raw_response_text="{}",
            provider_metadata={"test": True},
        )

    def analyze_stock(self, stock_summary, analysis_mode, rule_status=None):
        self.single_calls.append(analysis_mode)
        return provider_result(rule_status or "等待")


class FailingBatchProvider(BatchProvider):
    def analyze_stock_batch(self, stock_summaries, rule_statuses):
        self.batch_calls += 1
        raise AIAnalysisError("OpenRouter API request failed with status 429.", status_code=429)

    def analyze_stock(self, stock_summary, analysis_mode, rule_status=None):
        raise AssertionError("HTTP provider errors must not split into single-mode requests")


class AIBatchServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)

    def tearDown(self) -> None:
        self.engine.dispose()

    @patch("backend.app.ai_analysis.requests.post")
    def test_openrouter_batch_request_injects_rule_owned_statuses(self, post) -> None:
        post.return_value.status_code = 200
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {
            "id": "batch",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps({
                    "analyses": {
                        "unheld": {
                            "summary": {"text": "估值與技術訊號尚未一致，應等待資料確認。", "evidence_keys": ["quote.current_price_twd"]},
                            "positive_points": [], "risk_points": [], "watch_points": [],
                        },
                        "held": {
                            "summary": {"text": "持有理由仍需由成本、估值與趨勢共同確認。", "evidence_keys": ["quote.current_price_twd"]},
                            "positive_points": [], "risk_points": [], "watch_points": [],
                        },
                    }
                }, ensure_ascii=False)},
            }],
        }
        provider = OpenRouterProvider(api_key="test", model="model:free")
        summaries = {
            AI_MODE_UNHELD: {"evidence": {"quote.current_price_twd": 590}},
            AI_MODE_HELD: {"evidence": {"quote.current_price_twd": 590}},
        }

        result = provider.analyze_stock_batch(
            summaries,
            {AI_MODE_UNHELD: "等待", AI_MODE_HELD: "續抱"},
        )

        self.assertEqual(post.call_count, 1)
        schema = post.call_args.kwargs["json"]["response_format"]["json_schema"]["schema"]
        self.assertNotIn("overall_status", schema["properties"]["analyses"]["properties"]["unheld"]["properties"])
        self.assertEqual(result.analyses[AI_MODE_UNHELD].analysis.overall_status, "等待")
        self.assertEqual(result.analyses[AI_MODE_HELD].analysis.overall_status, "續抱")

    def test_provider_order_is_authoritative_and_paid_openrouter_model_is_unavailable(self) -> None:
        test_settings = SimpleNamespace(
            ai_provider="gemini",
            ai_provider_order=["openrouter", "gemini"],
            openrouter_api_key="test",
            openrouter_model="qwen/qwen3-next-80b-a3b-instruct",
            openrouter_fallback_models=[],
            gemini_api_key="test",
            gemini_model="gemini-flash",
        )
        with patch("backend.app.services.ai_batch_service.settings", test_settings):
            self.assertEqual(_provider_order(), ["openrouter", "gemini"])
            specs = _provider_specs()

        self.assertEqual(specs[0][:3], ("openrouter", "qwen/qwen3-next-80b-a3b-instruct", False))
        self.assertIn(":free", specs[0][3])
        self.assertEqual(specs[1][:3], ("gemini", "gemini-flash", True))

    def test_batch_format_failure_splits_only_invalid_mode(self) -> None:
        provider = BatchProvider(invalid_held=True)
        with self.Session() as session:
            run = make_run(session, [AI_MODE_UNHELD, AI_MODE_HELD])
            with (
                patch("backend.app.services.ai_batch_service._available_provider_specs", return_value=[("openrouter", provider.model)]),
                patch("backend.app.services.ai_batch_service._build_provider", return_value=provider),
            ):
                _execute_analysis_run(session, run)
            session.refresh(run)
            rows = session.scalars(select(StockAIAnalysis).where(StockAIAnalysis.run_id == run.id)).all()

        self.assertEqual(provider.batch_calls, 1)
        self.assertEqual(provider.single_calls, [AI_MODE_HELD])
        self.assertEqual(run.status, "success")
        self.assertIn("split_fallback", run.request_strategy)
        self.assertEqual(sum(row.status == "success" for row in rows), 2)

    def test_429_switches_provider_without_splitting(self) -> None:
        primary = FailingBatchProvider()
        secondary = BatchProvider()
        secondary.provider_id = "gemini"
        secondary.model = "gemini-free"
        with self.Session() as session:
            run = make_run(session, [AI_MODE_UNHELD, AI_MODE_HELD])
            with (
                patch(
                    "backend.app.services.ai_batch_service._available_provider_specs",
                    return_value=[("openrouter", primary.model), ("gemini", secondary.model)],
                ),
                patch(
                    "backend.app.services.ai_batch_service._build_provider",
                    side_effect=[primary, secondary],
                ),
            ):
                _execute_analysis_run(session, run)
            session.refresh(run)
            primary_health = session.scalar(select(AIProviderHealth).where(AIProviderHealth.provider == "openrouter"))

        self.assertEqual(primary.batch_calls, 1)
        self.assertEqual(primary.single_calls, [])
        self.assertEqual(secondary.batch_calls, 1)
        self.assertEqual(run.status, "success")
        self.assertIn("provider_fallback", run.request_strategy)
        self.assertEqual(primary_health.status, "COOLDOWN")
        self.assertEqual(primary_health.last_http_status, 429)

    def test_unexpected_background_failure_releases_run_lock(self) -> None:
        with self.Session() as session:
            run = make_run(session, [AI_MODE_UNHELD])
            with patch(
                "backend.app.services.ai_batch_service._execute_analysis_run",
                side_effect=ValueError("unexpected payload"),
            ):
                run_analysis_job_in_session(session, run.id)
            session.refresh(run)

        self.assertEqual(run.status, "failed")
        self.assertIn("unexpected payload", run.error_message)
        self.assertIsNotNone(run.finished_at)


if __name__ == "__main__":
    unittest.main()
