from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai_analysis import (
    AI_MODE_HELD,
    AI_MODE_UNHELD,
    AIConfigurationError,
)
from ..config import get_settings
from ..db.models import Stock, StockAIAnalysis, StockAIAnalysisRun, StockAIFeedback, StockPosition
from ..db.session import get_session
from ..market_data import normalize_symbol
from ..schema.ai import (
    StockAIAnalysisFeedbackRequest,
    StockAIAnalysisFeedbackResponse,
    StockAIAnalysisRequest,
    StockAIAnalysisResponse,
)
from ..services.ai_service import (
    _ai_analysis_batch_response,
    _ai_log_record,
    _json_field,
    _latest_ai_cache_row,
    _latest_ai_inflight_row,
    _rule_based_result_response,
)
from ..services.ai_batch_service import (
    build_analysis_response,
    enqueue_analysis_run,
    provider_health_responses,
    run_analysis_job,
    run_analysis_job_in_session,
)


router = APIRouter()
settings = get_settings()
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


@router.get("/api/stocks/{symbol}/ai-analysis/latest", response_model=StockAIAnalysisResponse)
def get_latest_stock_ai_analysis(
    symbol: str,
    provider: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> StockAIAnalysisResponse:
    try:
        normalized_symbol = normalize_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stock = session.scalar(select(Stock).where(Stock.symbol == normalized_symbol, Stock.is_active.is_(True)))
    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found")

    return build_analysis_response(session, stock, requested_provider=provider)


@router.post("/api/stocks/{symbol}/ai-analysis", response_model=StockAIAnalysisResponse)
def create_stock_ai_analysis(
    symbol: str,
    response: Response,
    payload: StockAIAnalysisRequest = Body(default_factory=StockAIAnalysisRequest),
    background_tasks: BackgroundTasks = None,
    session: Session = Depends(get_session),
) -> StockAIAnalysisResponse:
    try:
        normalized_symbol = normalize_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stock = session.scalar(select(Stock).where(Stock.symbol == normalized_symbol, Stock.is_active.is_(True)))
    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found")

    run, should_run = enqueue_analysis_run(
        session,
        stock,
        requested_provider=payload.provider,
        force_refresh=payload.force_refresh,
    )
    if should_run and run.status == "queued":
        if background_tasks is None:
            run_analysis_job_in_session(session, run.id)
        else:
            background_tasks.add_task(run_analysis_job, run.id)
            response.status_code = status.HTTP_202_ACCEPTED
    return build_analysis_response(session, stock, requested_provider=payload.provider)


@router.get("/api/ai-analysis/provider-health")
def get_ai_provider_health(session: Session = Depends(get_session)):
    return provider_health_responses(session)


@router.post("/api/stocks/{symbol}/ai-analysis/{mode}/feedback", response_model=StockAIAnalysisFeedbackResponse)
def submit_stock_ai_analysis_feedback(
    symbol: str,
    mode: str,
    payload: StockAIAnalysisFeedbackRequest,
    session: Session = Depends(get_session),
) -> StockAIAnalysisFeedbackResponse:
    try:
        normalized_symbol = normalize_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    normalized_mode = mode.strip().upper()
    if normalized_mode not in {AI_MODE_UNHELD, AI_MODE_HELD}:
        raise HTTPException(status_code=400, detail="Unsupported analysis mode.")

    stock = session.scalar(select(Stock).where(Stock.symbol == normalized_symbol, Stock.is_active.is_(True)))
    if not stock:
        raise HTTPException(status_code=404, detail="Stock not found")

    query = select(StockAIAnalysis).where(
        StockAIAnalysis.stock_id == stock.id,
        StockAIAnalysis.analysis_mode == normalized_mode,
        StockAIAnalysis.status == "success",
    )
    if payload.analysis_id is not None:
        query = query.where(StockAIAnalysis.id == payload.analysis_id)
    else:
        query = query.order_by(StockAIAnalysis.updated_at.desc()).limit(1)
    analysis = session.scalar(query)
    if not analysis:
        raise HTTPException(status_code=404, detail="Successful AI analysis not found.")

    now = datetime.now(UTC)
    tags_json = json.dumps(payload.tags, ensure_ascii=False, sort_keys=True)
    feedback = session.scalar(select(StockAIFeedback).where(StockAIFeedback.analysis_id == analysis.id))
    if feedback:
        feedback.rating = payload.rating
        feedback.tags_json = tags_json
        feedback.note = payload.note.strip() if payload.note else None
        feedback.updated_at = now
    else:
        feedback = StockAIFeedback(
            analysis_id=analysis.id,
            stock_id=stock.id,
            symbol=stock.symbol,
            analysis_mode=normalized_mode,
            rating=payload.rating,
            tags_json=tags_json,
            note=payload.note.strip() if payload.note else None,
            created_at=now,
            updated_at=now,
        )
        session.add(feedback)
    session.commit()
    session.refresh(feedback)
    return StockAIAnalysisFeedbackResponse(
        status="ok",
        analysis_id=analysis.id,
        rating=feedback.rating,
        tags=_json_field(feedback.tags_json) or [],
        note=feedback.note,
        updated_at=feedback.updated_at,
    )


@router.get("/api/ai-analysis/logs/summary")
def ai_analysis_logs_summary(
    symbol: str | None = Query(default=None),
    mode: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    session: Session = Depends(get_session),
):
    query = select(StockAIAnalysis, Stock.symbol).join(Stock, Stock.id == StockAIAnalysis.stock_id)
    if symbol:
        try:
            query = query.where(Stock.symbol == normalize_symbol(symbol))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if mode:
        normalized_mode = mode.strip().upper()
        if normalized_mode not in {"GENERAL", AI_MODE_UNHELD, AI_MODE_HELD}:
            raise HTTPException(status_code=400, detail="Unsupported analysis mode.")
        query = query.where(StockAIAnalysis.analysis_mode == normalized_mode)
    if provider:
        query = query.where(StockAIAnalysis.provider == provider.strip().lower())
    if date_from:
        query = query.where(StockAIAnalysis.analysis_date >= date_from)
    if date_to:
        query = query.where(StockAIAnalysis.analysis_date <= date_to)

    rows = session.execute(query).all()
    analysis_ids = [row.id for row, _ in rows]
    feedback_rows = (
        session.scalars(select(StockAIFeedback).where(StockAIFeedback.analysis_id.in_(analysis_ids))).all()
        if analysis_ids
        else []
    )

    def increment(bucket: dict[str, int], key: str | None) -> None:
        bucket[str(key or "unknown")] = bucket.get(str(key or "unknown"), 0) + 1

    by_status: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    provider_error_counts = {"rate_limited": 0, "outage": 0, "other": 0}
    validation_error_counts: dict[str, int] = {}
    quality_flag_counts: dict[str, int] = {}
    grounding_error_counts: dict[str, int] = {}
    for row, _ in rows:
        increment(by_status, row.status)
        increment(by_mode, row.analysis_mode)
        increment(by_provider, row.provider)
        if row.status == "failed":
            error = (row.error_message or "").lower()
            if "status 429" in error or "rate limit" in error:
                provider_error_counts["rate_limited"] += 1
            elif any(token in error for token in ("status 500", "status 502", "status 503", "status 504", "bad gateway", "high demand")):
                provider_error_counts["outage"] += 1
            else:
                provider_error_counts["other"] += 1
        for error in _json_field(row.validation_errors_json) or []:
            increment(validation_error_counts, str(error).split(":")[-1].strip())
        for flag in _json_field(row.quality_flags_json) or []:
            increment(quality_flag_counts, str(flag))
        for error in _json_field(row.grounding_errors_json) or []:
            increment(grounding_error_counts, str(error))

    feedback_by_rating: dict[str, int] = {}
    feedback_by_tag: dict[str, int] = {}
    for feedback in feedback_rows:
        increment(feedback_by_rating, feedback.rating)
        for tag in _json_field(feedback.tags_json) or []:
            increment(feedback_by_tag, str(tag))

    total = len(rows)
    run_ids = sorted({row.run_id for row, _ in rows if row.run_id is not None})
    run_rows = (
        session.scalars(select(StockAIAnalysisRun).where(StockAIAnalysisRun.id.in_(run_ids))).all()
        if run_ids else []
    )
    by_request_strategy: dict[str, int] = {}
    by_run_status: dict[str, int] = {}
    for run in run_rows:
        increment(by_request_strategy, run.request_strategy)
        increment(by_run_status, run.status)
    return {
        "total": total,
        "by_status": by_status,
        "by_mode": by_mode,
        "by_provider": by_provider,
        "success_rate": round(by_status.get("success", 0) / total * 100, 2) if total else 0,
        "format_fallback_rate": round(by_status.get("format_fallback", 0) / total * 100, 2) if total else 0,
        "provider_errors": provider_error_counts,
        "validation_error_counts": validation_error_counts,
        "quality_flag_counts": quality_flag_counts,
        "grounding_error_counts": grounding_error_counts,
        "feedback": {
            "total": len(feedback_rows),
            "by_rating": feedback_by_rating,
            "by_tag": feedback_by_tag,
        },
        "runs": {
            "total": len(run_rows),
            "by_status": by_run_status,
            "by_request_strategy": by_request_strategy,
        },
        "provider_health": [item.model_dump(mode="json") for item in provider_health_responses(session)],
    }


@router.get("/api/ai-analysis/logs/export")
def export_ai_analysis_logs(
    format: str = Query(default="json", pattern="^(json|csv)$"),
    symbol: str | None = Query(default=None),
    mode: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
    session: Session = Depends(get_session),
):
    query = (
        select(StockAIAnalysis, Stock.symbol)
        .join(Stock, Stock.id == StockAIAnalysis.stock_id)
        .order_by(StockAIAnalysis.updated_at.desc())
        .limit(limit)
    )
    if symbol:
        try:
            query = query.where(Stock.symbol == normalize_symbol(symbol))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if mode:
        normalized_mode = mode.strip().upper()
        if normalized_mode not in {"GENERAL", AI_MODE_UNHELD, AI_MODE_HELD}:
            raise HTTPException(status_code=400, detail="Unsupported analysis mode.")
        query = query.where(StockAIAnalysis.analysis_mode == normalized_mode)
    if provider:
        query = query.where(StockAIAnalysis.provider == provider.strip().lower())
    if date_from:
        query = query.where(StockAIAnalysis.analysis_date >= date_from)
    if date_to:
        query = query.where(StockAIAnalysis.analysis_date <= date_to)

    rows = session.execute(query).all()
    analysis_ids = [row.id for row, _ in rows]
    feedback_by_analysis_id = {
        feedback.analysis_id: feedback
        for feedback in (
            session.scalars(select(StockAIFeedback).where(StockAIFeedback.analysis_id.in_(analysis_ids))).all()
            if analysis_ids
            else []
        )
    }
    records = [
        _ai_log_record(row, stock_symbol, feedback_by_analysis_id.get(row.id))
        for row, stock_symbol in rows
    ]
    filename = f"ai-analysis-logs-{datetime.now(TAIPEI_TZ).date().isoformat()}"
    if format == "json":
        return JSONResponse(
            content=jsonable_encoder(records),
            headers={"Content-Disposition": f'attachment; filename="{filename}.json"'},
        )

    output = io.StringIO()
    fieldnames = list(records[0].keys()) if records else [
        "id",
        "symbol",
        "analysis_mode",
        "prompt_version",
        "provider",
        "model",
        "analysis_date",
        "input_hash",
        "status",
        "error_message",
        "request_payload",
        "normalized_response",
        "raw_response_text",
        "provider_metadata",
        "validation_errors",
        "quality_flags",
        "grounding_errors",
        "run",
        "feedback",
        "created_at",
        "updated_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for record in records:
        writer.writerow(
            {
                key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (dict, list))
                else value
                for key, value in record.items()
            }
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
    )
