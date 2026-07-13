import React, { useEffect, useRef, useState } from "react";
import { Wifi } from "lucide-react";

import {
  formatDate,
  formatNumber,
  formatOptionalNumber,
  formatOptionalSignedPercent,
  formatTaipeiTime,
  percentageToneClass,
} from "../../utils/formatters";


const FUTURES_DATA_GAP_THRESHOLD_MS = 30 * 60 * 1000;


export function FuturesTrackerCard({ data }) {
  const hasPrice = data?.current_price !== null && data?.current_price !== undefined;
  const difference = data?.difference_points;
  const percent = data?.difference_percent;
  const direction = difference === null || difference === undefined || difference === 0 ? "－" : difference > 0 ? "▲" : "▼";
  const toneClass = percentageToneClass(percent);

  return (
    <section className="futures-card" aria-label="台指期近一 WTX&">
      <div className="futures-header">
        <div>
          <span className="futures-kicker">{data?.symbol || "WTX&"}</span>
          <strong>{data?.name || "台指期近一"}</strong>
        </div>
        <div className="futures-session">
          <span>{data?.session_label || "最近一盤"}</span>
          <small>{data?.price_updated_at ? formatDate(data.price_updated_at) : "待更新"}</small>
        </div>
      </div>
      <div className="futures-main">
        <div className="futures-quote">
          <strong className="constant-value">{hasPrice ? formatNumber(data.current_price) : "待更新"}</strong>
          <span className={toneClass}>
            {direction} {difference === null || difference === undefined ? "—" : formatNumber(Math.abs(difference))}
            {" "}
            ({formatOptionalSignedPercent(percent)})
          </span>
        </div>
        <div className="futures-open">
          <span>開盤價</span>
          <strong>{formatOptionalNumber(data?.open_price)}</strong>
          {data?.is_stale && <em>使用快取</em>}
        </div>
      </div>
      <FuturesLineChart data={data} />
    </section>
  );
}

function futuresAxisTicks(sessionStart, sessionEnd, sessionType) {
  if (!sessionStart || !sessionEnd || sessionEnd <= sessionStart) {
    return [];
  }
  const stepMs = (sessionType === "night" ? 2 : 1) * 60 * 60 * 1000;
  const ticks = [sessionStart];
  let next = sessionStart + stepMs;
  while (next < sessionEnd - 60_000) {
    ticks.push(next);
    next += stepMs;
  }
  if (ticks[ticks.length - 1] !== sessionEnd) {
    ticks.push(sessionEnd);
  }
  return ticks;
}

function splitFuturesPointSegments(points) {
  const segments = [];
  const gaps = [];
  let current = [];

  for (const point of points) {
    const previous = current[current.length - 1];
    if (previous && point.timestamp - previous.timestamp > FUTURES_DATA_GAP_THRESHOLD_MS) {
      segments.push(current);
      gaps.push({
        start: previous.timestamp,
        end: point.timestamp,
        minutes: Math.round((point.timestamp - previous.timestamp) / 60000),
      });
      current = [point];
    } else {
      current.push(point);
    }
  }

  if (current.length) {
    segments.push(current);
  }

  return { segments, gaps };
}

function futuresSegmentPath(segment, xScale, yScale) {
  return segment
    .map((point, index) => `${index === 0 ? "M" : "L"} ${xScale(point.timestamp)} ${yScale(point.value)}`)
    .join(" ");
}

