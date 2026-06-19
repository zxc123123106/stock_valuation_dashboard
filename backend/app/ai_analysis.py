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
PROMPT_VERSION = "v2-dual-mode"
AI_MODE_GENERAL = "GENERAL"
AI_MODE_UNHELD = "UNHELD"
AI_MODE_HELD = "HELD"
ALLOWED_STATUSES = {
    AI_MODE_GENERAL: ("續抱", "觀察", "分批調節", "重新評估"),
    AI_MODE_UNHELD: ("分批布局", "等待", "避開", "資料不足"),
    AI_MODE_HELD: ("續抱", "觀察", "分批調節", "重新評估"),
}

_MISSING_DATA_WORDS = ("未提供", "缺少", "待補充", "需補充", "需要補充", "未納入", "無法完整評估")
_PRIVATE_POSITION_WORDS = (
    "持股股數",
    "持有股數",
    "持股比例",
    "總成本",
    "持倉市值",
    "資產規模",
    "總資產",
    "帳戶資訊",
    "券商帳戶",
)
_UNSUPPORTED_CONTEXT_WORDS = (
    "外資",
    "投信",
    "三大法人",
    "未平倉",
    "支撐位",
    "壓力位",
    "阻力",
    "新產品",
    "客戶布局",
    "客戶佈局",
    "未來EPS預測",
    "未來 EPS 預測",
    "全球需求",
    "外部需求",
    "需求放緩",
    "宏觀",
    "市場情緒",
)
_UNHELD_POSITION_WORDS = ("續抱", "持有中", "未實現損益", "加碼", "賣出", "停損", "調節")
_MISREPRESENTATION_WORDS = ("公平估值", "合理估值", "目標價", "必然上漲", "必然下跌")


class AIAnalysisError(RuntimeError):
    pass


class AIConfigurationError(AIAnalysisError):
    pass


@dataclass(frozen=True)
class AIProviderResult:
    analysis: StockAIAnalysisContent
    raw_response_text: str
    provider_metadata: dict[str, Any]
    validation_errors: list[str]


class AIProvider(Protocol):
    provider_id: str
    model: str

    def analyze_stock(self, stock_summary: dict[str, Any], analysis_mode: str) -> AIProviderResult:
        ...


