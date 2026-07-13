from __future__ import annotations

import json
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.ai_analysis import (
    AI_MODE_HELD,
    AI_MODE_UNHELD,
    AIConfigurationError,
    AIAnalysisError,
    AIProviderResult,
    OpenRouterProvider,
    PROMPT_VERSION,
    _message_content_text,
    normalize_ai_analysis,
    normalize_ai_analysis_with_errors,
    stock_summary_hash,
)
from backend.app.database import Base, Stock, StockAIAnalysis, StockPosition
from backend.app.services.application import (
    _ai_analysis_is_cacheable,
    _ai_analysis_result_response,
    _ai_cache_input_hash,
    _ai_stock_summary,
    _compact_ai_stock_summary,
)
from backend.app.services.application import _ai_analysis_is_fresh_inflight
from backend.app.services.application import _ai_failure_http_status_code, _generate_ai_mode, _generate_ai_mode_with_fallback
from backend.app.services.application import _enqueue_ai_mode_with_fallback, _run_ai_analysis_job
from backend.app.api.ai import ai_analysis_logs_summary, create_stock_ai_analysis, submit_stock_ai_analysis_feedback
from backend.app.schemas import StockAIAnalysisContent, StockAIAnalysisEvidenceText, StockAIAnalysisFeedbackRequest, StockAIAnalysisRequest


def analysis_text(value):
    return value.text if hasattr(value, "text") else value


def analysis_texts(values):
    return [analysis_text(value) for value in values]


class FailingProvider:
    provider_id = "openrouter"
    model = "openai/gpt-oss-120b:free"

    def analyze_stock(self, stock_summary, analysis_mode):
        raise AIAnalysisError("OpenRouter API request failed with status 429. Provider returned error")


class FallbackTestOpenRouterProvider:
    provider_id = "openrouter"
    api_key = "test"
    timeout_seconds = 45

    def __init__(self, api_key="test", model="primary-model", timeout_seconds=45):
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def analyze_stock(self, stock_summary, analysis_mode):
        if self.model == "primary-model":
            raise AIAnalysisError("OpenRouter API request failed with status 429. Provider returned error")
        analysis, errors = normalize_ai_analysis_with_errors(
            {
                "overall_status": "等待",
                "summary": {
                    "text": "目前估值、基本面、技術面與籌碼訊號仍然分歧，尚未形成一致方向，適合等待既有資料確認。",
                    "evidence_keys": ["quote.current_price_twd"],
                },
                "positive_points": [],
                "risk_points": [],
                "watch_points": [],
                "disclaimer": "僅供資料整理。",
            },
            analysis_mode,
            evidence_keys={"quote.current_price_twd"},
        )
        return AIProviderResult(
            analysis=analysis,
            raw_response_text="{}",
            provider_metadata={"test_model": self.model},
            validation_errors=errors,
            quality_flags=[],
            grounding_errors=[],
        )


