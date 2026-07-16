from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai_analysis import (
    AI_MODE_HELD,
    AI_MODE_UNHELD,
    PROMPT_VERSION,
    RULE_VERSION,
    AIAnalysisError,
    AIConfigurationError,
    AIProviderResult,
    GeminiProvider,
    OpenRouterProvider,
    stock_summary_hash,
)
from ..config import get_settings
from ..data_quality import freshness_for_state
from ..db.models import (
    AIProviderHealth,
    Stock,
    StockAIAnalysis,
    StockAIAnalysisRun,
    StockDataQualityState,
    StockPosition,
)
from ..db.session import SessionLocal
from ..schema.ai import (
    AIProviderHealthResponse,
    StockAIAnalysisModesResponse,
    StockAIAnalysisResponse,
    StockAIAnalysisRunResponse,
    StockAIAnalysisContent,
    StockAIDataAsOfResponse,
    StockAIRuleBasedModesResponse,
    StockAIRuleBasedResultResponse,
)
from .application import (
    AI_ANALYSIS_INFLIGHT_TIMEOUT,
    AI_ANALYSIS_JOB_LOCK,
    QUALITY_LABELS,
    TAIPEI_TZ,
    _ai_analysis_result_response,
    _ai_cache_input_hash,
    _ai_stock_summary,
    _compact_ai_stock_summary,
    _json_field,
    _rule_based_ai_analysis,
    _rule_based_result_response,
)


