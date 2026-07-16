"""AI analysis use-case exports.

The implementation is being migrated from ``application`` behind this stable
service boundary; routers and new tests should import from here.
"""

from .application import (
    AI_ANALYSIS_JOB_LOCK,
    _ai_analysis_batch_response,
    _ai_analysis_result_response,
    _ai_feedback_record,
    _ai_log_record,
    _enqueue_ai_mode_with_fallback,
    _generate_ai_mode_with_fallback,
    _json_field,
    _latest_ai_cache_row,
    _latest_ai_inflight_row,
    _openrouter_model_candidates,
    _rule_based_result_response,
    _run_ai_analysis_job,
)
from .ai_batch_service import (
    build_analysis_response,
    build_analysis_snapshot,
    enqueue_analysis_run,
    provider_health_responses,
    run_analysis_job,
    run_analysis_job_in_session,
)


def acquire_ai_analysis_job_lock() -> None:
    AI_ANALYSIS_JOB_LOCK.acquire()


def release_ai_analysis_job_lock() -> None:
    AI_ANALYSIS_JOB_LOCK.release()

__all__ = [name for name in globals() if not name.startswith("__")]
