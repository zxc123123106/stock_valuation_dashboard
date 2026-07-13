import React, { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, ChevronDown, ChevronRight, Loader2, Wifi } from "lucide-react";
import { CandlestickSeries, CrosshairMode, LineSeries, createChart } from "lightweight-charts";

import { API_BASE_URL, parseError } from "../../api/client";
import { DataQualityBadge } from "../shared/DataQualityBadge";
import {
  formatDate,
  formatOptionalChartNumber,
  formatOptionalPercent,
  formatTradingDate,
} from "../../utils/formatters";


const MA_PERIODS = [5, 10, 20, 60, 120, 240];
const MA_VISIBILITY_STORAGE_KEY = "stock-dashboard-visible-ma-lines";
const MA_LINE_COLORS = {
  5: "#f08f7f",
  10: "#e2c879",
  20: "#7fd8ff",
  60: "#b99cff",
  120: "#8be0b2",
  240: "#f2a7d8",
};


function defaultMaVisibility() {
  return Object.fromEntries(MA_PERIODS.map((period) => [period, false]));
}


function loadMaVisibility() {
  const fallback = defaultMaVisibility();
  try {
    const stored = window.localStorage.getItem(MA_VISIBILITY_STORAGE_KEY);
    if (!stored) return fallback;
    const parsed = JSON.parse(stored);
    return Object.fromEntries(MA_PERIODS.map((period) => [period, Boolean(parsed?.[period])]));
  } catch {
    return fallback;
  }
}


function storeMaVisibility(visibility) {
  try {
    window.localStorage.setItem(MA_VISIBILITY_STORAGE_KEY, JSON.stringify(visibility));
  } catch {
    // Storage is optional.
  }
}


