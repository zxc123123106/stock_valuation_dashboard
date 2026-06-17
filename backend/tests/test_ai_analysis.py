from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace

from backend.app.ai_analysis import _message_content_text, normalize_ai_analysis, stock_summary_hash
from backend.app.main import _ai_analysis_is_cacheable


class AIAnalysisTest(unittest.TestCase):
    def test_stock_summary_hash_is_stable(self) -> None:
        left = {"symbol": "4958", "metric": {"current_pe": 77.86, "current_price": 592}}
        right = {"metric": {"current_price": 592, "current_pe": 77.86}, "symbol": "4958"}

        self.assertEqual(stock_summary_hash(left), stock_summary_hash(right))

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
        self.assertEqual(analysis.positive_points, ["股價仍高於 MA20"])
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

        self.assertEqual(analysis.risk_points, ["PE 偏高"])

    def test_normalize_ai_analysis_falls_back_to_plain_text_summary(self) -> None:
        analysis = normalize_ai_analysis("目前技術面偏強，但估值偏高，建議持續觀察月營收與 MA20。")

        self.assertEqual(analysis.overall_status, "觀察")
        self.assertIn("技術面偏強", analysis.summary)
        self.assertFalse(analysis.format_valid)
        self.assertEqual(analysis.risk_points, [])
        self.assertEqual(analysis.watch_points, ["可重新產生 AI 分析以取得結構化摘要。"])

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
        self.assertEqual(analysis.summary, "臻鼎-KY目前持股有顯著未實現獲利30.08%，但估值偏高。")
        self.assertEqual(analysis.positive_points, ["持股已有未實現獲利"])
        self.assertEqual(analysis.risk_points, ["目前PE偏高"])

    def test_normalize_ai_analysis_parses_truncated_summary_fragment(self) -> None:
        analysis = normalize_ai_analysis(
            'overall_status": "觀察", "summary": "臻鼎-KY目前持股有顯著未實現獲利30.08'
        )

        self.assertEqual(analysis.overall_status, "觀察")
        self.assertEqual(analysis.summary, "臻鼎-KY目前持股有顯著未實現獲利30.08")
        self.assertEqual(analysis.risk_points, [])

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


if __name__ == "__main__":
    unittest.main()
