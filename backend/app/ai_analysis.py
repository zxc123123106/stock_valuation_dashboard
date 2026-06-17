from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from .schemas import StockAIAnalysisContent


GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_DISCLAIMER = "本分析僅依據既有資料整理，不構成任何投資建議。"


class AIAnalysisError(RuntimeError):
    pass


class AIConfigurationError(AIAnalysisError):
    pass


class AIProvider(Protocol):
    provider_id: str
    model: str

    def analyze_stock(self, stock_summary: dict[str, Any]) -> StockAIAnalysisContent:
        ...


@dataclass(frozen=True)
class GeminiProvider:
    api_key: str
    model: str
    timeout_seconds: int = 45
    provider_id: str = "gemini"

    def analyze_stock(self, stock_summary: dict[str, Any]) -> StockAIAnalysisContent:
        prompts = _analysis_prompts(stock_summary)
        payload = {
            "system_instruction": {"parts": [{"text": prompts.system}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompts.user}],
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 1200,
                "responseMimeType": "application/json",
            },
        }
        response = requests.post(
            GEMINI_GENERATE_URL.format(model=self.model),
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout_seconds,
        )
        _raise_for_provider_error(response, "Gemini")
        data = response.json()
        text = _gemini_text(data)
        return normalize_ai_analysis(text)


@dataclass(frozen=True)
class OpenRouterProvider:
    api_key: str
    model: str
    timeout_seconds: int = 45
    provider_id: str = "openrouter"

    def analyze_stock(self, stock_summary: dict[str, Any]) -> StockAIAnalysisContent:
        prompts = _analysis_prompts(stock_summary)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompts.system},
                {"role": "user", "content": prompts.user},
            ],
            "temperature": 0.2,
            "max_tokens": 1800,
            "reasoning": {"exclude": True},
            "response_format": {"type": "json_object"},
        }
        response = requests.post(
            OPENROUTER_CHAT_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout_seconds,
        )
        _raise_for_provider_error(response, "OpenRouter")
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise AIAnalysisError("OpenRouter did not return any choices.")
        message = choices[0].get("message") or {}
        return normalize_ai_analysis(_message_content_text(message))


@dataclass(frozen=True)
class AnalysisPrompts:
    system: str
    user: str


def build_ai_provider(settings, provider_id: str | None = None) -> AIProvider:
    normalized = (provider_id or settings.ai_provider or "gemini").strip().lower()
    if normalized == "gemini":
        if not settings.gemini_api_key:
            raise AIConfigurationError("GEMINI_API_KEY is not configured.")
        return GeminiProvider(api_key=settings.gemini_api_key, model=settings.gemini_model)
    if normalized == "openrouter":
        if not settings.openrouter_api_key:
            raise AIConfigurationError("OPENROUTER_API_KEY is not configured.")
        if not settings.openrouter_model:
            raise AIConfigurationError("OPENROUTER_MODEL is not configured.")
        return OpenRouterProvider(api_key=settings.openrouter_api_key, model=settings.openrouter_model)
    raise AIConfigurationError(f"Unsupported AI provider: {provider_id}")


def stock_summary_hash(stock_summary: dict[str, Any]) -> str:
    payload = json.dumps(stock_summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_ai_analysis(value: Any) -> StockAIAnalysisContent:
    payload = value
    if isinstance(value, str):
        payload = _parse_json_object(value)
        if payload is None:
            return _plain_text_analysis(value)
    if not isinstance(payload, dict):
        raise AIAnalysisError("AI response is not a JSON object.")
    if isinstance(payload.get("analysis"), dict):
        payload = payload["analysis"]

    return StockAIAnalysisContent(
        overall_status=_clean_text(payload.get("overall_status"), "觀察", 24),
        summary=_clean_text(payload.get("summary"), "AI 暫時沒有產生摘要。", 360),
        positive_points=_clean_list(payload.get("positive_points"), 5, 80),
        risk_points=_clean_list(payload.get("risk_points"), 5, 80),
        watch_points=_clean_list(payload.get("watch_points"), 5, 80),
        disclaimer=_clean_text(payload.get("disclaimer"), DEFAULT_DISCLAIMER, 120),
        format_valid=True,
    )


def _analysis_prompts(stock_summary: dict[str, Any]) -> AnalysisPrompts:
    schema = {
        "overall_status": "續抱 / 觀察 / 分批調節 / 重新評估 其中之一，若資料不足請用觀察",
        "summary": "80 到 140 字中文摘要",
        "positive_points": ["最多 5 點，限已知資料"],
        "risk_points": ["最多 5 點，限已知資料"],
        "watch_points": ["最多 5 點，後續觀察資料欄位"],
        "disclaimer": DEFAULT_DISCLAIMER,
    }
    system = (
        "你是台股持股資料解讀器。只能根據使用者提供的 JSON 摘要做資料整理與風險說明。"
        "不得編造缺漏資料，不得承諾報酬，不得給出買進或賣出的直接指令。"
        "你不負責計算數字；所有數字以 JSON 為準。"
        "只輸出一個 JSON object。第一個字元必須是 {，最後一個字元必須是 }。"
        "不要 Markdown，不要 code fence，不要推理過程，不要額外說明。"
    )
    user = (
        "請依照指定 schema 回傳 AI 分析。若某些資料為 null 或待更新，請在風險或觀察點中保守表達。\n\n"
        f"response_schema:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"stock_summary:\n{json.dumps(stock_summary, ensure_ascii=False, indent=2)}"
    )
    return AnalysisPrompts(system=system, user=user)


def _raise_for_provider_error(response: requests.Response, provider_name: str) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        message = f"{provider_name} API request failed with status {response.status_code}."
        try:
            body = response.json()
            provider_error = body.get("error") if isinstance(body, dict) else None
            if isinstance(provider_error, dict):
                message = f"{message} {provider_error.get('message') or provider_error.get('code') or ''}".strip()
            elif isinstance(provider_error, str):
                message = f"{message} {provider_error}"
        except ValueError:
            pass
        raise AIAnalysisError(message) from exc


def _gemini_text(data: dict[str, Any]) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        raise AIAnalysisError("Gemini did not return any candidates.")
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    text = "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
    if not text.strip():
        raise AIAnalysisError("Gemini returned an empty response.")
    return text


def _message_content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    text = _content_to_text(content)
    if text.strip():
        return text
    return _content_to_text(message.get("reasoning") or message.get("reasoning_content"))


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for item in content:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                pieces.append(str(item.get("text") or item.get("content") or ""))
        return "".join(pieces)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content)


