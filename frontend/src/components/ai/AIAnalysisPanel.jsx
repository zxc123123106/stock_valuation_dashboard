import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AlertCircle, CheckCircle2, Loader2, Sparkles, X } from "lucide-react";

import { DataQualityBadge } from "../shared/DataQualityBadge";
import { useAIAnalysis } from "../../hooks/useAIAnalysis";
import { formatDate } from "../../utils/formatters";


const AI_MODE_STORAGE_PREFIX = "stock-dashboard-ai-analysis-mode";
const AI_FEEDBACK_TAGS = [
  { label: "不準", tag: "wrong_number" },
  { label: "幻覺", tag: "hallucination" },
  { label: "太籠統", tag: "too_generic" },
  { label: "狀態不合理", tag: "wrong_status" },
];
const FRESHNESS_LABELS = {
  REALTIME: "即時",
  CURRENT: "最新",
  DELAYED: "延遲",
  STALE: "過期",
  MISSING: "待更新",
  NOT_APPLICABLE: "不適用",
};


function loadAiMode(symbol, hasPosition) {
  try {
    const stored = window.localStorage.getItem(`${AI_MODE_STORAGE_PREFIX}:${symbol}`);
    if (stored === "HELD" && hasPosition) {
      return "HELD";
    }
    if (stored === "UNHELD") {
      return "UNHELD";
    }
  } catch {
    // Storage can be unavailable in private browsing contexts.
  }
  return hasPosition ? "HELD" : "UNHELD";
}

function aiText(value) {
  if (typeof value === "string") {
    return value;
  }
  if (value && typeof value === "object") {
    return value.text || "";
  }
  return "";
}

function aiItemKey(item, index) {
  const text = aiText(item);
  return `${index}-${text}`;
}