@dataclass(frozen=True)
class GeminiProvider:
    api_key: str
    model: str
    timeout_seconds: int = 45
    provider_id: str = "gemini"

    def analyze_stock(self, stock_summary: dict[str, Any], analysis_mode: str) -> AIProviderResult:
        prompts = _analysis_prompts(stock_summary, analysis_mode)
        payload = {
            "system_instruction": {"parts": [{"text": prompts.system}]},
            "contents": [{"role": "user", "parts": [{"text": prompts.user}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 1400,
                "responseMimeType": "application/json",
            },
        }
        try:
            response = requests.post(
                GEMINI_GENERATE_URL.format(model=self.model),
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise AIAnalysisError(f"Gemini API request failed: {exc}") from exc
        _raise_for_provider_error(response, "Gemini")
        data = response.json()
        raw_text = _gemini_text(data)
        analysis, errors = normalize_ai_analysis_with_errors(raw_text, analysis_mode)
        candidate = (data.get("candidates") or [{}])[0]
        return AIProviderResult(
            analysis=analysis,
            raw_response_text=raw_text,
            provider_metadata={
                "response_id": data.get("responseId"),
                "model_version": data.get("modelVersion"),
                "finish_reason": candidate.get("finishReason"),
                "usage": data.get("usageMetadata"),
            },
            validation_errors=errors,
        )


@dataclass(frozen=True)
class OpenRouterProvider:
    api_key: str
    model: str
    timeout_seconds: int = 45
    provider_id: str = "openrouter"

    def analyze_stock(self, stock_summary: dict[str, Any], analysis_mode: str) -> AIProviderResult:
        prompts = _analysis_prompts(stock_summary, analysis_mode)
        strict_payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompts.system},
                {"role": "user", "content": prompts.user},
            ],
            "temperature": 0.2,
            "max_tokens": 1800,
            "reasoning": {"exclude": True},
            "provider": {"require_parameters": True},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": f"stock_analysis_{analysis_mode.lower()}",
                    "strict": True,
                    "schema": analysis_json_schema(analysis_mode),
                },
            },
        }
        response = self._post(strict_payload)
        structured_output_mode = "json_schema"
        routing_fallback_reason = None
        if _openrouter_requires_relaxed_routing(response):
            relaxed_payload = {
                **strict_payload,
                "provider": {"require_parameters": False},
                "response_format": {"type": "json_object"},
            }
            response = self._post(relaxed_payload)
            structured_output_mode = "json_object_fallback"
            routing_fallback_reason = "No provider endpoint accepted all strict JSON schema parameters."
        _raise_for_provider_error(response, "OpenRouter")
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise AIAnalysisError("OpenRouter did not return any choices.")
        choice = choices[0]
        message = choice.get("message") or {}
        raw_text = _message_content_text(message)
        analysis, errors = normalize_ai_analysis_with_errors(raw_text, analysis_mode)
        return AIProviderResult(
            analysis=analysis,
            raw_response_text=raw_text,
            provider_metadata={
                "id": data.get("id"),
                "created": data.get("created"),
                "finish_reason": choice.get("finish_reason"),
                "native_finish_reason": choice.get("native_finish_reason"),
                "usage": data.get("usage"),
                "structured_output_mode": structured_output_mode,
                "routing_fallback_reason": routing_fallback_reason,
            },
            validation_errors=errors,
        )

    def _post(self, payload: dict[str, Any]) -> requests.Response:
        try:
            return requests.post(
                OPENROUTER_CHAT_URL,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise AIAnalysisError(f"OpenRouter API request failed: {exc}") from exc


@dataclass(frozen=True)
class AnalysisPrompts:
    system: str
    user: str


def build_ai_provider(settings, provider_id: str | None = None) -> AIProvider:
    normalized, model = ai_provider_identity(settings, provider_id)
    if normalized == "gemini":
        if not settings.gemini_api_key:
            raise AIConfigurationError("GEMINI_API_KEY is not configured.")
        return GeminiProvider(api_key=settings.gemini_api_key, model=model)
    if normalized == "openrouter":
        if not settings.openrouter_api_key:
            raise AIConfigurationError("OPENROUTER_API_KEY is not configured.")
        return OpenRouterProvider(api_key=settings.openrouter_api_key, model=model)
    raise AIConfigurationError(f"Unsupported AI provider: {normalized}")


def ai_provider_identity(settings, provider_id: str | None = None) -> tuple[str, str]:
    normalized = (provider_id or settings.ai_provider or "gemini").strip().lower()
    if normalized == "gemini":
        if not settings.gemini_model:
            raise AIConfigurationError("GEMINI_MODEL is not configured.")
        return normalized, settings.gemini_model
    if normalized == "openrouter":
        if not settings.openrouter_model:
            raise AIConfigurationError("OPENROUTER_MODEL is not configured.")
        return normalized, settings.openrouter_model
    raise AIConfigurationError(f"Unsupported AI provider: {provider_id}")


def stock_summary_hash(stock_summary: dict[str, Any]) -> str:
    payload = json.dumps(stock_summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def analysis_json_schema(analysis_mode: str) -> dict[str, Any]:
    allowed = list(_allowed_statuses(analysis_mode))
    return {
        "type": "object",
        "properties": {
            "overall_status": {"type": "string", "enum": allowed},
            "summary": {"type": "string", "minLength": 40, "maxLength": 360},
            "positive_points": {
                "type": "array",
                "items": {"type": "string", "maxLength": 80},
                "maxItems": 5,
            },
            "risk_points": {
                "type": "array",
                "items": {"type": "string", "maxLength": 80},
                "maxItems": 5,
            },
            "watch_points": {
                "type": "array",
                "items": {"type": "string", "maxLength": 80},
                "maxItems": 5,
            },
            "disclaimer": {"type": "string", "maxLength": 120},
        },
        "required": [
            "overall_status",
            "summary",
            "positive_points",
            "risk_points",
            "watch_points",
            "disclaimer",
        ],
        "additionalProperties": False,
    }


def normalize_ai_analysis(value: Any, analysis_mode: str = AI_MODE_GENERAL) -> StockAIAnalysisContent:
    analysis, _ = normalize_ai_analysis_with_errors(value, analysis_mode)
    return analysis


def normalize_ai_analysis_with_errors(
    value: Any,
    analysis_mode: str,
) -> tuple[StockAIAnalysisContent, list[str]]:
    errors: list[str] = []
    payload = value
    if isinstance(value, str):
        payload = _parse_json_object(value)
        if payload is None:
            fallback = _plain_text_analysis(value, analysis_mode)
            return fallback, ["AI response was not a JSON object."]
    if not isinstance(payload, dict):
        fallback = _plain_text_analysis(str(value), analysis_mode)
        return fallback, ["AI response was not a JSON object."]
    if isinstance(payload.get("analysis"), dict):
        payload = payload["analysis"]
    payload = _normalize_analysis_keys(payload)

    required = ("overall_status", "summary", "positive_points", "risk_points", "watch_points")
    for key in required:
        if key not in payload:
            errors.append(f"Missing required field: {key}")

    allowed = _allowed_statuses(analysis_mode)
    status = _clean_text(payload.get("overall_status"), _fallback_status(analysis_mode), 24)
    if status not in allowed:
        errors.append(f"Unsupported status for {analysis_mode}: {status}")
        status = _fallback_status(analysis_mode)

    summary = _sanitize_summary(
        _clean_text(payload.get("summary"), "AI 暫時沒有產生摘要。", 360),
        analysis_mode,
        errors,
    )
    positive_points = _sanitize_points(payload.get("positive_points"), "positive_points", analysis_mode, errors)
    risk_points = _sanitize_points(payload.get("risk_points"), "risk_points", analysis_mode, errors)
    watch_points = _sanitize_points(payload.get("watch_points"), "watch_points", analysis_mode, errors)

    if summary == "AI 暫時沒有產生摘要。":
        errors.append("Summary is empty or missing.")
    elif len(summary) < 40:
        errors.append("Summary is shorter than 40 characters.")

    return StockAIAnalysisContent(
        overall_status=status,
        summary=summary,
        positive_points=positive_points,
        risk_points=risk_points,
        watch_points=watch_points,
        disclaimer=_clean_text(payload.get("disclaimer"), DEFAULT_DISCLAIMER, 120),
        format_valid=not _has_fatal_validation_errors(errors),
    ), errors


def _analysis_prompts(stock_summary: dict[str, Any], analysis_mode: str) -> AnalysisPrompts:
    allowed = " / ".join(_allowed_statuses(analysis_mode))
    if analysis_mode == AI_MODE_UNHELD:
        objective = (
            "這是未持有的新進場評估。使用者目前沒有持有此標的。"
            "評估現在是否適合開始建立部位，結論只能是分批布局、等待、避開或資料不足。"
            "不得使用續抱、調節、賣出、持有成本或未實現損益語意。"
        )
    elif analysis_mode == AI_MODE_HELD:
        objective = (
            "這是持有中評估。使用者已提供成交均價。"
            "評估持有理由是否仍成立，以及應續抱、觀察、分批調節或重新評估。"
            "可以使用成交均價與每股或百分比損益，但不得要求持股股數或總金額。"
        )
    else:
        objective = "請整理現有資料並給出保守的觀察結論。"

    system = (
        "你是台股資料解讀器，只能解讀輸入 JSON 中明確存在的資料。"
        f"{objective}"
        f"overall_status 只能是：{allowed}。"
        "私人持股股數、持股比例、總成本、持倉市值、資產規模、總資產與帳戶資訊是刻意省略的。"
        "不得要求、推測或把這些私人資料的缺少列為風險或觀察點。"
        "不得把任何未提供欄位列為缺失，也不得引用輸入中不存在的外資、投信、三大法人、"
        "未平倉、支撐壓力、產品、客戶或未來預測資料。"
        "EPS × 目前PE 是機械情境估算，不是預測價格、合理價保證或必然漲跌空間。"
        "volume_as_percent_of_ma20 是今日量占20日均量的比例；低於100代表量縮。"
        "volume_difference_vs_ma20_percent 才是今日量相對20日均量的增減百分比。"
        "不得自行重新計算後改寫輸入數字。不得承諾報酬或給出確定性買賣指令。"
        "輸出鍵名必須完全使用 overall_status、summary、positive_points、risk_points、watch_points、disclaimer，"
        "不得改名為 positives、risks、next_steps 或其他名稱。三個 points 欄位都必須是 JSON string array。"
        "只輸出符合 JSON schema 的一個 JSON object，不要 Markdown、code fence、推理過程或額外說明。"
    )
    user = (
        f"analysis_mode: {analysis_mode}\n"
        "請以繁體中文輸出 80 到 140 字摘要，以及最多五點正面因素、風險因素與後續觀察。\n\n"
        f"stock_summary:\n{json.dumps(stock_summary, ensure_ascii=False, indent=2)}"
    )
    return AnalysisPrompts(system=system, user=user)


def _allowed_statuses(analysis_mode: str) -> tuple[str, ...]:
    return ALLOWED_STATUSES.get(analysis_mode, ALLOWED_STATUSES[AI_MODE_GENERAL])


def _fallback_status(analysis_mode: str) -> str:
    return "資料不足" if analysis_mode == AI_MODE_UNHELD else "觀察"


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


def _openrouter_requires_relaxed_routing(response: requests.Response) -> bool:
    if response.status_code != 404:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    provider_error = body.get("error") if isinstance(body, dict) else None
    if isinstance(provider_error, dict):
        message = str(provider_error.get("message") or "")
    else:
        message = str(provider_error or "")
    return "No endpoints found that can handle the requested parameters" in message


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
    extracted = _extract_json(text)
    candidates = [text.strip(), extracted, _repair_json_text(extracted)]
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
    keys = ("overall_status", "summary", "positive_points", "risk_points", "watch_points")
    if not any(key in normalized for key in keys):
        return None
    parsed = {
        "overall_status": _extract_loose_string(normalized, "overall_status"),
        "summary": _extract_loose_string(normalized, "summary"),
        "positive_points": _extract_loose_list(normalized, "positive_points"),
        "risk_points": _extract_loose_list(normalized, "risk_points"),
        "watch_points": _extract_loose_list(normalized, "watch_points"),
        "disclaimer": _extract_loose_string(normalized, "disclaimer"),
    }
    return parsed if any(parsed.get(key) for key in keys) else None


def _normalize_analysis_keys(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    aliases = {
        "positive_points": ("positives", "strengths"),
        "risk_points": ("risks", "concerns"),
        "watch_points": ("next_steps", "watchlist"),
    }
    for target, source_keys in aliases.items():
        if target in normalized:
            continue
        for source in source_keys:
            if source in normalized:
                normalized[target] = normalized[source]
                break
    return normalized


def _extract_loose_string(text: str, key: str) -> str | None:
    match = re.search(
        rf'["\']?{re.escape(key)}["\']?\s*:\s*["\']?(.*?)(?=(?:["\']?(?:overall_status|summary|positive_points|risk_points|watch_points|disclaimer)["\']?\s*:)|$)',
        text,
        flags=re.DOTALL,
    )
    if not match:
        return None
    value = re.sub(r"^\s*[,{\[]+", "", match.group(1))
    value = re.sub(r"[,}\]]+\s*$", "", value)
    return value.strip().strip('"').strip("'").strip() or None


def _extract_loose_list(text: str, key: str) -> list[str]:
    array_match = re.search(rf'["\']?{re.escape(key)}["\']?\s*:\s*(\[.*?\])', text, flags=re.DOTALL)
    if array_match:
        try:
            return _clean_list(json.loads(_repair_json_text(array_match.group(1))), 5, 80)
        except json.JSONDecodeError:
            pass
    return _split_loose_items(_extract_loose_string(text, key))


def _split_loose_items(value: str | None) -> list[str]:
    if not value:
        return []
    parts = re.split(r"\n+|[；;]", value.replace("\\n", "\n"))
    return _clean_list([part.lstrip("-•0123456789.、) ").strip() for part in parts], 5, 80)


def _sanitize_summary(value: str, analysis_mode: str, errors: list[str]) -> str:
    sentences = re.split(r"(?<=[。！？!?])\s*", value)
    kept = []
    for sentence in sentences:
        if not sentence:
            continue
        violation = _validation_violation(sentence, analysis_mode)
        if violation:
            errors.append(f"summary: {violation}")
            continue
        kept.append(sentence)
    return "".join(kept).strip() or "AI 暫時沒有產生摘要。"


def _sanitize_points(value: Any, field: str, analysis_mode: str, errors: list[str]) -> list[str]:
    cleaned = _clean_list(value, 5, 80)
    kept = []
    for item in cleaned:
        violation = _validation_violation(item, analysis_mode)
        if violation:
            errors.append(f"{field}: {violation}")
            continue
        kept.append(item)
    return kept


def _validation_violation(text: str, analysis_mode: str) -> str | None:
    if re.search(r"([{}\[\]])\1{2,}", text) or "\\\"" in text:
        return "malformed content"
    if any(word in text for word in _PRIVATE_POSITION_WORDS):
        return "private position data requested or inferred"
    if any(word in text for word in _MISSING_DATA_WORDS):
        return "warning: missing data presented as a finding"
    if any(word in text for word in _UNSUPPORTED_CONTEXT_WORDS):
        return "warning: unsupported context referenced"
    if any(word in text for word in _MISREPRESENTATION_WORDS):
        return "warning: mechanical estimate misrepresented"
    if analysis_mode == AI_MODE_UNHELD and any(word in text for word in _UNHELD_POSITION_WORDS):
        return "held-position language used in unheld analysis"
    return None


def _has_fatal_validation_errors(errors: list[str]) -> bool:
    return any("warning:" not in error for error in errors)


def _plain_text_analysis(text: str, analysis_mode: str) -> StockAIAnalysisContent:
    summary = re.sub(r"\s+", " ", text).strip()
    return StockAIAnalysisContent(
        overall_status=_fallback_status(analysis_mode),
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
