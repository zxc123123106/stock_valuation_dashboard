from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from .schemas import StockAIAnalysisContent, StockAIAnalysisEvidenceText


GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_DISCLAIMER = "本分析僅依據既有資料整理，不構成任何投資建議。"
PROMPT_VERSION = "v4-analysis-time"
AI_MODE_GENERAL = "GENERAL"
AI_MODE_UNHELD = "UNHELD"
AI_MODE_HELD = "HELD"
ALLOWED_STATUSES = {
    AI_MODE_GENERAL: ("續抱", "觀察", "分批調節", "重新評估"),
    AI_MODE_UNHELD: ("分批布局", "等待", "避開", "資料不足"),
    AI_MODE_HELD: ("續抱", "觀察", "分批調節", "重新評估"),
}

_PRIVATE_POSITION_WORDS = (
    "持股股數",
    "持有股數",
    "庫存股數",
    "部位股數",
)
_UNHELD_POSITION_WORDS = ("續抱", "持有中", "未實現損益", "加碼", "賣出", "停損", "調節")
_MISREPRESENTATION_WORDS = ("公平估值", "合理估值", "目標價", "必然上漲", "必然下跌")
_EVIDENCE_REPAIRED_WARNING = "warning: evidence keys repaired"


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
    quality_flags: list[str]
    grounding_errors: list[str]


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
        analysis, errors = normalize_ai_analysis_with_errors(
            raw_text,
            analysis_mode,
            evidence_keys=extract_evidence_keys(stock_summary),
        )
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
            quality_flags=quality_flags_from_validation_errors(errors),
            grounding_errors=grounding_errors_from_validation_errors(errors),
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
            "max_tokens": 2400,
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
        analysis, errors = normalize_ai_analysis_with_errors(
            raw_text,
            analysis_mode,
            evidence_keys=extract_evidence_keys(stock_summary),
        )
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
            quality_flags=quality_flags_from_validation_errors(errors),
            grounding_errors=grounding_errors_from_validation_errors(errors),
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


def extract_evidence_keys(stock_summary: dict[str, Any]) -> set[str] | None:
    evidence = stock_summary.get("evidence") if isinstance(stock_summary, dict) else None
    if not isinstance(evidence, dict):
        return None
    return {str(key) for key in evidence.keys()}


def quality_flags_from_validation_errors(errors: list[str]) -> list[str]:
    flags: set[str] = set()
    for error in errors:
        lower = str(error).lower()
        if "invalid evidence key" in lower or "missing evidence key" in lower:
            flags.add("grounding_error")
        if "unsupported status" in lower:
            flags.add("wrong_status")
        if "private position" in lower:
            flags.add("private_position_leak")
        if "missing data presented" in lower:
            flags.add("missing_data_as_finding")
        if "evidence keys repaired" in lower:
            flags.add("evidence_repaired")
        if "not a json" in lower or "missing required field" in lower:
            flags.add("format_issue")
        if "mechanical estimate misrepresented" in lower:
            flags.add("valuation_misrepresentation")
        if "malformed content" in lower:
            flags.add("malformed_content")
    return sorted(flags)


def grounding_errors_from_validation_errors(errors: list[str]) -> list[str]:
    return [
        str(error)
        for error in errors
        if (
            "invalid evidence key" in str(error).lower()
            or "missing evidence key" in str(error).lower()
        )
    ]