settings = get_settings()
ANALYSIS_SNAPSHOT_VERSION = 1
ACTIVE_RUN_STATUSES = ("queued", "running")
SUCCESS_RUN_STATUSES = ("success", "partial")
QUALITY_CATEGORIES = (
    "QUOTE",
    "CURRENT_PE",
    "PE_HISTORY",
    "EPS",
    "FINANCIAL_QUARTER",
    "MONTHLY_REVENUE",
    "BROKER_TRADING",
    "TECHNICAL_DAILY",
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json_datetime(value: datetime | None) -> str | None:
    normalized = _utc(value)
    return normalized.isoformat() if normalized else None


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = (value or "").strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _provider_order(requested_provider: str | None = None) -> list[str]:
    # AI_PROVIDER_ORDER is authoritative. AI_PROVIDER remains a compatibility
    # fallback for existing .env files that have not adopted the ordered list.
    return _unique([
        requested_provider or "",
        *settings.ai_provider_order,
        settings.ai_provider,
    ])


def _provider_specs(requested_provider: str | None = None) -> list[tuple[str, str, bool, str | None]]:
    specs: list[tuple[str, str, bool, str | None]] = []
    for provider_id in _provider_order(requested_provider):
        if provider_id == "openrouter":
            models = _unique([settings.openrouter_model, *settings.openrouter_fallback_models])
            if models:
                for model in models:
                    if not settings.openrouter_api_key:
                        specs.append((provider_id, model, False, "OPENROUTER_API_KEY 尚未設定。"))
                    elif not model.endswith(":free"):
                        specs.append((provider_id, model, False, "免費模式要求 OpenRouter 模型名稱以 :free 結尾。"))
                    else:
                        specs.append((provider_id, model, True, None))
            else:
                specs.append((provider_id, "未設定模型", False, "OPENROUTER_MODEL 尚未設定。"))
        elif provider_id == "gemini":
            configured = bool(settings.gemini_api_key and settings.gemini_model)
            specs.append((
                provider_id,
                settings.gemini_model,
                configured,
                None if configured else "GEMINI_API_KEY 或 GEMINI_MODEL 尚未設定。",
            ))
    return specs


def _build_provider(provider_id: str, model: str):
    if provider_id == "openrouter":
        if not settings.openrouter_api_key:
            raise AIConfigurationError("OPENROUTER_API_KEY is not configured.")
        return OpenRouterProvider(api_key=settings.openrouter_api_key, model=model)
    if provider_id == "gemini":
        if not settings.gemini_api_key:
            raise AIConfigurationError("GEMINI_API_KEY is not configured.")
        return GeminiProvider(api_key=settings.gemini_api_key, model=model)
    raise AIConfigurationError(f"Unsupported AI provider: {provider_id}")


def _health_row(session: Session, provider: str, model: str) -> AIProviderHealth:
    row = session.scalar(
        select(AIProviderHealth).where(
            AIProviderHealth.provider == provider,
            AIProviderHealth.model == model,
        )
    )
    if row is None:
        row = AIProviderHealth(provider=provider, model=model, status="HEALTHY")
        session.add(row)
        session.flush()
    return row


def _health_is_cooling(row: AIProviderHealth, now: datetime | None = None) -> bool:
    current = now or datetime.now(UTC)
    cooldown_until = _utc(row.cooldown_until)
    if cooldown_until and cooldown_until > current:
        return True
    if cooldown_until and cooldown_until <= current:
        row.cooldown_until = None
        row.status = "DEGRADED"
    return False


def _provider_error_status(exc: AIAnalysisError) -> int | None:
    if exc.status_code is not None:
        return exc.status_code
    message = str(exc).lower()
    for code in (429, 500, 502, 503, 504, 401, 403, 404):
        if f"status {code}" in message:
            return code
    return None


def _record_provider_failure(
    session: Session,
    provider: str,
    model: str,
    error: AIAnalysisError | str,
    *,
    format_failure: bool = False,
) -> None:
    now = datetime.now(UTC)
    row = _health_row(session, provider, model)
    message = str(error)
    status_code = _provider_error_status(error) if isinstance(error, AIAnalysisError) else None
    if format_failure:
        cooldown_seconds = settings.ai_format_failure_cooldown_seconds
    elif status_code == 429:
        cooldown_seconds = settings.ai_rate_limit_cooldown_seconds
    elif status_code in {500, 502, 503, 504} or "timeout" in message.lower() or "timed out" in message.lower():
        cooldown_seconds = settings.ai_outage_cooldown_seconds
    else:
        cooldown_seconds = settings.ai_outage_cooldown_seconds
    row.status = "COOLDOWN" if cooldown_seconds > 0 else "DEGRADED"
    row.consecutive_failures += 1
    row.last_attempt_at = now
    row.last_failure_at = now
    row.last_http_status = status_code
    row.last_error_summary = message[:1000]
    row.cooldown_until = now + timedelta(seconds=cooldown_seconds) if cooldown_seconds > 0 else None
    row.updated_at = now
    session.commit()


def _record_provider_success(
    session: Session,
    provider: str,
    model: str,
    *,
    degraded_reason: str | None = None,
) -> None:
    now = datetime.now(UTC)
    row = _health_row(session, provider, model)
    row.status = "DEGRADED" if degraded_reason else "HEALTHY"
    row.consecutive_failures = 0
    row.last_attempt_at = now
    row.last_success_at = now
    row.last_http_status = None
    row.last_error_summary = degraded_reason
    row.cooldown_until = None
    row.updated_at = now
    session.commit()


def provider_health_responses(
    session: Session,
    requested_provider: str | None = None,
) -> list[AIProviderHealthResponse]:
    responses: list[AIProviderHealthResponse] = []
    now = datetime.now(UTC)
    for provider, model, configured, unavailable_reason in _provider_specs(requested_provider):
        if not configured:
            responses.append(
                AIProviderHealthResponse(
                    provider=provider,
                    model=model,
                    status="UNAVAILABLE",
                    configured=False,
                    last_error_summary=unavailable_reason or "API key 或模型尚未設定。",
                )
            )
            continue
        row = _health_row(session, provider, model)
        changed = bool(row.cooldown_until and not _health_is_cooling(row, now))
        if changed:
            row.updated_at = now
            session.commit()
        responses.append(
            AIProviderHealthResponse(
                provider=provider,
                model=model,
                status=row.status,
                configured=True,
                consecutive_failures=row.consecutive_failures,
                last_attempt_at=_utc(row.last_attempt_at),
                last_success_at=_utc(row.last_success_at),
                last_failure_at=_utc(row.last_failure_at),
                last_http_status=row.last_http_status,
                last_error_summary=row.last_error_summary,
                cooldown_until=_utc(row.cooldown_until),
            )
        )
    return responses


def _available_provider_specs(
    session: Session,
    requested_provider: str | None = None,
) -> list[tuple[str, str]]:
    available: list[tuple[str, str]] = []
    for provider, model, configured, _ in _provider_specs(requested_provider):
        if not configured:
            continue
        health = _health_row(session, provider, model)
        if not _health_is_cooling(health):
            available.append((provider, model))
    session.commit()
    return available


def _data_as_of(session: Session, stock: Stock) -> tuple[list[dict[str, Any]], list[str]]:
    now = datetime.now(UTC)
    states = {
        row.category: row
        for row in session.scalars(
            select(StockDataQualityState).where(StockDataQualityState.stock_id == stock.id)
        ).all()
    }
    is_etf = stock.asset_type == "ETF"
    items: list[dict[str, Any]] = []
    stale_items: list[str] = []
    for category in QUALITY_CATEGORIES:
        applicable = not (is_etf and category in {"CURRENT_PE", "PE_HISTORY", "EPS", "FINANCIAL_QUARTER", "MONTHLY_REVENUE"})
        state = states.get(category)
        freshness = freshness_for_state(session, state, category, now=now, applicable=applicable)
        item = {
            "category": category,
            "label": QUALITY_LABELS.get(category, category),
            "data_date": state.data_date.isoformat() if state and state.data_date else None,
            "data_period": state.data_period if state else None,
            "fetched_at": _json_datetime(state.fetched_at) if state else None,
            "source": state.source if state else None,
            "freshness_status": freshness,
            "is_cached": bool(state and state.is_cached),
        }
        items.append(item)
        if freshness in {"DELAYED", "STALE", "MISSING"}:
            stale_items.append(f"{item['label']}：{freshness}")
        elif item["is_cached"]:
            stale_items.append(f"{item['label']}：使用快取")
    return items, stale_items


def _analysis_modes(session: Session, stock: Stock) -> list[str]:
    modes = [AI_MODE_UNHELD]
    if session.scalar(select(StockPosition.id).where(StockPosition.stock_id == stock.id)):
        modes.append(AI_MODE_HELD)
    return modes


def build_analysis_snapshot(session: Session, stock: Stock) -> tuple[dict[str, Any], str]:
    requested_at = datetime.now(TAIPEI_TZ)
    modes = _analysis_modes(session, stock)
    summaries = {
        mode: _compact_ai_stock_summary(
            _ai_stock_summary(stock, session, mode),
            mode,
            analysis_now=requested_at,
        )
        for mode in modes
    }
    data_as_of, stale_items = _data_as_of(session, stock)
    for summary in summaries.values():
        summary["data_quality_context"] = {
            "items": data_as_of,
            "stale_items": stale_items,
        }
        evidence = summary.setdefault("evidence", {})
        for item in data_as_of:
            prefix = f"data_quality.{str(item['category']).lower()}"
            evidence[f"{prefix}.freshness_status"] = item["freshness_status"]
            evidence[f"{prefix}.is_cached"] = item["is_cached"]
            if item.get("data_date"):
                evidence[f"{prefix}.data_date"] = item["data_date"]
            if item.get("data_period"):
                evidence[f"{prefix}.data_period"] = item["data_period"]
        summary["available_evidence_keys"] = sorted(evidence)
    rules = {
        mode: _rule_based_ai_analysis(summary, mode).model_dump(mode="json")
        for mode, summary in summaries.items()
    }
    snapshot = {
        "snapshot_version": ANALYSIS_SNAPSHOT_VERSION,
        "snapshot_created_at": requested_at.isoformat(timespec="seconds"),
        "prompt_version": PROMPT_VERSION,
        "rule_version": RULE_VERSION,
        "symbol": stock.symbol,
        "requested_modes": modes,
        "modes": summaries,
        "rule_results": rules,
        "data_as_of": data_as_of,
        "stale_items": stale_items,
    }
    stable_payload = {
        "prompt_version": PROMPT_VERSION,
        "rule_version": RULE_VERSION,
        "requested_modes": modes,
        "mode_hashes": {mode: _ai_cache_input_hash(summary) for mode, summary in summaries.items()},
        "data_as_of": data_as_of,
    }
    return snapshot, stock_summary_hash(stable_payload)


def _fresh_inflight(run: StockAIAnalysisRun) -> bool:
    if run.status not in ACTIVE_RUN_STATUSES:
        return False
    updated_at = _utc(run.updated_at) or datetime.now(UTC)
    return datetime.now(UTC) - updated_at <= AI_ANALYSIS_INFLIGHT_TIMEOUT


def _latest_run(session: Session, stock_id: int) -> StockAIAnalysisRun | None:
    return session.scalar(
        select(StockAIAnalysisRun)
        .where(
            StockAIAnalysisRun.stock_id == stock_id,
            StockAIAnalysisRun.prompt_version == PROMPT_VERSION,
        )
        .order_by(StockAIAnalysisRun.updated_at.desc())
        .limit(1)
    )


def _latest_success_row(session: Session, stock_id: int, mode: str) -> StockAIAnalysis | None:
    return session.scalar(
        select(StockAIAnalysis)
        .where(
            StockAIAnalysis.stock_id == stock_id,
            StockAIAnalysis.analysis_mode == mode,
            StockAIAnalysis.prompt_version == PROMPT_VERSION,
            StockAIAnalysis.status == "success",
        )
        .order_by(StockAIAnalysis.updated_at.desc())
        .limit(1)
    )


def enqueue_analysis_run(
    session: Session,
    stock: Stock,
    *,
    requested_provider: str | None = None,
    force_refresh: bool = False,
) -> tuple[StockAIAnalysisRun, bool]:
    snapshot, snapshot_hash = build_analysis_snapshot(session, stock)
    snapshot["requested_provider"] = requested_provider
    inflight = session.scalars(
        select(StockAIAnalysisRun)
        .where(
            StockAIAnalysisRun.stock_id == stock.id,
            StockAIAnalysisRun.prompt_version == PROMPT_VERSION,
            StockAIAnalysisRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .order_by(StockAIAnalysisRun.updated_at.desc())
        .limit(5)
    ).all()
    current_inflight = next((run for run in inflight if _fresh_inflight(run)), None)
    if current_inflight is not None:
        return current_inflight, True
    for stale_run in inflight:
        stale_run.status = "failed"
        stale_run.error_message = "AI 分析工作逾時，已解除執行鎖。"
        stale_run.finished_at = datetime.now(UTC)

    if not force_refresh:
        cached = session.scalar(
            select(StockAIAnalysisRun)
            .where(
                StockAIAnalysisRun.stock_id == stock.id,
                StockAIAnalysisRun.prompt_version == PROMPT_VERSION,
                StockAIAnalysisRun.rule_version == RULE_VERSION,
                StockAIAnalysisRun.snapshot_hash == snapshot_hash,
                StockAIAnalysisRun.status.in_(SUCCESS_RUN_STATUSES),
            )
            .order_by(StockAIAnalysisRun.updated_at.desc())
            .limit(1)
        )
        if cached is not None:
            session.commit()
            return cached, False

    candidates = _available_provider_specs(session, requested_provider)
    first_provider, first_model = candidates[0] if candidates else (None, None)
    now = datetime.now(UTC)
    run = StockAIAnalysisRun(
        stock_id=stock.id,
        provider=first_provider,
        model=first_model,
        prompt_version=PROMPT_VERSION,
        rule_version=RULE_VERSION,
        requested_modes_json=json.dumps(snapshot["requested_modes"], ensure_ascii=False),
        analysis_snapshot_json=json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
        snapshot_hash=snapshot_hash,
        rule_results_json=json.dumps(snapshot["rule_results"], ensure_ascii=False, sort_keys=True),
        data_as_of_json=json.dumps(snapshot["data_as_of"], ensure_ascii=False, sort_keys=True),
        stale_items_json=json.dumps(snapshot["stale_items"], ensure_ascii=False),
        request_strategy="batch" if len(snapshot["requested_modes"]) > 1 else "single",
        status="queued" if candidates else "failed",
        error_message=None if candidates else "所有已設定的免費 AI provider 目前不可用或仍在冷卻中，已顯示規則分析。",
        requested_at=now,
        finished_at=None if candidates else now,
        created_at=now,
        updated_at=now,
    )
    if not candidates:
        run.request_strategy = "rule_only"
    session.add(run)
    session.commit()
    session.refresh(run)
    return run, bool(candidates)


def _attempt_row(
    session: Session,
    run: StockAIAnalysisRun,
    provider,
    mode: str,
    summary: dict[str, Any],
    *,
    rule_status: str,
    result: AIProviderResult | None = None,
    error: str | None = None,
) -> StockAIAnalysis:
    now = datetime.now(UTC)
    status = "failed"
    normalized_analysis = None
    if result is not None:
        status = "success" if result.analysis.format_valid else "format_fallback"
        normalized_analysis = result.analysis.model_copy(update={"overall_status": rule_status})
    row = StockAIAnalysis(
        stock_id=run.stock_id,
        run_id=run.id,
        provider=provider.provider_id,
        model=provider.model,
        analysis_mode=mode,
        prompt_version=PROMPT_VERSION,
        analysis_date=run.requested_at.astimezone(TAIPEI_TZ).date() if run.requested_at.tzinfo else run.requested_at.date(),
        input_hash=_ai_cache_input_hash(summary),
        request_payload_json=json.dumps(summary, ensure_ascii=False, sort_keys=True),
        response_json=(
            json.dumps(normalized_analysis.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            if result is not None else "{}"
        ),
        raw_response_text=result.raw_response_text if result is not None else None,
        provider_metadata_json=(
            json.dumps(result.provider_metadata, ensure_ascii=False, sort_keys=True)
            if result is not None else None
        ),
        validation_errors_json=json.dumps(result.validation_errors if result is not None else [], ensure_ascii=False),
        quality_flags_json=json.dumps(result.quality_flags if result is not None else ["provider_error"], ensure_ascii=False),
        grounding_errors_json=json.dumps(result.grounding_errors if result is not None else [], ensure_ascii=False),
        status=status,
        error_message=(error if result is None else None if result.analysis.format_valid else "AI response failed validation."),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _call_single(provider, summary: dict[str, Any], mode: str, rule_status: str) -> AIProviderResult:
    try:
        return provider.analyze_stock(summary, mode, rule_status)
    except TypeError as exc:
        if "positional" not in str(exc) and "argument" not in str(exc):
            raise
        return provider.analyze_stock(summary, mode)


def _execute_analysis_run(session: Session, run: StockAIAnalysisRun) -> None:
    if run.status != "queued":
        return
    snapshot = _json_field(run.analysis_snapshot_json) or {}
    summaries: dict[str, dict[str, Any]] = snapshot.get("modes") or {}
    requested_modes: list[str] = snapshot.get("requested_modes") or []
    rule_results: dict[str, dict[str, Any]] = snapshot.get("rule_results") or {}
    rule_statuses = {mode: rule_results[mode]["overall_status"] for mode in requested_modes}
    requested_provider = snapshot.get("requested_provider")
    run.status = "running"
    run.started_at = datetime.now(UTC)
    run.updated_at = run.started_at
    session.commit()

    unresolved = set(requested_modes)
    successful: dict[str, StockAIAnalysis] = {}
    errors: list[str] = []
    attempts: list[dict[str, Any]] = []
    used_batch = False
    used_split = False
    used_provider_fallback = False
    candidate_specs = _available_provider_specs(session, requested_provider)

    for candidate_index, (provider_id, model) in enumerate(candidate_specs):
        if not unresolved:
            break
        if candidate_index > 0:
            used_provider_fallback = True
        try:
            provider = _build_provider(provider_id, model)
        except AIAnalysisError as exc:
            _record_provider_failure(session, provider_id, model, exc)
            errors.append(f"{provider_id}/{model}: {exc}")
            attempts.append({"provider": provider_id, "model": model, "status": "configuration_error", "error": str(exc)})
            continue
        target_modes = [mode for mode in requested_modes if mode in unresolved]
        success_count_before = len(successful)
        provider_had_format_issue = False

        if len(target_modes) > 1:
            used_batch = True
            try:
                batch_result = provider.analyze_stock_batch(
                    {mode: summaries[mode] for mode in target_modes},
                    {mode: rule_statuses[mode] for mode in target_modes},
                )
            except AIAnalysisError as exc:
                _record_provider_failure(session, provider_id, model, exc)
                for mode in target_modes:
                    _attempt_row(session, run, provider, mode, summaries[mode], rule_status=rule_statuses[mode], error=str(exc))
                errors.append(f"{provider_id}/{model}: {exc}")
                attempts.append({"provider": provider_id, "model": model, "status": "provider_error", "error": str(exc)})
                continue

            invalid_modes: list[str] = []
            for mode in target_modes:
                mode_result = batch_result.analyses[mode]
                row = _attempt_row(session, run, provider, mode, summaries[mode], rule_status=rule_statuses[mode], result=mode_result)
                if mode_result.analysis.format_valid:
                    successful[mode] = row
                    unresolved.discard(mode)
                else:
                    provider_had_format_issue = True
                    invalid_modes.append(mode)

            if invalid_modes:
                used_split = True
                persistent_format_failure = False
                for mode in invalid_modes:
                    try:
                        single_result = _call_single(provider, summaries[mode], mode, rule_statuses[mode])
                    except AIAnalysisError as exc:
                        _record_provider_failure(session, provider_id, model, exc)
                        _attempt_row(session, run, provider, mode, summaries[mode], rule_status=rule_statuses[mode], error=str(exc))
                        errors.append(f"{provider_id}/{model}/{mode}: {exc}")
                        continue
                    row = _attempt_row(session, run, provider, mode, summaries[mode], rule_status=rule_statuses[mode], result=single_result)
                    if single_result.analysis.format_valid:
                        successful[mode] = row
                        unresolved.discard(mode)
                    else:
                        persistent_format_failure = True
                        errors.append(f"{provider_id}/{model}/{mode}: 回覆格式驗證失敗")
                if persistent_format_failure:
                    _record_provider_failure(
                        session,
                        provider_id,
                        model,
                        "批次與單模式回覆皆未通過格式驗證。",
                        format_failure=True,
                    )
                elif not unresolved:
                    _record_provider_success(session, provider_id, model, degraded_reason="批次格式已由單模式補救。")
            else:
                _record_provider_success(session, provider_id, model)
        else:
            mode = target_modes[0]
            try:
                mode_result = _call_single(provider, summaries[mode], mode, rule_statuses[mode])
            except AIAnalysisError as exc:
                _record_provider_failure(session, provider_id, model, exc)
                _attempt_row(session, run, provider, mode, summaries[mode], rule_status=rule_statuses[mode], error=str(exc))
                errors.append(f"{provider_id}/{model}: {exc}")
                continue
            row = _attempt_row(session, run, provider, mode, summaries[mode], rule_status=rule_statuses[mode], result=mode_result)
            if mode_result.analysis.format_valid:
                successful[mode] = row
                unresolved.discard(mode)
                _record_provider_success(session, provider_id, model)
            else:
                provider_had_format_issue = True
                _record_provider_failure(session, provider_id, model, "單模式回覆未通過格式驗證。", format_failure=True)
                errors.append(f"{provider_id}/{model}/{mode}: 回覆格式驗證失敗")

        attempts.append({
            "provider": provider_id,
            "model": model,
            "status": "success" if not unresolved else "partial",
            "format_issue": provider_had_format_issue,
        })
        if len(successful) > success_count_before:
            run.provider = provider_id
            run.model = model

    strategies = []
    if len(requested_modes) == 1:
        strategies.append("single")
    elif used_batch:
        strategies.append("batch")
    if used_split:
        strategies.append("split_fallback")
    if used_provider_fallback:
        strategies.append("provider_fallback")
    run.request_strategy = "+".join(strategies) or "rule_only"
    run.status = "success" if not unresolved else "partial" if successful else "failed"
    run.error_message = "；".join(errors) if errors else None
    run.provider_metadata_json = json.dumps({"attempts": attempts}, ensure_ascii=False, sort_keys=True)
    run.finished_at = datetime.now(UTC)
    run.updated_at = run.finished_at
    session.commit()


def run_analysis_job(run_id: int) -> None:
    with AI_ANALYSIS_JOB_LOCK:
        with SessionLocal() as session:
            run = session.get(StockAIAnalysisRun, run_id)
            if run is not None:
                try:
                    _execute_analysis_run(session, run)
                except Exception as exc:  # Do not leave a permanent running lock after a background crash.
                    _fail_unexpected_run(session, run, exc)


def run_analysis_job_in_session(session: Session, run_id: int) -> None:
    run = session.get(StockAIAnalysisRun, run_id)
    if run is not None:
        try:
            _execute_analysis_run(session, run)
        except Exception as exc:
            _fail_unexpected_run(session, run, exc)


def _fail_unexpected_run(session: Session, run: StockAIAnalysisRun, exc: Exception) -> None:
    now = datetime.now(UTC)
    run.status = "failed"
    run.error_message = f"AI 分析背景工作發生未預期錯誤：{exc}"
    run.finished_at = now
    run.updated_at = now
    session.commit()


def _run_response(run: StockAIAnalysisRun | None) -> StockAIAnalysisRunResponse | None:
    if run is None:
        return None
    return StockAIAnalysisRunResponse(
        id=run.id,
        status=run.status,
        requested_modes=_json_field(run.requested_modes_json) or [],
        provider=run.provider,
        model=run.model,
        prompt_version=run.prompt_version,
        rule_version=run.rule_version,
        request_strategy=run.request_strategy,
        snapshot_hash=run.snapshot_hash,
        requested_at=_utc(run.requested_at),
        started_at=_utc(run.started_at),
        finished_at=_utc(run.finished_at),
    )


def _data_as_of_responses(items: list[dict[str, Any]]) -> list[StockAIDataAsOfResponse]:
    return [StockAIDataAsOfResponse.model_validate(item) for item in items]


def _changed_snapshot_items(saved: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[str]:
    saved_by_category = {item.get("category"): item for item in saved}
    changed: list[str] = []
    for item in current:
        old = saved_by_category.get(item.get("category"))
        if old is None:
            continue
        comparable_keys = ("data_date", "data_period", "fetched_at", "source")
        if any(old.get(key) != item.get(key) for key in comparable_keys):
            changed.append(f"{item.get('label') or item.get('category')}：分析後已有新資料")
    return changed


def build_analysis_response(
    session: Session,
    stock: Stock,
    *,
    requested_provider: str | None = None,
) -> StockAIAnalysisResponse:
    modes = _analysis_modes(session, stock)
    run = _latest_run(session, stock.id)
    if run and run.status in ACTIVE_RUN_STATUSES and not _fresh_inflight(run):
        run.status = "failed"
        run.error_message = "AI 分析工作逾時，已解除執行鎖。"
        run.finished_at = datetime.now(UTC)
        run.updated_at = run.finished_at
        session.commit()
        session.refresh(run)
    stored_rules = _json_field(run.rule_results_json) if run else None
    if isinstance(stored_rules, dict):
        rule_based = {
            mode: StockAIRuleBasedResultResponse(
                mode=mode,
                generated_at=_utc(run.requested_at),
                analysis=StockAIAnalysisContent.model_validate(stored_rules[mode]),
            )
            for mode in modes
            if mode in stored_rules
        }
    else:
        rule_based = {mode: _rule_based_result_response(stock, session, mode) for mode in modes}
    results = {}
    for mode in modes:
        row = _latest_success_row(session, stock.id, mode)
        if row is not None:
            results[mode] = _ai_analysis_result_response(row, cached=bool(run and row.run_id != run.id))

    current_data_as_of, current_stale = _data_as_of(session, stock)
    saved_data_as_of = _json_field(run.data_as_of_json) if run else None
    saved_stale = _json_field(run.stale_items_json) if run else None
    stale_items = list(dict.fromkeys([
        *(saved_stale or current_stale),
        *(_changed_snapshot_items(saved_data_as_of, current_data_as_of) if saved_data_as_of else []),
    ]))
    running = {
        mode.lower(): True
        for mode in (_json_field(run.requested_modes_json) or [])
    } if run and _fresh_inflight(run) else {}
    errors: dict[str, str] = {}
    if run and run.status in {"failed", "partial"} and run.error_message:
        for mode in modes:
            if mode not in results or results[mode].cached:
                errors[mode.lower()] = run.error_message

    return StockAIAnalysisResponse(
        symbol=stock.symbol,
        analyses=StockAIAnalysisModesResponse(
            unheld=results.get(AI_MODE_UNHELD),
            held=results.get(AI_MODE_HELD),
        ),
        rule_based=StockAIRuleBasedModesResponse(
            unheld=rule_based.get(AI_MODE_UNHELD),
            held=rule_based.get(AI_MODE_HELD),
        ),
        errors=errors,
        running=running,
        run=_run_response(run),
        provider_health=provider_health_responses(session, requested_provider),
        data_as_of=_data_as_of_responses(saved_data_as_of or current_data_as_of),
        stale_items=stale_items,
        request_strategy=run.request_strategy if run else None,
    )


def run_log_metadata(run: StockAIAnalysisRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "requested_modes": _json_field(run.requested_modes_json) or [],
        "prompt_version": run.prompt_version,
        "rule_version": run.rule_version,
        "snapshot_hash": run.snapshot_hash,
        "request_strategy": run.request_strategy,
        "data_as_of": _json_field(run.data_as_of_json) or [],
        "stale_items": _json_field(run.stale_items_json) or [],
        "analysis_snapshot": _json_field(run.analysis_snapshot_json),
        "requested_at": _json_datetime(run.requested_at),
        "started_at": _json_datetime(run.started_at),
        "finished_at": _json_datetime(run.finished_at),
    }