class AIAnalysisTest(unittest.TestCase):
    def test_stock_summary_hash_is_stable(self) -> None:
        left = {"symbol": "4958", "metric": {"current_pe": 77.86, "current_price": 592}}
        right = {"metric": {"current_price": 592, "current_pe": 77.86}, "symbol": "4958"}

        self.assertEqual(stock_summary_hash(left), stock_summary_hash(right))

    def test_analysis_time_is_in_payload_but_not_cache_hash(self) -> None:
        base = {
            "symbol": "4958",
            "name": "臻鼎-KY",
            "asset_type": "STOCK",
            "market": "TWSE",
            "currency": "TWD",
            "quote": {"current_price_twd": 590},
        }
        morning = _compact_ai_stock_summary(
            base,
            AI_MODE_UNHELD,
            datetime(2026, 7, 13, 9, 5, 6, tzinfo=UTC),
        )
        evening = _compact_ai_stock_summary(
            base,
            AI_MODE_UNHELD,
            datetime(2026, 7, 13, 12, 30, 45, tzinfo=UTC),
        )

        self.assertEqual(morning["analysis_context"]["analysis_requested_at"], "2026-07-13T17:05:06+08:00")
        self.assertEqual(morning["analysis_context"]["local_date"], "2026-07-13")
        self.assertEqual(morning["analysis_context"]["local_time"], "17:05:06")
        self.assertEqual(morning["analysis_context"]["timezone"], "Asia/Taipei")
        self.assertIn("analysis_context.analysis_requested_at", morning["available_evidence_keys"])
        self.assertNotEqual(stock_summary_hash(morning), stock_summary_hash(evening))
        self.assertEqual(_ai_cache_input_hash(morning), _ai_cache_input_hash(evening))

    def test_normalize_ai_analysis_extracts_fenced_json(self) -> None:
        analysis = normalize_ai_analysis(
            """```json
            {
              "overall_status": "觀察",
              "summary": "估值偏高但技術仍偏強，後續需確認基本面是否支撐。",
              "positive_points": ["股價仍高於 MA20"],
              "risk_points": ["目前 PE 偏高"],
              "watch_points": ["月營收 YoY 是否維持成長"]
            }
            ```"""
        )

        self.assertEqual(analysis.overall_status, "觀察")
        self.assertEqual(analysis_texts(analysis.positive_points), ["股價仍高於 MA20"])
        self.assertEqual(analysis.disclaimer, "本分析僅依據既有資料整理，不構成任何投資建議。")

    def test_normalize_ai_analysis_repairs_trailing_commas(self) -> None:
        analysis = normalize_ai_analysis(
            """
            下面是 JSON：
            {
              "overall_status": "觀察",
              "summary": "估值偏高，仍需觀察後續基本面。",
              "positive_points": ["營收成長"],
              "risk_points": ["PE 偏高"],
              "watch_points": ["MA20"],
            }
            """
        )

        self.assertEqual(analysis_texts(analysis.risk_points), ["PE 偏高"])

    def test_normalize_ai_analysis_falls_back_to_plain_text_summary(self) -> None:
        analysis = normalize_ai_analysis("目前技術面偏強，但估值偏高，建議持續觀察月營收與 MA20。")

        self.assertEqual(analysis.overall_status, "觀察")
        self.assertIn("技術面偏強", analysis_text(analysis.summary))
        self.assertFalse(analysis.format_valid)
        self.assertEqual(analysis_texts(analysis.risk_points), [])
        self.assertEqual(analysis_texts(analysis.watch_points), ["可重新產生 AI 分析以取得結構化摘要。"])

    def test_normalize_ai_analysis_parses_loose_key_value_fragment(self) -> None:
        analysis = normalize_ai_analysis(
            '''
            overall_status": "觀察",
            "summary": "臻鼎-KY目前持股有顯著未實現獲利30.08%，但估值偏高。",
            "positive_points": ["持股已有未實現獲利"],
            "risk_points": ["目前PE偏高"],
            "watch_points": ["觀察 MA20 是否跌破"]
            '''
        )

        self.assertEqual(analysis.overall_status, "觀察")
        self.assertEqual(analysis_text(analysis.summary), "臻鼎-KY目前持股有顯著未實現獲利30.08%，但估值偏高。")
        self.assertEqual(analysis_texts(analysis.positive_points), ["持股已有未實現獲利"])
        self.assertEqual(analysis_texts(analysis.risk_points), ["目前PE偏高"])

    def test_normalize_ai_analysis_parses_truncated_summary_fragment(self) -> None:
        analysis = normalize_ai_analysis(
            'overall_status": "觀察", "summary": "臻鼎-KY目前持股有顯著未實現獲利30.08'
        )

        self.assertEqual(analysis.overall_status, "觀察")
        self.assertEqual(analysis_text(analysis.summary), "臻鼎-KY目前持股有顯著未實現獲利30.08")
        self.assertEqual(analysis_texts(analysis.risk_points), [])

    def test_normalize_ai_analysis_parses_chinese_sections_with_evidence_repair(self) -> None:
        analysis, errors = normalize_ai_analysis_with_errors(
            """
            狀態：等待
            摘要：
            目前現價、估值與技術面訊號仍未形成一致方向，應等待 MA20 與成交量改善後再評估進場。

            正面因素
            - 月營收 YoY 維持正值

            風險因素
            - 目前 PE 高於三年平均

            後續觀察
            - 觀察成交量是否回到 20 日均量之上
            """,
            AI_MODE_UNHELD,
            evidence_keys={
                "quote.current_price_twd",
                "valuation.current_pe_vs_average_percent",
                "fundamental.latest_revenue_yoy_percent",
                "technical.latest.price_vs_ma20_percent",
                "technical.latest.volume_difference_vs_ma20_percent",
            },
        )

        self.assertEqual(analysis.overall_status, "等待")
        self.assertTrue(analysis.format_valid)
        self.assertIn("現價", analysis_text(analysis.summary))
        self.assertEqual(analysis_texts(analysis.positive_points), ["月營收 YoY 維持正值"])
        self.assertTrue(analysis.summary.evidence_keys)
        self.assertTrue(any("evidence keys repaired" in error for error in errors))

    def test_message_content_text_reads_openrouter_content_parts(self) -> None:
        text = _message_content_text(
            {
                "content": [
                    {"type": "text", "text": '{"overall_status":"觀察",'},
                    {"type": "text", "text": '"summary":"測試"}'},
                ],
                "reasoning": "should not be used",
            }
        )

        self.assertEqual(text, '{"overall_status":"觀察","summary":"測試"}')

    def test_message_content_text_falls_back_to_reasoning_when_content_empty(self) -> None:
        text = _message_content_text({"content": "", "reasoning": '{"summary":"測試"}'})

        self.assertEqual(text, '{"summary":"測試"}')

    def test_format_fallback_analysis_is_not_cacheable(self) -> None:
        row = SimpleNamespace(
            status="success",
            response_json='{"overall_status":"觀察","summary":"測試","format_valid":false}',
        )

        self.assertFalse(_ai_analysis_is_cacheable(row))

    def test_legacy_format_fallback_analysis_is_not_cacheable(self) -> None:
        row = SimpleNamespace(
            status="success",
            response_json=(
                '{"overall_status":"觀察","summary":"AI 已回覆，但格式不是 JSON。",'
                '"risk_points":["AI 回覆格式不是 JSON，本次僅保留文字摘要。"]}'
            ),
        )

        self.assertFalse(_ai_analysis_is_cacheable(row))

    def test_valid_analysis_is_cacheable(self) -> None:
        row = SimpleNamespace(
            status="success",
            response_json='{"overall_status":"觀察","summary":"測試","format_valid":true}',
            provider="openrouter",
            model="openai/gpt-oss-120b:free",
            analysis_date=date(2026, 6, 17),
            updated_at=datetime.now(UTC),
        )

        self.assertTrue(_ai_analysis_is_cacheable(row))

    def test_fresh_running_analysis_is_inflight_but_stale_running_is_not(self) -> None:
        fresh = SimpleNamespace(status="running", updated_at=datetime.now(UTC))
        stale = SimpleNamespace(status="running", updated_at=datetime(2026, 1, 1, tzinfo=UTC))
        success = SimpleNamespace(status="success", updated_at=datetime.now(UTC))

        self.assertTrue(_ai_analysis_is_fresh_inflight(fresh))
        self.assertFalse(_ai_analysis_is_fresh_inflight(stale))
        self.assertFalse(_ai_analysis_is_fresh_inflight(success))

    def test_analysis_with_only_sanitization_warnings_is_cacheable(self) -> None:
        row = SimpleNamespace(
            status="success",
            response_json='{"overall_status":"等待","summary":"測試","format_valid":true}',
            validation_errors_json='["risk_points: warning: unsupported context referenced"]',
        )

        self.assertTrue(_ai_analysis_is_cacheable(row))

    def test_ai_failure_status_code_distinguishes_rate_limit_and_provider_outage(self) -> None:
        self.assertEqual(
            _ai_failure_http_status_code({"unheld": "OpenRouter API request failed with status 429."}),
            429,
        )
        self.assertEqual(
            _ai_failure_http_status_code({"unheld": "OpenRouter API request failed with status 502. Bad Gateway"}),
            503,
        )
        self.assertEqual(
            _ai_failure_http_status_code({"unheld": "OpenRouter did not return any choices."}),
            502,
        )

    @patch("backend.app.services.application._ai_stock_summary", return_value={"symbol": "4958", "price": 590})
    def test_ai_generation_failure_returns_latest_success_cache(self, _summary) -> None:
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with Session() as session:
            stock = Stock(symbol="4958", name="臻鼎-KY", asset_type="STOCK")
            session.add(stock)
            session.commit()
            session.refresh(stock)
            cached_row = StockAIAnalysis(
                stock_id=stock.id,
                provider="openrouter",
                model="openai/gpt-oss-120b:free",
                analysis_mode=AI_MODE_UNHELD,
                prompt_version=PROMPT_VERSION,
                analysis_date=date(2026, 6, 24),
                input_hash="old-input",
                request_payload_json="{}",
                response_json=(
                    '{"overall_status":"等待",'
                    '"summary":"目前估值、基本面、技術面與籌碼訊號仍然分歧，尚未形成一致方向，適合等待既有資料確認。",'
                    '"positive_points":[],"risk_points":[],"watch_points":[],"disclaimer":"僅供資料整理。","format_valid":true}'
                ),
                validation_errors_json="[]",
                status="success",
                updated_at=datetime(2026, 6, 24, tzinfo=UTC),
            )
            session.add(cached_row)
            session.commit()
            session.refresh(cached_row)

            row, cached, error, is_running = _generate_ai_mode(
                session,
                stock,
                FailingProvider(),
                AI_MODE_UNHELD,
                force_refresh=True,
            )

            failed_count = session.query(StockAIAnalysis).filter(StockAIAnalysis.status == "failed").count()

        self.assertIsNotNone(row)
        self.assertEqual(row.id, cached_row.id)
        self.assertTrue(cached)
        self.assertFalse(is_running)
        self.assertIn("已顯示最近成功快取", error)
        self.assertEqual(failed_count, 1)

    @patch("backend.app.services.application._ai_stock_summary")
    @patch("backend.app.services.application.settings")
    def test_openrouter_fallback_model_runs_after_primary_rate_limit(self, settings, stock_summary) -> None:
        settings.openrouter_fallback_models = ["fallback-model"]
        settings.openrouter_model_cooldown_seconds = 600
        stock_summary.return_value = {
            "symbol": "4958",
            "evidence": {"quote.current_price_twd": 590},
        }
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with Session() as session:
            stock = Stock(symbol="4958", name="臻鼎-KY", asset_type="STOCK")
            session.add(stock)
            session.commit()
            session.refresh(stock)

            row, cached, error, is_running = _generate_ai_mode_with_fallback(
                session,
                stock,
                FallbackTestOpenRouterProvider(model="primary-model"),
                AI_MODE_UNHELD,
                force_refresh=True,
            )
            failed = session.query(StockAIAnalysis).filter(StockAIAnalysis.status == "failed").one()

        self.assertIsNotNone(row)
        self.assertEqual(row.model, "fallback-model")
        self.assertFalse(cached)
        self.assertFalse(is_running)
        self.assertIsNone(error)
        self.assertEqual(failed.model, "primary-model")
        self.assertIn("provider_rate_limited", failed.quality_flags_json)

    @patch("backend.app.services.application._ai_stock_summary")
    @patch("backend.app.services.application.settings")
    def test_openrouter_fallback_skips_model_in_cooldown(self, settings, stock_summary) -> None:
        settings.openrouter_fallback_models = ["fallback-model"]
        settings.openrouter_model_cooldown_seconds = 600
        stock_summary.return_value = {
            "symbol": "4958",
            "evidence": {"quote.current_price_twd": 590},
        }
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with Session() as session:
            stock = Stock(symbol="4958", name="臻鼎-KY", asset_type="STOCK")
            session.add(stock)
            session.commit()
            session.refresh(stock)
            session.add(
                StockAIAnalysis(
                    stock_id=stock.id,
                    provider="openrouter",
                    model="primary-model",
                    analysis_mode=AI_MODE_UNHELD,
                    prompt_version=PROMPT_VERSION,
                    analysis_date=date(2026, 6, 29),
                    input_hash="old",
                    request_payload_json="{}",
                    response_json="{}",
                    validation_errors_json="[]",
                    quality_flags_json='["provider_rate_limited"]',
                    grounding_errors_json="[]",
                    status="failed",
                    error_message="OpenRouter API request failed with status 429.",
                    updated_at=datetime.now(UTC),
                )
            )
            session.commit()

            row, cached, error, is_running = _generate_ai_mode_with_fallback(
                session,
                stock,
                FallbackTestOpenRouterProvider(model="primary-model"),
                AI_MODE_UNHELD,
                force_refresh=True,
            )
            failed_count = session.query(StockAIAnalysis).filter(StockAIAnalysis.status == "failed").count()

        self.assertIsNotNone(row)
        self.assertEqual(row.model, "fallback-model")
        self.assertFalse(cached)
        self.assertFalse(is_running)
        self.assertIsNone(error)
        self.assertEqual(failed_count, 1)

    def test_unheld_mode_rejects_held_status(self) -> None:
        analysis, errors = normalize_ai_analysis_with_errors(
            {
                "overall_status": "續抱",
                "summary": "目前估值與技術訊號互有強弱，應等待價格與基本面形成一致訊號後再評估進場。",
                "positive_points": [],
                "risk_points": [],
                "watch_points": [],
                "disclaimer": "本分析僅依據既有資料整理，不構成任何投資建議。",
            },
            AI_MODE_UNHELD,
        )

        self.assertEqual(analysis.overall_status, "資料不足")
        self.assertFalse(analysis.format_valid)
        self.assertTrue(any("Unsupported status" in error for error in errors))

    def test_grounded_analysis_requires_valid_evidence_keys(self) -> None:
        analysis, errors = normalize_ai_analysis_with_errors(
            {
                "overall_status": "等待",
                "summary": {
                    "text": "目前現價與估值訊號仍然沒有形成足夠一致方向，應等待後續已提供數據確認後再評估進場。",
                    "evidence_keys": ["quote.current_price_twd"],
                },
                "positive_points": [
                    {"text": "現價資料已可用", "evidence_keys": ["quote.current_price_twd"]}
                ],
                "risk_points": [
                    {"text": "引用不存在的證據", "evidence_keys": ["not.real"]}
                ],
                "watch_points": [],
                "disclaimer": "僅供資料整理。",
            },
            AI_MODE_UNHELD,
            evidence_keys={"quote.current_price_twd"},
        )

        self.assertEqual(analysis.summary.evidence_keys, ["quote.current_price_twd"])
        self.assertFalse(analysis.format_valid)
        self.assertTrue(any("invalid evidence key" in error for error in errors))

    def test_private_position_request_is_filtered(self) -> None:
        analysis, errors = normalize_ai_analysis_with_errors(
            {
                "overall_status": "觀察",
                "summary": "技術與估值訊號仍然分歧，應持續檢查已提供的基本面與成交量資料。",
                "positive_points": [],
                "risk_points": ["需要補充持有股數才能評估部位風險", "總資產配置可作為未來風險管理參考"],
                "watch_points": [],
                "disclaimer": "本分析僅依據既有資料整理，不構成任何投資建議。",
            },
            AI_MODE_HELD,
        )

        self.assertEqual(analysis_texts(analysis.risk_points), ["總資產配置可作為未來風險管理參考"])
        self.assertFalse(analysis.format_valid)
        self.assertTrue(any("private position" in error for error in errors))

    def test_missing_evidence_keys_are_repaired_when_text_is_usable(self) -> None:
        analysis, errors = normalize_ai_analysis_with_errors(
            {
                "overall_status": "等待",
                "summary": {
                    "text": "目前現價、估值、基本面與技術面訊號仍未形成一致方向，應等待成交量與 MA20 訊號改善後再評估進場。",
                    "evidence_keys": [],
                },
                "positive_points": ["現價資料與 MA20 資料已可用"],
                "risk_points": ["目前 PE 高於三年平均，估值位置需要留意"],
                "watch_points": ["觀察成交量是否重新高於 20 日均量"],
                "disclaimer": "僅供資料整理。",
            },
            AI_MODE_UNHELD,
            evidence_keys={
                "quote.current_price_twd",
                "valuation.current_pe_vs_average_percent",
                "technical.latest.price_vs_ma20_percent",
                "technical.latest.volume_difference_vs_ma20_percent",
            },
        )

        self.assertTrue(analysis.format_valid)
        self.assertTrue(analysis.summary.evidence_keys)
        self.assertTrue(analysis.positive_points[0].evidence_keys)
        self.assertTrue(any("evidence keys repaired" in error for error in errors))

    def test_openrouter_common_aliases_are_repaired(self) -> None:
        analysis, errors = normalize_ai_analysis_with_errors(
            {
                "overall_status": "等待",
                "summary": "目前基本面、估值、技術面與籌碼訊號仍然分歧，尚未形成一致方向，適合等待既有資料進一步確認。",
                "positives": ["營收年增率維持正值"],
                "risks": ["目前 PE 高於三年平均"],
                "next_steps": ["觀察價格與 MA20 的相對位置"],
                "disclaimer": "僅供資料整理。",
            },
            AI_MODE_UNHELD,
        )

        self.assertEqual(analysis_texts(analysis.positive_points), ["營收年增率維持正值"])
        self.assertEqual(analysis_texts(analysis.risk_points), ["目前 PE 高於三年平均"])
        self.assertEqual(analysis_texts(analysis.watch_points), ["觀察價格與 MA20 的相對位置"])
        self.assertEqual(errors, [])
        self.assertTrue(analysis.format_valid)

    def test_unheld_mode_rejects_held_position_language(self) -> None:
        analysis, errors = normalize_ai_analysis_with_errors(
            {
                "overall_status": "等待",
                "summary": "目前基本面與技術面訊號尚未一致，應等待現有公開數據出現更明確方向後再評估進場。",
                "positive_points": [],
                "risk_points": [],
                "watch_points": ["回檔至 MA20 時可以考慮加碼"],
                "disclaimer": "僅供資料整理。",
            },
            AI_MODE_UNHELD,
        )

        self.assertEqual(analysis_texts(analysis.watch_points), [])
        self.assertTrue(any("held-position language" in error for error in errors))
        self.assertFalse(analysis.format_valid)

    def test_broader_market_context_is_allowed_when_not_share_count(self) -> None:
        analysis, errors = normalize_ai_analysis_with_errors(
            {
                "overall_status": "等待",
                "summary": "目前估值、基本面、技術面與籌碼訊號仍然分歧，尚未形成一致方向，適合等待既有資料確認。",
                "positive_points": ["營收年增率維持正值"],
                "risk_points": ["全球需求放緩可能影響後續表現", "目前 PE 高於三年平均"],
                "watch_points": [],
                "disclaimer": "僅供資料整理。",
            },
            AI_MODE_UNHELD,
        )

        self.assertEqual(analysis_texts(analysis.risk_points), ["全球需求放緩可能影響後續表現", "目前 PE 高於三年平均"])
        self.assertEqual(errors, [])
        self.assertTrue(analysis.format_valid)

    @patch("backend.app.ai_analysis.requests.post")
    def test_openrouter_uses_strict_json_schema(self, post) -> None:
        post.return_value = SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {
                "id": "test-response",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": (
                                '{"overall_status":"等待","summary":"目前估值、基本面、技術面與籌碼訊號互有強弱，尚未形成一致方向，應等待更多已提供數據確認後再評估是否進場。",'
                                '"positive_points":[],"risk_points":[],"watch_points":[],"disclaimer":"僅供資料整理。"}'
                            )
                        },
                    }
                ],
            },
        )
        provider = OpenRouterProvider(api_key="test", model="openai/gpt-oss-120b:free")

        result = provider.analyze_stock({"symbol": "4958"}, AI_MODE_UNHELD)

        request_payload = post.call_args.kwargs["json"]
        self.assertTrue(request_payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(
            request_payload["response_format"]["json_schema"]["schema"]["properties"]["overall_status"]["enum"],
            ["分批布局", "等待", "避開", "資料不足"],
        )
        self.assertTrue(result.analysis.format_valid)

    @patch("backend.app.ai_analysis.requests.post")
    def test_openrouter_relaxes_routing_when_strict_endpoint_is_unavailable(self, post) -> None:
        post.side_effect = [
            SimpleNamespace(
                status_code=404,
                json=lambda: {
                    "error": {
                        "message": "No endpoints found that can handle the requested parameters"
                    }
                },
            ),
            SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {
                    "id": "fallback-response",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": (
                                    '{"overall_status":"等待","summary":"目前基本面、估值、技術面與籌碼訊號仍然分歧，尚未形成一致方向，適合等待更多既有指標確認後再評估進場。",'
                                    '"positive_points":[],"risk_points":[],"watch_points":[],"disclaimer":"僅供資料整理。"}'
                                )
                            },
                        }
                    ],
                },
            ),
        ]
        provider = OpenRouterProvider(api_key="test", model="openai/gpt-oss-120b:free")

        result = provider.analyze_stock({"symbol": "2330"}, AI_MODE_UNHELD)

        self.assertEqual(post.call_count, 2)
        fallback_payload = post.call_args_list[1].kwargs["json"]
        self.assertFalse(fallback_payload["provider"]["require_parameters"])
        self.assertEqual(fallback_payload["response_format"], {"type": "json_object"})
        self.assertEqual(result.provider_metadata["structured_output_mode"], "json_object_fallback")
        self.assertTrue(result.analysis.format_valid)

    @patch("backend.app.services.application._technical_summary_for_ai", return_value={"latest": None})
    @patch("backend.app.services.application._stock_response")
    def test_unheld_payload_excludes_position_data(self, stock_response, _technical_summary) -> None:
        metric = SimpleNamespace(
            current_price=590,
            open_price=580,
            previous_close=575,
            day_high=600,
            day_low=570,
            price_updated_at=datetime.now(UTC),
            source="TWSE",
            current_pe=70,
            pe_average_3y=40,
            pe_min_3y=20,
            pe_max_3y=80,
            pe_vs_average_percent=75,
            pe_updated_at=datetime.now(UTC),
        )
        stock_response.return_value = SimpleNamespace(
            symbol="4958",
            name="臻鼎-KY",
            asset_type="STOCK",
            market="TWSE",
            currency="TWD",
            metric=metric,
            valuations=[],
            fundamental=None,
            broker_trading=None,
            position=SimpleNamespace(
                buy_price=490,
                unrealized_profit_loss=100,
                unrealized_profit_loss_percent=20.4,
                fee_adjusted_profit_loss=99,
                fee_adjusted_profit_loss_percent=20.2,
                broker_id="CATHAY",
                broker_fee_rate=0.000399,
            ),
        )

        unheld = _ai_stock_summary(SimpleNamespace(symbol="4958"), SimpleNamespace(), AI_MODE_UNHELD)
        held = _ai_stock_summary(SimpleNamespace(symbol="4958"), SimpleNamespace(), AI_MODE_HELD)

        self.assertNotIn("position", unheld)
        self.assertNotIn("constraints", unheld)
        self.assertEqual(unheld["quote"]["current_vs_open_percent"], 1.72)
        self.assertEqual(unheld["pe_context"]["current_pe_position_in_3y_range_percent"], 83.33)
        self.assertEqual(held["position"]["average_cost_price_twd"], 490)
        self.assertNotIn("share_count", held["position"])

    def test_compact_payload_limits_model_input_surface(self) -> None:
        full_summary = {
            "symbol": "4958",
            "name": "臻鼎-KY",
            "asset_type": "STOCK",
            "market": "TWSE",
            "currency": "TWD",
            "summary_version": 2,
            "prompt_version": PROMPT_VERSION,
            "analysis_mode": AI_MODE_UNHELD,
            "quote": {
                "current_price_twd": 590,
                "open_price_twd": 580,
                "previous_close_twd": 575,
                "day_high_twd": 600,
                "day_low_twd": 570,
                "source": "verbose source that should be removed",
            },
            "pe_context": {
                "current_pe": 80.82,
                "pe_average_3y": 40,
                "pe_min_3y": 10,
                "pe_max_3y": 90,
                "current_pe_vs_average_percent": 102.05,
                "current_pe_position_in_3y_range_percent": 88.52,
            },
            "valuation_scenarios": [
                {
                    "eps_type": "TTM",
                    "eps_value": 7.58,
                    "eps_period": "2026Q1 + 2025Q4 + 2025Q3 + 2025Q2",
                    "mechanical_eps_times_current_pe_price_twd": 612.62,
                    "mechanical_price_vs_current_price_percent": 3.83,
                    "calculated_at": "2026-07-03T00:00:00Z",
                },
                {
                    "eps_type": "LAST_YEAR",
                    "eps_value": 6.91,
                    "mechanical_eps_times_current_pe_price_twd": 558.51,
                    "mechanical_price_vs_current_price_percent": -5.34,
                },
                {
                    "eps_type": "EXTRA",
                    "eps_value": 1,
                    "mechanical_eps_times_current_pe_price_twd": 1,
                },
            ],
            "fundamental": {"latest_quarter_eps": 1.33, "source": "removed"},
            "technical": {
                "latest": {
                    "date": "2026-07-03",
                    "close_price_twd": 590,
                    "ma5": 580,
                    "ma10": 570,
                    "price_vs_ma20_percent": 4.2,
                    "volume_difference_vs_ma20_percent": -30,
                }
            },
            "chip": {
                "main_net_volume_lots": 1200,
                "top_buy_brokers": [
                    {"rank": index + 1, "broker_name": f"B{index}", "buy_volume_lots": 100, "sell_volume_lots": 10, "net_volume_lots": 90}
                    for index in range(5)
                ],
                "top_sell_brokers": [],
                "source": "removed",
            },
            "position": {"average_cost_price_twd": 490, "share_count": 1000},
        }

        compact = _compact_ai_stock_summary(full_summary, AI_MODE_UNHELD)

        self.assertNotIn("position", compact)
        self.assertEqual(len(compact["valuation_scenarios"]), 2)
        self.assertNotIn("source", compact["quote"])
        self.assertNotIn("ma5", compact["technical"]["latest"])
        self.assertEqual(len(compact["chip"]["top_buy_brokers"]), 3)
        self.assertIn("quote.current_price_twd", compact["available_evidence_keys"])
        self.assertNotIn("position.share_count", compact["available_evidence_keys"])

    def test_feedback_endpoint_updates_summary_counts(self) -> None:
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with Session() as session:
            stock = Stock(symbol="4958", name="臻鼎-KY", asset_type="STOCK")
            session.add(stock)
            session.commit()
            session.refresh(stock)
            row = StockAIAnalysis(
                stock_id=stock.id,
                provider="openrouter",
                model="openai/gpt-oss-120b:free",
                analysis_mode=AI_MODE_UNHELD,
                prompt_version=PROMPT_VERSION,
                analysis_date=date(2026, 6, 29),
                input_hash="input",
                request_payload_json='{"evidence":{"quote.current_price_twd":590}}',
                response_json=(
                    '{"overall_status":"等待",'
                    '"summary":{"text":"目前估值、基本面、技術面與籌碼訊號仍然分歧，尚未形成一致方向，適合等待既有資料確認。","evidence_keys":["quote.current_price_twd"]},'
                    '"positive_points":[],"risk_points":[],"watch_points":[],"disclaimer":"僅供資料整理。","format_valid":true}'
                ),
                validation_errors_json="[]",
                quality_flags_json="[]",
                grounding_errors_json="[]",
                status="success",
                updated_at=datetime(2026, 6, 29, tzinfo=UTC),
            )
            session.add(row)
            session.commit()
            session.refresh(row)

            feedback = submit_stock_ai_analysis_feedback(
                "4958",
                AI_MODE_UNHELD,
                StockAIAnalysisFeedbackRequest(
                    analysis_id=row.id,
                    rating="not_useful",
                    tags=["hallucination"],
                ),
                session=session,
            )
            summary = ai_analysis_logs_summary(
                symbol=None,
                mode=None,
                provider=None,
                date_from=None,
                date_to=None,
                session=session,
            )

        self.assertEqual(feedback.status, "ok")
        self.assertEqual(summary["feedback"]["by_rating"]["not_useful"], 1)
        self.assertEqual(summary["feedback"]["by_tag"]["hallucination"], 1)

    def test_ai_analysis_endpoint_runs_unheld_and_held_together_when_position_exists(self) -> None:
        for force_refresh in (False, True):
            with self.subTest(force_refresh=force_refresh):
                engine = create_engine("sqlite:///:memory:", future=True)
                Base.metadata.create_all(engine)
                Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
                calls = []

                def fake_generate(session, stock, provider, mode, force_refresh):
                    calls.append((mode, force_refresh))
                    return SimpleNamespace(analysis_mode=mode), False, None, False

                with Session() as session:
                    stock = Stock(symbol="4958", name="臻鼎-KY", asset_type="STOCK", is_active=True)
                    session.add(stock)
                    session.commit()
                    session.refresh(stock)
                    session.add(StockPosition(stock_id=stock.id, buy_price=Decimal("491.25")))
                    session.commit()

                    with (
                        patch("backend.app.api.ai.build_ai_provider", return_value=SimpleNamespace(provider_id="openrouter")),
                        patch("backend.app.api.ai._generate_ai_mode_with_fallback", side_effect=fake_generate),
                        patch(
                            "backend.app.api.ai._ai_analysis_batch_response",
                            side_effect=lambda symbol, results, errors=None, running=None, rule_based=None: {
                                "symbol": symbol,
                                "modes": sorted(results.keys()),
                                "errors": errors or {},
                                "running": running or {},
                                "rule_based": sorted((rule_based or {}).keys()),
                            },
                        ),
                    ):
                        result = create_stock_ai_analysis(
                            "4958",
                            Response(),
                            StockAIAnalysisRequest(force_refresh=force_refresh),
                            session=session,
                        )

                self.assertEqual(result["modes"], [AI_MODE_HELD, AI_MODE_UNHELD])
                self.assertEqual(result["rule_based"], [AI_MODE_HELD, AI_MODE_UNHELD])
                self.assertEqual(calls, [(AI_MODE_UNHELD, force_refresh), (AI_MODE_HELD, force_refresh)])

    def test_ai_analysis_endpoint_returns_rule_summary_when_provider_is_not_configured(self) -> None:
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

        with Session() as session:
            stock = Stock(symbol="8064", name="東捷", asset_type="STOCK", is_active=True)
            session.add(stock)
            session.commit()

            with patch(
                "backend.app.api.ai.build_ai_provider",
                side_effect=AIConfigurationError("OPENROUTER_MODEL is not configured."),
            ):
                result = create_stock_ai_analysis(
                    "8064",
                    Response(),
                    StockAIAnalysisRequest(force_refresh=True),
                    session=session,
                )

        engine.dispose()

        self.assertIsNone(result.analyses.unheld)
        self.assertIsNotNone(result.rule_based.unheld)
        self.assertIn("not configured", result.errors["unheld"])

    def test_enqueued_ai_job_persists_success_result(self) -> None:
        import backend.app.services.application as main

        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        old_session_local = main.SessionLocal

        class PassingProvider:
            provider_id = "gemini"
            model = "test-model"

            def analyze_stock(self, stock_summary, analysis_mode):
                return AIProviderResult(
                    analysis=StockAIAnalysisContent(
                        overall_status="等待",
                        summary=StockAIAnalysisEvidenceText(text="目前資料仍需確認，先等待更明確訊號。", evidence_keys=["symbol"]),
                        positive_points=[],
                        risk_points=[],
                        watch_points=[],
                        disclaimer="僅供資料整理。",
                    ),
                    raw_response_text="{}",
                    provider_metadata={"test": True},
                    validation_errors=[],
                    quality_flags=[],
                    grounding_errors=[],
                )

        try:
            main.SessionLocal = lambda: Session()
            with Session() as session:
                stock = Stock(symbol="8064", name="東捷", asset_type="STOCK", is_active=True)
                session.add(stock)
                session.commit()
                session.refresh(stock)

                row, cached, error, running, queued_row_id = _enqueue_ai_mode_with_fallback(
                    session,
                    stock,
                    PassingProvider(),
                    AI_MODE_UNHELD,
                    force_refresh=True,
                )

            self.assertIsNone(row)
            self.assertFalse(cached)
            self.assertIsNone(error)
            self.assertTrue(running)
            self.assertIsNotNone(queued_row_id)

            with patch("backend.app.services.application._build_ai_provider_for_row", return_value=PassingProvider()):
                _run_ai_analysis_job(queued_row_id)

            with Session() as session:
                stored = session.get(StockAIAnalysis, queued_row_id)
                self.assertEqual(stored.status, "success")
                self.assertEqual(stored.analysis_mode, AI_MODE_UNHELD)
                request_payload = json.loads(stored.request_payload_json)
                requested_at = request_payload["analysis_context"]["analysis_requested_at"]
                self.assertEqual(request_payload["analysis_context"]["timezone"], "Asia/Taipei")
                self.assertEqual(_ai_analysis_result_response(stored, cached=False).analysis_requested_at.isoformat(), requested_at)
        finally:
            main.SessionLocal = old_session_local
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