export function TechnicalAnalysisDisclosure({ symbol, metricUpdatedAt, quality, expanded, onToggle }) {
  const [analysis, setAnalysis] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!expanded) {
      return undefined;
    }

    const controller = new AbortController();
    async function loadTechnicalAnalysis() {
      setLoading(true);
      setError("");
      try {
        const response = await fetch(`${API_BASE_URL}/api/stocks/${symbol}/technical-analysis?limit=120`, {
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(await parseError(response));
        }
        setAnalysis(await response.json());
      } catch (requestError) {
        if (requestError.name !== "AbortError") {
          setError(requestError.message);
        }
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      }
    }

    loadTechnicalAnalysis();
    return () => controller.abort();
  }, [expanded, metricUpdatedAt, symbol]);

  return (
    <div className="technical-analysis">
      <button className="technical-toggle" type="button" onClick={onToggle} aria-expanded={expanded}>
        <span>
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          技術分析
        </span>
        <span className="disclosure-meta"><small>日線 · MA</small><DataQualityBadge quality={quality} compact /></span>
      </button>
      {expanded && (
        <div className="technical-panel">
          {loading && !analysis ? (
            <div className="valuation-empty">
              <Loader2 className="spin" size={16} />
              日線載入中
            </div>
          ) : error && !analysis ? (
            <div className="valuation-empty technical-error">
              <AlertCircle size={16} />
              {error}
            </div>
          ) : analysis?.candles?.length ? (
            <>
              <DailyCandlestickChart candles={analysis.candles} />
              <div className="technical-source">
                <span>{analysis.source}</span>
                <span>{analysis.fetched_at ? formatDate(analysis.fetched_at) : "待更新"}</span>
              </div>
            </>
          ) : (
            <div className="valuation-empty">
              <Wifi size={15} />
              日線快取待更新
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DailyCandlestickChart({ candles }) {
  const containerRef = useRef(null);
  const latestCandle = candles[candles.length - 1] || null;
  const [selectedCandle, setSelectedCandle] = useState(latestCandle);
  const [visibleMaLines, setVisibleMaLines] = useState(() => loadMaVisibility());

  const toggleMaLine = useCallback((period) => {
    setVisibleMaLines((current) => {
      const next = { ...current, [period]: !current[period] };
      storeMaVisibility(next);
      return next;
    });
  }, []);

  useEffect(() => {
    setSelectedCandle(latestCandle);
  }, [latestCandle]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !candles.length) {
      return undefined;
    }

    const candleByDate = new Map(candles.map((candle) => [candle.date, candle]));
    const chart = createChart(container, {
      autoSize: true,
      layout: {
        background: { color: "#111110" },
        textColor: "#9f988d",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "rgba(226, 200, 121, 0.07)" },
        horzLines: { color: "rgba(226, 200, 121, 0.07)" },
      },
      crosshair: {
        mode: CrosshairMode.Magnet,
        vertLine: { color: "rgba(226, 200, 121, 0.58)", labelBackgroundColor: "#755f27" },
        horzLine: { color: "rgba(226, 200, 121, 0.58)", labelBackgroundColor: "#755f27" },
      },
      rightPriceScale: { borderColor: "rgba(226, 200, 121, 0.18)" },
      timeScale: {
        borderColor: "rgba(226, 200, 121, 0.18)",
        timeVisible: false,
        rightOffset: 0,
        fixLeftEdge: true,
        fixRightEdge: true,
        lockVisibleTimeRangeOnResize: true,
      },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#ef8c7f",
      downColor: "#57d3a0",
      borderVisible: false,
      wickUpColor: "#ef8c7f",
      wickDownColor: "#57d3a0",
      priceLineVisible: false,
    });
    candleSeries.setData(candles.map((candle) => ({
      time: candle.date,
      open: candle.open,
      high: candle.high,
      low: candle.low,
      close: candle.close,
    })));

    MA_PERIODS.filter((period) => visibleMaLines[period]).forEach((period) => {
      const key = `ma${period}`;
      const maSeries = chart.addSeries(LineSeries, {
        color: MA_LINE_COLORS[period],
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true,
        crosshairMarkerVisible: false,
      });
      maSeries.setData(
        candles
          .filter((candle) => candle[key] !== null && candle[key] !== undefined)
          .map((candle) => ({ time: candle.date, value: candle[key] })),
      );
    });

    function handleCrosshairMove(param) {
      if (!param.time) {
        setSelectedCandle(latestCandle);
        return;
      }
      const dateKey = typeof param.time === "string"
        ? param.time
        : `${param.time.year}-${String(param.time.month).padStart(2, "0")}-${String(param.time.day).padStart(2, "0")}`;
      setSelectedCandle(candleByDate.get(dateKey) || latestCandle);
    }

    const timeScale = chart.timeScale();
    const fullLogicalRange = { from: -0.5, to: candles.length - 0.5 };
    let restoringFullRange = false;
    let resizeFrame = 0;

    function syncMinimumBarSpacing() {
      const plotWidth = Math.max(1, timeScale.width());
      chart.applyOptions({
        timeScale: {
          minBarSpacing: Math.max(0.5, plotWidth / candles.length),
        },
      });
    }

    function clampVisibleRange(range) {
      if (!range || restoringFullRange || range.to - range.from <= candles.length + 0.01) {
        return;
      }
      restoringFullRange = true;
      timeScale.setVisibleLogicalRange(fullLogicalRange);
      window.requestAnimationFrame(() => {
        restoringFullRange = false;
      });
    }

    const resizeObserver = new ResizeObserver(() => {
      window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(syncMinimumBarSpacing);
    });

    chart.subscribeCrosshairMove(handleCrosshairMove);
    timeScale.subscribeVisibleLogicalRangeChange(clampVisibleRange);
    resizeObserver.observe(container);
    syncMinimumBarSpacing();
    timeScale.setVisibleLogicalRange(fullLogicalRange);
    return () => {
      window.cancelAnimationFrame(resizeFrame);
      resizeObserver.disconnect();
      timeScale.unsubscribeVisibleLogicalRangeChange(clampVisibleRange);
      chart.unsubscribeCrosshairMove(handleCrosshairMove);
      chart.remove();
    };
  }, [candles, latestCandle, visibleMaLines]);

  const summary = selectedCandle || latestCandle;
  return (
    <div className="technical-chart-shell">
      <div className="technical-summary">
        <div className="technical-summary-primary">
          <div className="technical-summary-date">
            <span>日期</span>
            <strong>{formatTradingDate(summary?.date)}</strong>
            {summary?.is_provisional && <em>暫定 K 棒</em>}
          </div>
          <TechnicalSummaryValue label="收盤" value={summary?.close} accent />
        </div>
        <div className="technical-summary-ma">
          {MA_PERIODS.map((period) => (
            <TechnicalSummaryValue
              key={period}
              label={`MA${period}`}
              value={summary?.[`ma${period}`]}
              accent
            />
          ))}
        </div>
        <div className="technical-summary-volume">
          <TechnicalSummaryValue label="今日成交量" value={summary?.volume} accent digits={0} suffix=" 張" />
          <TechnicalSummaryValue label="5 日均量" value={summary?.volume_ma5} accent digits={0} suffix=" 張" />
          <TechnicalSummaryValue label="20 日均量" value={summary?.volume_ma20} accent digits={0} suffix=" 張" />
          <TechnicalSummaryValue
            label="今日量 / 20 日均量"
            value={summary?.volume_vs_ma20_percent}
            accent
            formatter={formatOptionalPercent}
          />
        </div>
      </div>
      <div className="technical-ma-controls">
        {MA_PERIODS.map((period) => (
          <label key={period} className="technical-ma-toggle">
            <input
              type="checkbox"
              checked={Boolean(visibleMaLines[period])}
              onChange={() => toggleMaLine(period)}
            />
            <span style={{ "--ma-color": MA_LINE_COLORS[period] }}>{`MA${period}`}</span>
          </label>
        ))}
      </div>
      <div className="technical-chart" ref={containerRef} />
    </div>
  );
}

function TechnicalSummaryValue({ label, value, accent = false, digits = 2, suffix = "", formatter = null }) {
  const formattedValue = formatter
    ? formatter(value, digits)
    : `${formatOptionalChartNumber(value, digits)}${value === null || value === undefined ? "" : suffix}`;
  return (
    <div>
      <span>{label}</span>
      <strong className={accent ? "technical-accent-value" : ""}>{formattedValue}</strong>
    </div>
  );
}