def _extract_json(text: str) -> str:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return stripped


def _parse_json_object(text: str) -> dict[str, Any] | None:
    candidates = [
        text.strip(),
        _extract_json(text),
        _repair_json_text(_extract_json(text)),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return _parse_loose_analysis(text)


def _repair_json_text(text: str) -> str:
    repaired = text.strip()
    repaired = repaired.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    return repaired


def _parse_loose_analysis(text: str) -> dict[str, Any] | None:
    normalized = _repair_json_text(text)
    if not any(key in normalized for key in ("overall_status", "summary", "positive_points", "risk_points", "watch_points")):
        return None

    parsed = {
        "overall_status": _extract_loose_string(normalized, "overall_status"),
        "summary": _extract_loose_string(normalized, "summary"),
        "positive_points": _extract_loose_list(normalized, "positive_points"),
        "risk_points": _extract_loose_list(normalized, "risk_points"),
        "watch_points": _extract_loose_list(normalized, "watch_points"),
        "disclaimer": _extract_loose_string(normalized, "disclaimer"),
    }
    if not any(parsed.get(key) for key in ("overall_status", "summary", "positive_points", "risk_points", "watch_points")):
        return None
    return parsed


def _extract_loose_string(text: str, key: str) -> str | None:
    match = re.search(
        rf'["\']?{re.escape(key)}["\']?\s*:\s*["\']?(.*?)(?=(?:["\']?(?:overall_status|summary|positive_points|risk_points|watch_points|disclaimer)["\']?\s*:)|$)',
        text,
        flags=re.DOTALL,
    )
    if not match:
        return None
    value = match.group(1)
    value = re.sub(r"^\s*[,{\[]+", "", value)
    value = re.sub(r"[,}\]]+\s*$", "", value)
    return value.strip().strip('"').strip("'").strip() or None


def _extract_loose_list(text: str, key: str) -> list[str]:
    array_match = re.search(rf'["\']?{re.escape(key)}["\']?\s*:\s*(\[.*?\])', text, flags=re.DOTALL)
    if array_match:
        try:
            parsed = json.loads(_repair_json_text(array_match.group(1)))
            return _clean_list(parsed, 5, 80)
        except json.JSONDecodeError:
            pass
    string_value = _extract_loose_string(text, key)
    return _split_loose_items(string_value)


def _split_loose_items(value: str | None) -> list[str]:
    if not value:
        return []
    cleaned = value.replace("\\n", "\n")
    parts = re.split(r"\n+|[；;]", cleaned)
    return _clean_list([part.lstrip("-•0123456789.、) ").strip() for part in parts], 5, 80)


def _plain_text_analysis(text: str) -> StockAIAnalysisContent:
    summary = re.sub(r"\s+", " ", text).strip()
    return StockAIAnalysisContent(
        overall_status="觀察",
        summary=_clean_text(summary, "AI 已回覆，但格式不是 JSON。", 360),
        positive_points=[],
        risk_points=[],
        watch_points=["可重新產生 AI 分析以取得結構化摘要。"],
        disclaimer=DEFAULT_DISCLAIMER,
        format_valid=False,
    )


def _clean_text(value: Any, fallback: str, limit: int) -> str:
    text = str(value or fallback).strip()
    return text[:limit] if len(text) > limit else text


def _clean_list(value: Any, limit: int, item_limit: int) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = []

    cleaned = []
    for item in items:
        text = str(item or "").strip()
        if text:
            cleaned.append(text[:item_limit])
        if len(cleaned) >= limit:
            break
    return cleaned