function FuturesLineChart({ data }) {
  const containerRef = useRef(null);
  const [chartSize, setChartSize] = useState({ width: 760, height: 190 });
  const [hoverPoint, setHoverPoint] = useState(null);
  const points = data?.chart_points || [];
  const sessionStart = data?.session_start_at ? new Date(data.session_start_at).getTime() : null;
  const sessionEnd = data?.session_end_at ? new Date(data.session_end_at).getTime() : null;

  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return undefined;
    }
    const updateSize = () => {
      const rect = container.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        setChartSize({
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        });
      }
    };
    updateSize();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", updateSize);
      return () => window.removeEventListener("resize", updateSize);
    }
    const observer = new ResizeObserver(updateSize);
    observer.observe(container);
    return () => observer.disconnect();
  }, [points.length]);

  if (!points.length || sessionStart === null || sessionEnd === null || sessionEnd <= sessionStart) {
    return (
      <div className="futures-chart empty-chart">
        <Wifi size={15} />
        當盤圖表待更新
      </div>
    );
  }

  const width = Math.max(320, chartSize.width || 760);
  const height = Math.max(170, chartSize.height || 190);
  const margin = { top: 18, right: 22, bottom: 32, left: 52 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;
  const linePoints = points
    .map((point) => ({
      timestamp: new Date(point.timestamp).getTime(),
      value: Number(point.difference_percent),
      price: Number(point.price),
    }))
    .filter((point) => Number.isFinite(point.timestamp) && Number.isFinite(point.value) && Number.isFinite(point.price))
    .sort((left, right) => left.timestamp - right.timestamp);

  if (!linePoints.length) {
    return (
      <div className="futures-chart empty-chart">
        <Wifi size={15} />
        當盤圖表待更新
      </div>
    );
  }

  const values = linePoints.map((point) => point.value);
  const rawMin = Math.min(...values, 0);
  const rawMax = Math.max(...values, 0);
  const padding = rawMin === rawMax ? 0.2 : (rawMax - rawMin) * 0.15;
  const yMin = rawMin - padding;
  const yMax = rawMax + padding;
  const xScale = (timestamp) => margin.left + ((timestamp - sessionStart) / (sessionEnd - sessionStart)) * innerWidth;
  const yScale = (value) => margin.top + ((yMax - value) / (yMax - yMin)) * innerHeight;
  const { segments, gaps } = splitFuturesPointSegments(linePoints);
  const axisTicks = futuresAxisTicks(sessionStart, sessionEnd, data?.session_type);
  const zeroY = yScale(0);
  const selectedPoint = hoverPoint || null;
  const selectedX = selectedPoint ? xScale(selectedPoint.timestamp) : null;
  const selectedY = selectedPoint ? yScale(selectedPoint.value) : null;
  const selectedDifference =
    selectedPoint && data?.open_price ? selectedPoint.price - Number(data.open_price) : null;
  const tooltipWidth = 188;
  const tooltipHeight = 70;
  const tooltipX =
    selectedX === null ? 0 : Math.min(Math.max(selectedX + 12, margin.left), width - tooltipWidth - margin.right);
  const tooltipY =
    selectedY === null ? 0 : Math.min(Math.max(selectedY - tooltipHeight - 12, margin.top), height - tooltipHeight - margin.bottom);

  const updateHoverPoint = (clientX, target) => {
    const rect = target.getBoundingClientRect();
    const svgX = ((clientX - rect.left) / rect.width) * width;
    let nearest = linePoints[0];
    let nearestDistance = Math.abs(xScale(nearest.timestamp) - svgX);
    for (const point of linePoints) {
      const distance = Math.abs(xScale(point.timestamp) - svgX);
      if (distance < nearestDistance) {
        nearest = point;
        nearestDistance = distance;
      }
    }
    setHoverPoint(nearest);
  };

  return (
    <div className="futures-chart" ref={containerRef}>
      <svg
        className="futures-svg-chart"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label="台指期當盤漲跌幅"
        onPointerMove={(event) => updateHoverPoint(event.clientX, event.currentTarget)}
        onPointerLeave={() => setHoverPoint(null)}
        onMouseMove={(event) => updateHoverPoint(event.clientX, event.currentTarget)}
        onMouseLeave={() => setHoverPoint(null)}
        onTouchMove={(event) => {
          const touch = event.touches[0];
          if (touch) {
            updateHoverPoint(touch.clientX, event.currentTarget);
          }
        }}
        onTouchEnd={() => setHoverPoint(null)}
      >
        {[0, 0.5, 1].map((ratio) => {
          const y = margin.top + ratio * innerHeight;
          return <line key={`fy-${ratio}`} className="futures-grid-line" x1={margin.left} x2={width - margin.right} y1={y} y2={y} />;
        })}
        {axisTicks.map((timestamp) => {
          const x = xScale(timestamp);
          return (
            <g key={timestamp}>
              <line className="futures-grid-line soft" x1={x} x2={x} y1={margin.top} y2={height - margin.bottom} />
              <text className="futures-axis-label" x={x} y={height - 9} textAnchor="middle">
                {formatTaipeiTime(timestamp)}
              </text>
            </g>
          );
        })}
        {zeroY >= margin.top && zeroY <= height - margin.bottom && (
          <line className="futures-zero-line" x1={margin.left} x2={width - margin.right} y1={zeroY} y2={zeroY} />
        )}
        {gaps.map((gap) => {
          const gapX = Math.max(margin.left, xScale(gap.start));
          const gapEndX = Math.min(width - margin.right, xScale(gap.end));
          const gapWidth = Math.max(0, gapEndX - gapX);
          if (gapWidth <= 0) {
            return null;
          }
          return (
            <g key={`${gap.start}-${gap.end}`}>
              <rect
                className="futures-gap-band"
                x={gapX}
                y={margin.top}
                width={gapWidth}
                height={innerHeight}
              />
              {gapWidth > 74 && (
                <text
                  className="futures-gap-label"
                  x={gapX + gapWidth / 2}
                  y={margin.top + 18}
                  textAnchor="middle"
                >
                  資料中斷 {gap.minutes} 分
                </text>
              )}
            </g>
          );
        })}
        <text className="futures-axis-label" x={margin.left - 8} y={margin.top + 5} textAnchor="end">
          {formatOptionalSignedPercent(yMax)}
        </text>
        <text className="futures-axis-label" x={margin.left - 8} y={height - margin.bottom} textAnchor="end">
          {formatOptionalSignedPercent(yMin)}
        </text>
        {segments.map((segment, index) =>
          segment.length > 1 ? (
            <path
              key={`segment-${index}`}
              className="futures-line"
              d={futuresSegmentPath(segment, xScale, yScale)}
              fill="none"
            />
          ) : (
            <circle
              key={`segment-${index}`}
              className="futures-point-marker"
              cx={xScale(segment[0].timestamp)}
              cy={yScale(segment[0].value)}
              r="3.5"
            />
          ),
        )}
        {selectedPoint && selectedX !== null && selectedY !== null && (
          <g className="futures-hover-layer">
            <line className="futures-hover-line" x1={selectedX} x2={selectedX} y1={margin.top} y2={height - margin.bottom} />
            <line className="futures-hover-line" x1={margin.left} x2={width - margin.right} y1={selectedY} y2={selectedY} />
            <circle className="futures-hover-marker" cx={selectedX} cy={selectedY} r="4" />
            <g transform={`translate(${tooltipX} ${tooltipY})`}>
              <rect className="futures-tooltip-box" width={tooltipWidth} height={tooltipHeight} rx="8" />
              <text className="futures-tooltip-label" x="12" y="20">
                {formatTaipeiTime(selectedPoint.timestamp)}
              </text>
              <text className="futures-tooltip-value" x="12" y="43">
                {formatNumber(selectedPoint.price)}
              </text>
              <text className={selectedPoint.value >= 0 ? "futures-tooltip-positive" : "futures-tooltip-negative"} x="12" y="61">
                {selectedDifference === null ? "—" : formatNumber(Math.abs(selectedDifference))}
                {" "}
                ({formatOptionalSignedPercent(selectedPoint.value)})
              </text>
            </g>
          </g>
        )}
      </svg>
    </div>
  );
}
