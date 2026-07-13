import React, { useCallback, useRef, useState } from "react";
import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  Clock3,
  Database,
  GripVertical,
  Loader2,
  RefreshCcw,
  Trash2,
  Wifi,
} from "lucide-react";

import { AIAnalysisPopover } from "../ai/AIAnalysisPanel";
import { BrokerTradingDisclosure } from "../broker/BrokerPanel";
import { FundamentalDisclosure } from "../fundamentals/FundamentalPanel";
import { DataQualityPopover } from "../quality/DataQualityPanel";
import { DataQualityBadge } from "../shared/DataQualityBadge";
import { TechnicalAnalysisDisclosure } from "../technical/TechnicalPanel";
import { PositionEditor } from "./PositionEditor";
import {
  comparisonPercent,
  comparisonToneClass,
  formatNumber,
  formatOptionalNumber,
  formatOptionalPe,
  formatOptionalSignedPercent,
  formatPeRange,
  percentageToneClass,
} from "../../utils/formatters";
import { isPendingRefresh } from "../../utils/stocks";


const EPS_LABELS = {
  TTM: "近四季",
  LAST_YEAR: "去年全年",
};

const REFRESH_STATUS_LABELS = {
  idle: "待命",
  queued: "已排入",
  running: "更新中",
  refreshing: "更新中",
  success: "已更新",
  failed: "更新失敗，使用快取",
  retry_wait: "等待重試",
};


export function SortableStockCard({
  stock,
  index,
  total,
  refreshState,
  showRefreshState,
  sortingDisabled,
  actionDisabled,
  onRegisterRef,
  onMoveUp,
  onMoveDown,
  onRefresh,
  onDelete,
  onSavePosition,
  onClearPosition,
  aiAnalysisPending,
  onAiAnalysisPendingChange,
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: stock.symbol,
    disabled: sortingDisabled,
  });
  const setCombinedRef = useCallback(
    (node) => {
      setNodeRef(node);
      onRegisterRef(stock.symbol, node);
    },
    [onRegisterRef, setNodeRef, stock.symbol],
  );

  return (
    <StockCard
      ref={setCombinedRef}
      style={{
        transform: CSS.Transform.toString(transform),
        transition,
      }}
      stock={stock}
      refreshState={refreshState}
      showRefreshState={showRefreshState}
      dragging={isDragging}
      sortingDisabled={sortingDisabled}
      actionDisabled={actionDisabled}
      aiAnalysisPending={aiAnalysisPending}
      onAiAnalysisPendingChange={onAiAnalysisPendingChange}
      canMoveUp={index > 0}
      canMoveDown={index < total - 1}
      dragAttributes={attributes}
      dragListeners={listeners}
      onMoveUp={onMoveUp}
      onMoveDown={onMoveDown}
      onRefresh={onRefresh}
      onDelete={onDelete}
      onSavePosition={onSavePosition}
      onClearPosition={onClearPosition}
    />
  );
}

