import React, { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";

import { API_BASE_URL } from "../../api/client";
import { DataQualityBadge } from "../shared/DataQualityBadge";
import {
  formatDate,
  formatFundamentalMetric,
  formatOptionalChartNumber,
  formatOptionalSignedPercent,
  fundamentalToneClass,
  trendDisplayValue,
  trendNumericValue,
} from "../../utils/formatters";


const FUNDAMENTAL_CATEGORY_STORAGE_PREFIX = "stock-dashboard-fundamental-category";
const FUNDAMENTAL_CATEGORY_KEYS = ["eps", "monthly_revenue", "gross_margin", "operating_margin", "net_margin"];
const FUNDAMENTAL_CATEGORY_LABELS = {
  eps: "EPS",
  monthly_revenue: "月營收",
  gross_margin: "毛利率",
  operating_margin: "營益率",
  net_margin: "淨利率",
};


function loadFundamentalCategory(symbol) {
  try {
    const stored = window.localStorage.getItem(`${FUNDAMENTAL_CATEGORY_STORAGE_PREFIX}:${symbol}`);
    return FUNDAMENTAL_CATEGORY_KEYS.includes(stored) ? stored : "eps";
  } catch {
    return "eps";
  }
}

function storeFundamentalCategory(symbol, categoryKey) {
  try {
    window.localStorage.setItem(`${FUNDAMENTAL_CATEGORY_STORAGE_PREFIX}:${symbol}`, categoryKey);
  } catch {
    // Storage is optional; category switching still works for the current render.
  }
}

export function FundamentalDisclosure({ symbol, fundamental, quality, expanded, onToggle }) {
  const [activeCategory, setActiveCategory] = useState(() => loadFundamentalCategory(symbol));
  const [trends, setTrends] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setActiveCategory(loadFundamentalCategory(symbol));
  }, [symbol]);

  useEffect(() => {
    if (!expanded || trends?.symbol === symbol) {
      return undefined;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    fetch(`${API_BASE_URL}/api/stocks/${symbol}/fundamentals/trends`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        return response.json();
      })
      .then((payload) => {
        setTrends(payload);
        const availableKeys = payload.categories?.map((category) => category.key) || [];
        setActiveCategory((current) => (availableKeys.includes(current) ? current : "eps"));
      })
      .catch((fetchError) => {
        if (fetchError.name !== "AbortError") {
          setError("基本面趨勢資料讀取失敗");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [expanded, symbol, trends?.symbol]);

  const categories = trends?.categories || [];
  const activeTrend = categories.find((category) => category.key === activeCategory) || categories[0] || null;
  const toggleDate = activeTrend?.fetched_at || fundamental?.fetched_at;

  const selectCategory = (categoryKey) => {
    setActiveCategory(categoryKey);
    storeFundamentalCategory(symbol, categoryKey);
  };

  return (
    <div className="fundamental-disclosure">
      <button className="fundamental-toggle" type="button" onClick={onToggle} aria-expanded={expanded}>
        <span>
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          基本面
        </span>
        <span className="disclosure-meta"><small>{toggleDate ? formatDate(toggleDate) : "待更新"}</small><DataQualityBadge quality={quality} compact /></span>
      </button>
      {expanded && (
        <div className="fundamental-panel">
          <div className="fundamental-tabs" role="tablist" aria-label={`${symbol} 基本面分類`}>
            {FUNDAMENTAL_CATEGORY_KEYS.map((categoryKey) => (
              <button
                key={categoryKey}
                type="button"
                className={activeCategory === categoryKey ? "active" : ""}
                onClick={() => selectCategory(categoryKey)}
                role="tab"
                aria-selected={activeCategory === categoryKey}
              >
                {FUNDAMENTAL_CATEGORY_LABELS[categoryKey]}
              </button>
            ))}
          </div>
          {loading ? (
            <div className="fundamental-loading">
              <Loader2 size={16} />
              讀取基本面趨勢
            </div>
          ) : error ? (
            <div className="valuation-empty">{error}</div>
          ) : activeTrend ? (
            <>
              <div className="fundamental-summary-grid">
                {activeTrend.summary.map((item) => (
                  <div className="fundamental-summary-card" key={item.key}>
                    <span>{item.label}</span>
                    <strong className={fundamentalToneClass(item.value, item.value_type)}>
                      {formatFundamentalMetric(item.value, item.value_type, activeTrend.key)}
                    </strong>
                  </div>
                ))}
              </div>
              <FundamentalTrendChart category={activeTrend} />
              <small className="fundamental-source">{activeTrend.source || fundamental?.source || "FinMind fundamental cache"}</small>
            </>
          ) : (
            <div className="valuation-empty">基本面快取建立中</div>
          )}
        </div>
      )}
    </div>
  );
}

function FundamentalTrendChart({ category }) {
  const [hoverIndex, setHoverIndex] = useState(null);
  const chartWidth = 760;
  const chartHeight = 280;
  const margin = { top: 24, right: 26, bottom: 44, left: 52 };
  const innerWidth = chartWidth - margin.left - margin.right;
  const innerHeight = chartHeight - margin.top - margin.bottom;
  const chartPoints = category.points || [];
  const plotted = chartPoints.map((point, index) => ({
    ...point,
    index,
    numericValue: trendNumericValue(point.value, category.key),
  }));
  const validPoints = plotted.filter((point) => point.numericValue !== null && Number.isFinite(point.numericValue));

  if (!validPoints.length) {
    return <div className="fundamental-chart-empty">趨勢資料待更新</div>;
  }

  const values = validPoints.map((point) => point.numericValue);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const padding = rawMin === rawMax ? Math.max(1, Math.abs(rawMin) * 0.08) : (rawMax - rawMin) * 0.12;
  const yMin = rawMin - padding;
  const yMax = rawMax + padding;
  const xScale = (index) => margin.left + (chartPoints.length <= 1 ? innerWidth / 2 : (index / (chartPoints.length - 1)) * innerWidth);
  const yScale = (value) => margin.top + ((yMax - value) / (yMax - yMin)) * innerHeight;
  const path = validPoints
    .map((point, sequenceIndex) => `${sequenceIndex === 0 ? "M" : "L"} ${xScale(point.index)} ${yScale(point.numericValue)}`)
    .join(" ");
  const hoveredPoint = hoverIndex !== null ? plotted[hoverIndex] : validPoints[validPoints.length - 1];
  const hoveredX = hoveredPoint ? xScale(hoveredPoint.index) : null;
  const hoveredY = hoveredPoint?.numericValue !== null ? yScale(hoveredPoint.numericValue) : margin.top;
  const labelStep = chartPoints.length > 9 ? 2 : 1;

  const updateHover = (event) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const relativeX = ((event.clientX - bounds.left) / bounds.width) * chartWidth;
    const nearest = plotted.reduce((best, point) => {
      const distance = Math.abs(xScale(point.index) - relativeX);
      return !best || distance < best.distance ? { index: point.index, distance } : best;
    }, null);
    if (nearest) {
      setHoverIndex(nearest.index);
    }
  };

  return (
    <div className="fundamental-chart-card">
      <svg
        className="fundamental-trend-chart"
        viewBox={`0 0 ${chartWidth} ${chartHeight}`}
        role="img"
        aria-label={`${category.label} 過去一年趨勢`}
        onPointerMove={updateHover}
        onPointerDown={updateHover}
        onPointerLeave={() => setHoverIndex(null)}
      >
        {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
          const y = margin.top + ratio * innerHeight;
          return (
            <g key={`grid-y-${ratio}`}>
              <line className="fundamental-grid-line" x1={margin.left} x2={chartWidth - margin.right} y1={y} y2={y} />
            </g>
          );
        })}
        {chartPoints.map((point, index) => {
          if (index % labelStep !== 0 && index !== chartPoints.length - 1) {
            return null;
          }
          const x = xScale(index);
          return (
            <g key={`grid-x-${point.period}`}>
              <line className="fundamental-grid-line soft" x1={x} x2={x} y1={margin.top} y2={chartHeight - margin.bottom} />
              <text className="fundamental-axis-label" x={x} y={chartHeight - 12} textAnchor="middle">
                {point.period}
              </text>
            </g>
          );
        })}
        <text className="fundamental-axis-label" x={margin.left - 10} y={margin.top + 4} textAnchor="end">
          {formatOptionalChartNumber(yMax)}
        </text>
        <text className="fundamental-axis-label" x={margin.left - 10} y={chartHeight - margin.bottom} textAnchor="end">
          {formatOptionalChartNumber(yMin)}
        </text>
        <path className="fundamental-line" d={path} fill="none" />
        {validPoints.map((point) => (
          <circle
            key={`point-${point.period}`}
            className={`fundamental-point${hoverIndex === point.index ? " active" : ""}`}
            cx={xScale(point.index)}
            cy={yScale(point.numericValue)}
            r={hoverIndex === point.index ? 6 : 5}
          />
        ))}
        {hoveredPoint && hoveredPoint.numericValue !== null && (
          <>
            <line className="fundamental-crosshair" x1={hoveredX} x2={hoveredX} y1={margin.top} y2={chartHeight - margin.bottom} />
            <line className="fundamental-crosshair" x1={margin.left} x2={chartWidth - margin.right} y1={hoveredY} y2={hoveredY} />
          </>
        )}
      </svg>
      {hoveredPoint && (
        <div className="fundamental-tooltip">
          <strong>{hoveredPoint.period}</strong>
          <span>
            {category.label} <b>{trendDisplayValue(hoveredPoint.value, category.key)}</b>
          </span>
          {category.key === "eps" && (
            <>
              <span>單季 EPS YoY {formatOptionalSignedPercent(hoveredPoint.yoy_percent)}</span>
              <span>TTM EPS YoY {formatOptionalSignedPercent(hoveredPoint.ttm_eps_yoy_percent)}</span>
            </>
          )}
          {category.key === "monthly_revenue" && (
            <>
              <span>YoY {formatOptionalSignedPercent(hoveredPoint.yoy_percent)}</span>
              <span>MoM {formatOptionalSignedPercent(hoveredPoint.mom_percent)}</span>
            </>
          )}
          {category.key !== "eps" && category.key !== "monthly_revenue" && (
            <span>SoS {formatOptionalSignedPercent(hoveredPoint.sos_percent)}</span>
          )}
        </div>
      )}
    </div>
  );
}