export function AIAnalysisPopover({
  stock,
  open,
  anchorRef,
  onClose,
}) {
  const hasPosition = Boolean(stock.position);
  const panelRef = useRef(null);
  const [activeMode, setActiveMode] = useState(() => loadAiMode(stock.symbol, hasPosition));
  const [panelStyle, setPanelStyle] = useState({});
  const [clock, setClock] = useState(() => Date.now());
  const {
    analysisResponse,
    loading,
    running,
    error,
    generate,
    submitFeedback: submitAnalysisFeedback,
    feedbackStatus,
    feedbackSubmitting,
  } = useAIAnalysis(stock, open);

  useEffect(() => {
    setActiveMode(loadAiMode(stock.symbol, hasPosition));
  }, [stock.symbol]);

  useEffect(() => {
    if (!hasPosition && activeMode === "HELD") {
      setActiveMode("UNHELD");
    }
  }, [activeMode, hasPosition]);

  useEffect(() => {
    try {
      window.localStorage.setItem(`${AI_MODE_STORAGE_PREFIX}:${stock.symbol}`, activeMode);
    } catch {
      // The panel remains usable without persistent storage.
    }
  }, [activeMode, stock.symbol]);

  useEffect(() => {
    if (!open) {
      return undefined;
    }

    function positionPanel() {
      const anchor = anchorRef.current;
      if (!anchor) {
        return;
      }
      const rect = anchor.getBoundingClientRect();
      const viewportWidth = window.innerWidth;
      const viewportHeight = window.innerHeight;
      const mobile = viewportWidth <= 520;
      const width = mobile ? viewportWidth - 20 : Math.min(520, viewportWidth - 24);
      const left = mobile
        ? 10
        : Math.min(Math.max(12, rect.right - width), viewportWidth - width - 12);
      let top = mobile ? 10 : rect.bottom + 8;
      if (!mobile && viewportHeight - top < 360 && rect.top > 360) {
        top = Math.max(12, rect.top - Math.min(620, viewportHeight - 24) - 8);
      }
      if (!mobile) {
        top = Math.max(10, Math.min(top, viewportHeight - 180));
      }
      setPanelStyle({
        left,
        top,
        width,
        maxHeight: Math.max(160, viewportHeight - top - 10),
      });
    }

    function handlePointerDown(event) {
      if (panelRef.current?.contains(event.target) || anchorRef.current?.contains(event.target)) {
        return;
      }
      onClose();
    }

    function handleKeyDown(event) {
      if (event.key === "Escape") {
        onClose();
      }
    }

    positionPanel();
    window.addEventListener("resize", positionPanel);
    window.addEventListener("scroll", positionPanel, true);
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("resize", positionPanel);
      window.removeEventListener("scroll", positionPanel, true);
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [anchorRef, onClose, open]);

  useEffect(() => {
    if (!open) return undefined;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [open]);

  async function generateAnalysis() {
    await generate();
  }

  async function submitFeedback(rating, tags = []) {
    await submitAnalysisFeedback(activeMode, result?.id, rating, tags);
  }

  if (!open) {
    return null;
  }

  const modeKey = activeMode === "HELD" ? "held" : "unheld";
  const result = analysisResponse?.analyses?.[modeKey];
  const ruleResult = analysisResponse?.rule_based?.[modeKey];
  const displayResult = result || ruleResult;
  const analysis = displayResult?.analysis;
  const usingRuleSummary = Boolean(!result && ruleResult);
  const modeError = analysisResponse?.errors?.[modeKey];
  const hasAnyAnalysis = Boolean(analysisResponse?.analyses?.unheld || analysisResponse?.analyses?.held);
  const providerHealth = analysisResponse?.provider_health || [];
  const configuredProviders = providerHealth.filter((item) => item.configured !== false);
  const availableProviders = configuredProviders.filter((item) => {
    if (["HEALTHY", "DEGRADED"].includes(item.status)) return true;
    if (item.status === "COOLDOWN" && item.cooldown_until) {
      return new Date(item.cooldown_until).getTime() <= clock;
    }
    return false;
  });
  const providersUnavailable = providerHealth.length > 0 && availableProviders.length === 0;
  const cooldowns = configuredProviders
    .filter((item) => item.cooldown_until && new Date(item.cooldown_until).getTime() > clock)
    .map((item) => ({
      ...item,
      remaining: Math.max(0, Math.ceil((new Date(item.cooldown_until).getTime() - clock) / 1000)),
    }));

  return createPortal(
    <section
      ref={panelRef}
      className="ai-analysis-popover"
      style={panelStyle}
      role="dialog"
      aria-label={`${stock.symbol} AI 分析`}
    >
      <header className="ai-popover-header">
        <div>
          <span>AI 分析摘要</span>
          <strong>{stock.symbol} {stock.name}</strong>
        </div>
        <button className="icon-button small" type="button" onClick={onClose} aria-label="關閉 AI 分析">
          <X size={16} />
        </button>
      </header>
      <div className="ai-mode-tabs" role="tablist" aria-label="AI 分析類別">
        <button
          type="button"
          role="tab"
          aria-selected={activeMode === "UNHELD"}
          className={activeMode === "UNHELD" ? "active" : ""}
          onClick={() => setActiveMode("UNHELD")}
        >
          未持有
        </button>
        {hasPosition && (
          <button
            type="button"
            role="tab"
            aria-selected={activeMode === "HELD"}
            className={activeMode === "HELD" ? "active" : ""}
            onClick={() => setActiveMode("HELD")}
          >
            持有中
          </button>
        )}
      </div>
      <div className="ai-analysis-panel">
        <div className="ai-analysis-actions">
          <button
            className="text-button ai-analysis-button"
            type="button"
            onClick={generateAnalysis}
            disabled={loading || running || providersUnavailable}
          >
            {loading || running ? <Loader2 className="spin" size={15} /> : <Sparkles size={15} />}
            {running ? "分析處理中" : hasAnyAnalysis ? "更新全部分析" : "產生全部分析"}
          </button>
        </div>
        {cooldowns.length > 0 && (
          <div className="ai-provider-health warning">
            <AlertCircle size={15} />
            <span>
              {cooldowns.map((item) => `${item.provider} ${Math.floor(item.remaining / 60)}:${String(item.remaining % 60).padStart(2, "0")}`).join(" · ")}
              {availableProviders.length ? " · 將自動切換可用 provider" : " · 冷卻期間已停用重複請求"}
            </span>
          </div>
        )}
        {providerHealth.length > 0 && configuredProviders.length === 0 && (
          <div className="ai-provider-health warning">
            <AlertCircle size={15} />
            <span>尚未設定可用的免費 AI provider，目前使用本機規則分析。</span>
          </div>
        )}
        {(error || modeError) && (
          <div className="ai-analysis-error">
            <AlertCircle size={15} />
            {usingRuleSummary ? `AI 暫時不可用，已先顯示規則摘要。${modeError || error ? ` ${modeError || error}` : ""}` : modeError || error}
          </div>
        )}
        {(loading || running) && !analysis ? (
          <div className="ai-analysis-empty">
            <Loader2 className="spin" size={15} />
            未持有與持有中分析處理中
          </div>
        ) : analysis ? (
          <>
            {result && <DataQualityBadge quality={stock.data_quality_summary?.categories?.AI_ANALYSIS} />}
            <div className="ai-analysis-source rule">規則判斷</div>
            <div className="ai-status-row">
              <span>{activeMode === "HELD" ? "持有判斷" : "進場判斷"}</span>
              <strong>{ruleResult?.analysis?.overall_status || analysis.overall_status}</strong>
            </div>
            <div className={`ai-analysis-source ${usingRuleSummary ? "rule" : "ai"}`}>
              {usingRuleSummary
                ? running
                  ? "AI 解讀處理中，暫以規則說明"
                  : "規則解讀"
                : "AI 解讀"}
            </div>
            <p>{aiText(analysis.summary)}</p>
            <div className="ai-analysis-lists">
              <AIAnalysisList title="正面因素" items={analysis.positive_points} />
              <AIAnalysisList title="風險因素" items={analysis.risk_points} />
              <AIAnalysisList title="後續觀察" items={analysis.watch_points} />
            </div>
            <div className="ai-feedback-row" aria-label="AI 分析回饋">
              <button
                type="button"
                className="text-button feedback-button"
                disabled={!result?.id || feedbackSubmitting}
                onClick={() => submitFeedback("useful", [])}
              >
                {feedbackSubmitting ? <Loader2 className="spin" size={14} /> : <CheckCircle2 size={14} />}
                有幫助
              </button>
              {AI_FEEDBACK_TAGS.map((item) => (
                <button
                  key={item.tag}
                  type="button"
                  className="text-button feedback-button"
                  disabled={!result?.id || feedbackSubmitting}
                  onClick={() => submitFeedback("not_useful", [item.tag])}
                >
                  {item.label}
                </button>
              ))}
            </div>
            {feedbackStatus && <small>{feedbackStatus}</small>}
            {result ? (
              <small>
                {result.provider} · {result.model}
                {result.cached ? " · 使用快取" : " · 新產生"}
                {" · AI 解讀完成時間 "}
                {formatDate(result.analysis_requested_at || result.generated_at)}
              </small>
            ) : (
              <small>rule_based · 本機規則摘要 · 分析時間 {formatDate(ruleResult?.generated_at)}</small>
            )}
            {analysisResponse?.run && (
              <small>
                請求方式 {analysisResponse.run.request_strategy}
                {analysisResponse.run.finished_at && ["success", "partial"].includes(analysisResponse.run.status)
                  ? ` · 分析完成 ${formatDate(analysisResponse.run.finished_at)}`
                  : analysisResponse.run.finished_at && analysisResponse.run.status === "failed"
                    ? ` · 最近嘗試失敗 ${formatDate(analysisResponse.run.finished_at)}`
                    : ""}
              </small>
            )}
            {analysisResponse?.data_as_of?.length > 0 && (
              <div className="ai-data-as-of">
                <strong>AI 使用資料截至</strong>
                <div>
                  {analysisResponse.data_as_of
                    .filter((item) => item.freshness_status !== "NOT_APPLICABLE")
                    .map((item) => (
                      <span key={item.category} className={item.freshness_status.toLowerCase()}>
                        <b>{item.label}</b>
                        {item.data_period || item.data_date || (item.fetched_at ? formatDate(item.fetched_at) : "待更新")}
                        <em>{FRESHNESS_LABELS[item.freshness_status] || item.freshness_status}{item.is_cached ? " · 快取" : ""}</em>
                      </span>
                    ))}
                </div>
              </div>
            )}
            {analysisResponse?.stale_items?.length > 0 && (
              <div className="ai-stale-items">
                <strong>資料限制</strong>
                <ul>
                  {analysisResponse.stale_items.map((item) => <li key={item}>{item}</li>)}
                </ul>
              </div>
            )}
            <small>{analysis.disclaimer}</small>
          </>
        ) : (
          <div className="ai-analysis-empty">
            {activeMode === "HELD"
              ? "持有分析會使用成交均價、每股／百分比損益與公開指標；唯一禁止傳送的是持有股數。"
              : "未持有分析只使用行情、估值、基本面、技術面與籌碼摘要。"}
          </div>
        )}
      </div>
    </section>,
    document.body,
  );
}

function AIAnalysisList({ title, items }) {
  const displayItems = items?.length ? items : ["暫無明確訊號"];
  return (
    <div>
      <strong>{title}</strong>
      <ul>
        {displayItems.map((item, index) => (
          <li key={`${title}-${aiItemKey(item, index)}`}>{aiText(item)}</li>
        ))}
      </ul>
    </div>
  );
}