export const StockCard = React.forwardRef(function StockCard(
  {
    stock,
    refreshState,
    showRefreshState,
    dragging = false,
    overlay = false,
    sortingDisabled = false,
    actionDisabled = false,
    canMoveUp = false,
    canMoveDown = false,
    dragAttributes = {},
    dragListeners = {},
    onMoveUp,
    onMoveDown,
    onRefresh,
    onDelete,
    onSavePosition,
    onClearPosition,
    aiAnalysisPending = false,
    onAiAnalysisPendingChange = () => {},
    style,
  },
  ref,
) {
  const metric = stock.metric;
  const pendingRefresh = isPendingRefresh(refreshState);
  const statusLabel = REFRESH_STATUS_LABELS[refreshState?.status] || refreshState?.status;
  const isEtf = stock.asset_type === "ETF";
  const peNotApplicable = !isEtf && metric && (metric.current_pe === null || metric.current_pe === undefined);
  const [fundamentalExpanded, setFundamentalExpanded] = useState(false);
  const [brokerTradingExpanded, setBrokerTradingExpanded] = useState(false);
  const [technicalExpanded, setTechnicalExpanded] = useState(false);
  const [aiAnalysisOpen, setAiAnalysisOpen] = useState(false);
  const [dataQualityOpen, setDataQualityOpen] = useState(false);
  const aiButtonRef = useRef(null);
  const dataQualityButtonRef = useRef(null);
  const qualitySummary = stock.data_quality_summary;

  return (
    <article
      ref={ref}
      style={style}
      data-symbol={stock.symbol}
      className={`stock-card${dragging ? " dragging" : ""}${overlay ? " overlay" : ""}`}
    >
      <header className="stock-header">
        <div className="stock-heading">
          <button
            className="icon-button small drag-handle"
            type="button"
            title="拖曳排序"
            aria-label="拖曳排序"
            disabled={sortingDisabled}
            {...dragAttributes}
            {...dragListeners}
          >
            <GripVertical size={16} />
          </button>
          <div>
            <div className="stock-title">
              <strong>{stock.symbol}</strong>
              <span>{stock.name}</span>
              {isEtf && <span className="asset-pill">ETF</span>}
              {showRefreshState && (
                <span className={`status-pill ${refreshState.status}`}>
                  {refreshState.status === "running" || refreshState.status === "refreshing" ? <Loader2 size={13} /> : null}
                  {refreshState.status === "queued" || refreshState.status === "retry_wait" ? <Clock3 size={13} /> : null}
                  {refreshState.status === "success" ? <CheckCircle2 size={13} /> : null}
                  {refreshState.status === "failed" ? <AlertCircle size={13} /> : null}
                  {statusLabel}
                </span>
              )}
            </div>
            <div className="stock-meta">
              <span>{stock.market}</span>
              <span>{stock.currency}</span>
              {!metric && <span>快取建立中</span>}
            </div>
          </div>
        </div>
        <div className="stock-actions">
          <button
            ref={dataQualityButtonRef}
            className={`icon-button small data-quality-button ${String(qualitySummary?.overall_status || "CRITICAL").toLowerCase()}`}
            type="button"
            onClick={() => setDataQualityOpen((current) => !current)}
            title="資料可信度"
            aria-label="資料可信度"
            aria-expanded={dataQualityOpen}
            disabled={overlay}
          >
            <Database size={16} />
            {qualitySummary?.issue_count > 0 && <span>{qualitySummary.issue_count}</span>}
          </button>
          <button
            ref={aiButtonRef}
            className="icon-button small ai-icon-button"
            type="button"
            onClick={() => setAiAnalysisOpen((current) => !current)}
            title="AI 分析"
            aria-label="AI 分析"
            aria-expanded={aiAnalysisOpen}
            disabled={overlay}
          >
            <span className="ai-icon-mark" aria-hidden="true" />
          </button>
          <button
            className="icon-button small"
            type="button"
            onClick={onMoveUp}
            title="上移此標的"
            aria-label="上移此標的"
            disabled={actionDisabled || pendingRefresh || !canMoveUp}
          >
            <ArrowUp size={16} />
          </button>
          <button
            className="icon-button small"
            type="button"
            onClick={onMoveDown}
            title="下移此標的"
            aria-label="下移此標的"
            disabled={actionDisabled || pendingRefresh || !canMoveDown}
          >
            <ArrowDown size={16} />
          </button>
          <button
            className="icon-button small"
            type="button"
            onClick={onRefresh}
            title="排入此標的更新"
            aria-label="排入此標的更新"
            disabled={actionDisabled || pendingRefresh}
          >
            <RefreshCcw size={16} />
          </button>
          <button
            className="icon-button small danger"
            type="button"
            onClick={onDelete}
            title="刪除此標的"
            aria-label="刪除此標的"
            disabled={actionDisabled || pendingRefresh}
          >
            <Trash2 size={16} />
          </button>
        </div>
      </header>

      <div className="stock-metrics">
        <div className="metric-tile quote-grid-tile">
          <div className="quote-current-row">
            <span className="metric-label">現價</span>
            <strong>{formatOptionalNumber(metric?.current_price)}</strong>
          </div>
          <div className="quote-comparison-grid">
            <QuoteComparison label="開盤" value={metric?.open_price} currentPrice={metric?.current_price} />
            <QuoteComparison label="昨收" value={metric?.previous_close} currentPrice={metric?.current_price} />
            <QuoteComparison label="最高" value={metric?.day_high} currentPrice={metric?.current_price} />
            <QuoteComparison label="最低" value={metric?.day_low} currentPrice={metric?.current_price} />
          </div>
          <DataQualityBadge quality={qualitySummary?.categories?.QUOTE} compact />
        </div>
        {!isEtf && (
          <div className="metric-tile pe-tile">
            <div className="quote-current-row">
              <span className="metric-label">目前PE</span>
              <strong>{metric ? formatOptionalPe(metric.current_pe) : "待更新"}</strong>
            </div>
            <div className="quote-comparison-grid pe-history-grid">
              <div className="quote-comparison-item">
                <span className="metric-label">平均</span>
                <div className="quote-comparison-value">
                  <strong>{formatOptionalNumber(metric?.pe_average_3y)}</strong>
                  <span className={comparisonToneClass(metric?.pe_vs_average_percent)}>
                    {formatOptionalSignedPercent(metric?.pe_vs_average_percent)}
                  </span>
                </div>
              </div>
              <div className="quote-comparison-item">
                <span className="metric-label">區間</span>
                <strong>{formatPeRange(metric?.pe_min_3y, metric?.pe_max_3y)}</strong>
              </div>
            </div>
            <DataQualityBadge quality={qualitySummary?.categories?.PE} compact />
          </div>
        )}
        <div className="metric-tile profit-tile">
          <div className="profit-section">
            <span className="metric-label">純損益</span>
            <strong className={percentageToneClass(stock.position?.unrealized_profit_loss_percent)}>
              {formatOptionalSignedPercent(stock.position?.unrealized_profit_loss_percent)}
            </strong>
          </div>
          <div className="profit-section">
            <span className="metric-label">費後損益估算</span>
            <strong className={percentageToneClass(stock.position?.fee_adjusted_profit_loss_percent)}>
              {formatOptionalSignedPercent(stock.position?.fee_adjusted_profit_loss_percent)}
            </strong>
          </div>
          <PositionEditor
            stock={stock}
            disabled={actionDisabled || pendingRefresh || overlay}
            onSavePosition={onSavePosition}
            onClearPosition={onClearPosition}
            compact
          />
        </div>
      </div>

      {!isEtf && (
        <div className="valuation-table">
          <div className="valuation-row head">
            <span></span>
            <span>EPS</span>
            <span>預期股價</span>
            <span>預期損益</span>
            <span>預期成本損益</span>
          </div>
          {stock.valuations.length ? (
            stock.valuations.map((valuation) => (
              <div className="valuation-row" key={`${stock.symbol}-${valuation.eps_type}`}>
                <span>
                  <strong>{EPS_LABELS[valuation.eps_type] || valuation.eps_type}</strong>
                  <small>{valuation.eps_period}</small>
                </span>
                <span className="constant-value">{formatNumber(valuation.eps_value)}</span>
                <span className="constant-value">{formatNumber(valuation.estimated_price)}</span>
                <span className={percentageToneClass(valuation.difference_percent)}>
                  <strong>{formatOptionalSignedPercent(valuation.difference_percent)}</strong>
                </span>
                <span className={percentageToneClass(valuation.cost_difference_percent)}>
                  <strong>{formatOptionalSignedPercent(valuation.cost_difference_percent)}</strong>
                </span>
              </div>
            ))
          ) : peNotApplicable ? (
            <div className="valuation-empty">
              <Wifi size={15} />
              PE 不適用，無法建立 EPS × PE 估值
            </div>
          ) : (
            <div className="valuation-empty">
              <Wifi size={15} />
              背景快取建立中
            </div>
          )}
        </div>
      )}

      <div className="stock-disclosures">
        {!isEtf && (
          <FundamentalDisclosure
            symbol={stock.symbol}
            fundamental={stock.fundamental}
            quality={qualitySummary?.categories?.FUNDAMENTAL}
            expanded={fundamentalExpanded}
            onToggle={() => setFundamentalExpanded((current) => !current)}
          />
        )}
        <BrokerTradingDisclosure
          brokerTrading={stock.broker_trading}
          quality={qualitySummary?.categories?.BROKER_TRADING}
          expanded={brokerTradingExpanded}
          onToggle={() => setBrokerTradingExpanded((current) => !current)}
        />
        <TechnicalAnalysisDisclosure
          symbol={stock.symbol}
          metricUpdatedAt={metric?.price_updated_at}
          quality={qualitySummary?.categories?.TECHNICAL_DAILY}
          expanded={technicalExpanded}
          onToggle={() => setTechnicalExpanded((current) => !current)}
        />
      </div>
      <DataQualityPopover
        stock={stock}
        open={dataQualityOpen}
        anchorRef={dataQualityButtonRef}
        onClose={() => setDataQualityOpen(false)}
      />
      <AIAnalysisPopover
        stock={stock}
        open={aiAnalysisOpen}
        anchorRef={aiButtonRef}
        onClose={() => setAiAnalysisOpen(false)}
        analysisPending={aiAnalysisPending}
        onAnalysisPendingChange={onAiAnalysisPendingChange}
      />
    </article>
  );
});

function QuoteComparison({ label, value, currentPrice }) {
  const percent = comparisonPercent(currentPrice, value);
  return (
    <div className="quote-comparison-item">
      <span className="metric-label">{label}</span>
      <div className="quote-comparison-value">
        <strong>{formatOptionalNumber(value)}</strong>
        <span className={comparisonToneClass(percent)}>{formatOptionalSignedPercent(percent)}</span>
      </div>
    </div>
  );
}