def analysis_json_schema(analysis_mode: str) -> dict[str, Any]:
    allowed = list(_allowed_statuses(analysis_mode))
    grounded_text_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 360},
            "evidence_keys": {
                "type": "array",
                "items": {"type": "string", "maxLength": 120},
                "maxItems": 4,
            },
        },
        "required": ["text", "evidence_keys"],
        "additionalProperties": False,
    }
    grounded_point_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 96},
            "evidence_keys": {
                "type": "array",
                "items": {"type": "string", "maxLength": 120},
                "maxItems": 3,
            },
        },
        "required": ["text", "evidence_keys"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "overall_status": {"type": "string", "enum": allowed},
            "summary": grounded_text_schema,
            "positive_points": {
                "type": "array",
                "items": grounded_point_schema,
                "maxItems": 3,
            },
            "risk_points": {
                "type": "array",
                "items": grounded_point_schema,
                "maxItems": 3,
            },
            "watch_points": {
                "type": "array",
                "items": grounded_point_schema,
                "maxItems": 3,
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


def normalize_ai_analysis(
    value: Any,
    analysis_mode: str = AI_MODE_GENERAL,
    evidence_keys: set[str] | None = None,
) -> StockAIAnalysisContent:
    analysis, _ = normalize_ai_analysis_with_errors(value, analysis_mode, evidence_keys=evidence_keys)
    return analysis


def normalize_ai_analysis_with_errors(
    value: Any,
    analysis_mode: str,
    evidence_keys: set[str] | None = None,
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

    summary = _sanitize_grounded_text(
        payload.get("summary"),
        "summary",
        analysis_mode,
        errors,
        evidence_keys,
        360,
        require_evidence=evidence_keys is not None,
    )
    positive_points = _sanitize_points(
        payload.get("positive_points"),
        "positive_points",
        analysis_mode,
        errors,
        evidence_keys,
    )
    risk_points = _sanitize_points(
        payload.get("risk_points"),
        "risk_points",
        analysis_mode,
        errors,
        evidence_keys,
    )
    watch_points = _sanitize_points(
        payload.get("watch_points"),
        "watch_points",
        analysis_mode,
        errors,
        evidence_keys,
    )

    if summary.text == "AI 暫時沒有產生摘要。":
        errors.append("Summary is empty or missing.")
    elif len(summary.text) < 40:
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
            "可以使用成交均價、每股損益、百分比損益、費後損益與公開市場資料，但不得要求或推測持有股數。"
        )
    else:
        objective = "請整理現有資料並給出保守的觀察結論。"

    system = (
        "你是台股資料解讀器，只能解讀輸入 JSON 中明確存在的資料。"
        f"{objective}"
        f"overall_status 只能是：{allowed}。"
        "持有股數是唯一禁用的個人部位資料，不得要求、推測或把未提供持有股數列為風險或觀察點。"
        "成交均價、每股損益、百分比損益、費後損益、公開財務資料、公開籌碼資料與市場指標都可以使用。"
        "若輸入沒有某項資料，可以在觀察點中保守提醒未來可補強該面向，但不得把未提供資料當成既定事實。"
        "請綜合估值、基本面、技術面、成交量、籌碼、PE 歷史位置、行情相對開盤/昨收/高低點的變化，不要只依賴單一指標。"
        "EPS × 目前PE 是機械情境估算，不是預測價格、合理價保證或必然漲跌空間。"
        "volume_as_percent_of_ma20 是今日量占20日均量的比例；低於100代表量縮。"
        "volume_difference_vs_ma20_percent 才是今日量相對20日均量的增減百分比。"
        "不得自行重新計算後改寫輸入數字。不得承諾報酬或給出確定性買賣指令。"
        "analysis_context.analysis_requested_at 是本次分析請求當下的 Asia/Taipei 日期時間，"
        "不是行情、財報或籌碼的資料更新時間。判斷資料新鮮度時，必須將它與各資料欄位自己的日期時間分開解讀。"
        "stock_summary.evidence 是唯一可引用的證據清單。summary、positive_points、risk_points、watch_points "
        "每一項都必須是 {\"text\": \"...\", \"evidence_keys\": [\"...\"]}，且 evidence_keys 只能使用 evidence 中存在的 key。"
        "每個非空結論至少綁定一個 evidence key；若沒有足夠證據，請降低結論強度並使用資料不足。"
        "輸出鍵名必須完全使用 overall_status、summary、positive_points、risk_points、watch_points、disclaimer，"
        "不得改名為 positives、risks、next_steps 或其他名稱。"
        "positive_points、risk_points、watch_points 各最多三點。"
        "只輸出符合 JSON schema 的一個 JSON object，不要 Markdown、code fence、推理過程或額外說明。"
    )
    user = (
        f"analysis_mode: {analysis_mode}\n"
        "請以繁體中文輸出 60 到 110 字摘要，以及最多三點正面因素、風險因素與後續觀察。"
        "每個 text 只寫結論，不要在文字中列出 evidence key。\n\n"
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
        raw_body = (getattr(response, "text", "") or "").strip()
        try:
            body = response.json()
            provider_error = body.get("error") if isinstance(body, dict) else None
            if isinstance(provider_error, dict):
                provider_message = provider_error.get("message") or provider_error.get("code") or ""
                metadata = provider_error.get("metadata")
                if isinstance(metadata, dict):
                    metadata_message = metadata.get("raw") or metadata.get("reason") or metadata.get("message")
                    if metadata_message and str(metadata_message) not in str(provider_message):
                        provider_message = f"{provider_message} {metadata_message}".strip()
                message = f"{message} {provider_message}".strip()
            elif isinstance(provider_error, str):
                message = f"{message} {provider_error}"
        except ValueError:
            if raw_body:
                message = f"{message} {raw_body[:300]}".strip()
        if response.status_code == 429:
            message = f"{message} 免費模型或供應商目前達到速率限制，請稍後重試或切換模型。"
        elif response.status_code in {500, 502, 503, 504}:
            message = f"{message} 模型供應商暫時不可用，請稍後重試；若已有快取，系統會保留最近成功分析。"
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
    if any(key in normalized for key in keys):
        parsed = {
            "overall_status": _extract_loose_string(normalized, "overall_status"),
            "summary": _extract_loose_string(normalized, "summary"),
            "positive_points": _extract_loose_list(normalized, "positive_points"),
            "risk_points": _extract_loose_list(normalized, "risk_points"),
            "watch_points": _extract_loose_list(normalized, "watch_points"),
            "disclaimer": _extract_loose_string(normalized, "disclaimer"),
        }
        return parsed if any(parsed.get(key) for key in keys) else None
    parsed = _parse_chinese_section_analysis(normalized)
    return parsed if parsed and any(parsed.get(key) for key in keys) else None


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


def _parse_chinese_section_analysis(text: str) -> dict[str, Any] | None:
    status = _extract_chinese_status(text)
    summary = _extract_chinese_section(text, ("摘要", "分析摘要", "總結", "整體摘要"))
    positive = _extract_chinese_section_items(text, ("正面因素", "優勢", "利多", "正面"))
    risks = _extract_chinese_section_items(text, ("風險因素", "風險", "利空"))
    watch = _extract_chinese_section_items(text, ("後續觀察", "觀察重點", "觀察", "追蹤重點"))
    if not any([status, summary, positive, risks, watch]):
        return None
    return {
        "overall_status": status,
        "summary": summary,
        "positive_points": positive,
        "risk_points": risks,
        "watch_points": watch,
        "disclaimer": _extract_chinese_section(text, ("免責聲明", "聲明")),
    }


def _extract_chinese_status(text: str) -> str | None:
    match = re.search(r"(?:狀態|結論|overall_status)\s*[:：]\s*([^\n，,。；;]+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip().strip('"').strip("'")
    for status_group in ALLOWED_STATUSES.values():
        for status in status_group:
            if re.search(rf"(?:^|[：:\s，,。；;]){re.escape(status)}(?:$|[：:\s，,。；;])", text):
                return status
    return None


def _extract_chinese_section(text: str, labels: tuple[str, ...]) -> str | None:
    block = _extract_chinese_section_block(text, labels)
    if not block:
        return None
    lines = [line.strip().lstrip("-•0123456789.、) ") for line in block.splitlines()]
    cleaned = " ".join(line for line in lines if line).strip()
    return cleaned or None


def _extract_chinese_section_items(text: str, labels: tuple[str, ...]) -> list[str]:
    block = _extract_chinese_section_block(text, labels)
    if not block:
        return []
    items: list[str] = []
    for line in block.replace("\\n", "\n").splitlines():
        cleaned = line.strip().lstrip("-•0123456789.、) ").strip()
        if cleaned:
            items.append(cleaned)
    if len(items) <= 1:
        items = _split_loose_items(block)
    return _clean_list(items, 5, 80)


def _extract_chinese_section_block(text: str, labels: tuple[str, ...]) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_labels = (
        "狀態",
        "結論",
        "摘要",
        "分析摘要",
        "總結",
        "整體摘要",
        "正面因素",
        "優勢",
        "利多",
        "正面",
        "風險因素",
        "風險",
        "利空",
        "後續觀察",
        "觀察重點",
        "觀察",
        "追蹤重點",
        "免責聲明",
        "聲明",
    )
    stop_pattern = "|".join(re.escape(label) for label in stop_labels)
    match = re.search(
        rf"(?:^|\n)\s*(?:{label_pattern})\s*[:：]?\s*(.*?)(?=\n\s*(?:{stop_pattern})\s*[:：]?|\Z)",
        text,
        flags=re.DOTALL,
    )
    if not match:
        return None
    return match.group(1).strip()


def _split_loose_items(value: str | None) -> list[str]:
    if not value:
        return []
    parts = re.split(r"\n+|[；;]", value.replace("\\n", "\n"))
    return _clean_list([part.lstrip("-•0123456789.、) ").strip() for part in parts], 5, 80)


def _sanitize_grounded_text(
    value: Any,
    field: str,
    analysis_mode: str,
    errors: list[str],
    evidence_keys: set[str] | None,
    text_limit: int,
    require_evidence: bool = False,
) -> StockAIAnalysisEvidenceText:
    text, keys = _coerce_grounded_text(value, text_limit)
    sentences = re.split(r"(?<=[。！？!?])\s*", text)
    kept = []
    for sentence in sentences:
        if not sentence:
            continue
        violation = _validation_violation(sentence, analysis_mode)
        if violation:
            errors.append(f"{field}: {violation}")
            continue
        kept.append(sentence)
    cleaned_text = "".join(kept).strip() or "AI 暫時沒有產生摘要。"
    valid_keys = _validate_evidence_keys(
        field,
        keys,
        evidence_keys,
        errors,
        require_evidence and cleaned_text,
        cleaned_text,
    )
    return StockAIAnalysisEvidenceText(text=cleaned_text, evidence_keys=valid_keys)


def _sanitize_points(
    value: Any,
    field: str,
    analysis_mode: str,
    errors: list[str],
    evidence_keys: set[str] | None,
) -> list[StockAIAnalysisEvidenceText]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = []
    kept = []
    for index, item in enumerate(items[:5]):
        grounded = _sanitize_grounded_text(
            item,
            f"{field}[{index}]",
            analysis_mode,
            errors,
            evidence_keys,
            80,
            require_evidence=evidence_keys is not None,
        )
        if grounded.text == "AI 暫時沒有產生摘要。":
            continue
        violation = _validation_violation(grounded.text, analysis_mode)
        if violation:
            errors.append(f"{field}: {violation}")
            continue
        kept.append(grounded)
    return kept


def _coerce_grounded_text(value: Any, text_limit: int) -> tuple[str, list[str]]:
    if isinstance(value, StockAIAnalysisEvidenceText):
        return value.text[:text_limit], list(value.evidence_keys or [])
    if isinstance(value, dict):
        text = _clean_text(value.get("text"), "AI 暫時沒有產生摘要。", text_limit)
        raw_keys = value.get("evidence_keys")
        keys = [str(key).strip() for key in raw_keys if str(key).strip()] if isinstance(raw_keys, list) else []
        return text, keys[:5]
    return _clean_text(value, "AI 暫時沒有產生摘要。", text_limit), []


def _validate_evidence_keys(
    field: str,
    keys: list[str],
    allowed_keys: set[str] | None,
    errors: list[str],
    require_evidence: bool | str,
    text: str = "",
) -> list[str]:
    if allowed_keys is None:
        return keys
    valid = []
    invalid = []
    for key in keys:
        if key in allowed_keys:
            valid.append(key)
        else:
            invalid.append(key)
    if invalid:
        errors.append(f"{field}: invalid evidence key(s): {', '.join(invalid[:5])}")
    if require_evidence and not valid:
        repaired = _infer_evidence_keys(text, allowed_keys, field)
        if repaired:
            errors.append(f"{field}: {_EVIDENCE_REPAIRED_WARNING}")
            return repaired
        errors.append(f"{field}: missing evidence key")
    return valid


def _infer_evidence_keys(text: str, allowed_keys: set[str], field: str) -> list[str]:
    if not allowed_keys:
        return []
    normalized = text.lower()
    candidates: list[str] = []

    def add_prefixes(*prefixes: str) -> None:
        for prefix in prefixes:
            for key in sorted(allowed_keys):
                if key.startswith(prefix) and key not in candidates:
                    candidates.append(key)
                    break

    if any(token in normalized for token in ("現價", "股價", "開盤", "昨收", "最高", "最低", "行情", "價格")):
        add_prefixes("quote.")
    if any(token.lower() in normalized for token in ("pe", "本益比", "估值", "估算", "預期股價", "預期損益", "成本估算")):
        add_prefixes("valuation.", "valuation_scenarios.")
    if any(token.lower() in normalized for token in ("eps", "營收", "毛利", "營益", "淨利", "基本面", "yoy", "mom", "sos")):
        add_prefixes("fundamental.", "valuation_scenarios.")
    if any(token.lower() in normalized for token in ("ma", "均線", "技術", "成交量", "量縮", "量增", "量能")):
        add_prefixes("technical.")
    if any(token in normalized for token in ("主力", "券商", "籌碼", "買超", "賣超", "買賣超")):
        add_prefixes("chip.")
    if any(token in normalized for token in ("成交均價", "成本", "損益", "費後", "持有")):
        add_prefixes("position.")

    if not candidates:
        preferred = (
            "quote.current_price_twd",
            "valuation.current_pe",
            "valuation.current_pe_vs_average_percent",
            "fundamental.ttm_eps_yoy_percent",
            "technical.latest.price_vs_ma20_percent",
            "technical.latest.volume_difference_vs_ma20_percent",
            "chip.main_net_volume_lots",
            "position.unrealized_return_percent",
        )
        for key in preferred:
            if key in allowed_keys and key not in candidates:
                candidates.append(key)
            if len(candidates) >= 3:
                break

    limit = 3 if field == "summary" else 2
    return candidates[:limit]


def _validation_violation(text: str, analysis_mode: str) -> str | None:
    if re.search(r"([{}\[\]])\1{2,}", text) or "\\\"" in text:
        return "malformed content"
    if any(word in text for word in _PRIVATE_POSITION_WORDS):
        return "private position data requested or inferred"
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
        summary=StockAIAnalysisEvidenceText(
            text=_clean_text(summary, "AI 已回覆，但格式不是 JSON。", 360),
            evidence_keys=[],
        ),
        positive_points=[],
        risk_points=[],
        watch_points=[
            StockAIAnalysisEvidenceText(text="可重新產生 AI 分析以取得結構化摘要。", evidence_keys=[])
        ],
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
