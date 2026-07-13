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
            disabled={loading || running}
          >
            {loading || running ? <Loader2 className="spin" size={15} /> : <Sparkles size={15} />}
            {running ? "分析處理中" : hasAnyAnalysis ? "更新全部分析" : "產生全部分析"}
          </button>
        </div>
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
            <div className={`ai-analysis-source ${usingRuleSummary ? "rule" : "ai"}`}>
              {usingRuleSummary
                ? running
                  ? "AI 分析處理中，先顯示規則摘要"
                  : "目前顯示規則摘要"
                : "AI 分析結果"}
            </div>
            <DataQualityBadge quality={stock.data_quality_summary?.categories?.AI_ANALYSIS} />
            <div className="ai-status-row">
              <span>{activeMode === "HELD" ? "持有判斷" : "進場判斷"}</span>
              <strong>{analysis.overall_status}</strong>
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
                {" · 分析時間 "}
                {formatDate(result.analysis_requested_at || result.generated_at)}
              </small>
            ) : (
              <small>rule_based · 本機規則摘要 · 分析時間 {formatDate(ruleResult?.generated_at)}</small>
            )}
            <small>{analysis.disclaimer}</small>
          </>
        ) : (
          <div className="ai-analysis-empty">
            {activeMode === "HELD"
              ? "持有分析只使用成交均價與每股／百分比損益，不傳股數、總成本或資產資料。"
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
