import React from "react";

import { formatCountdown, formatDate, formatTradingDate } from "../../utils/formatters";
import { latestMetricTime } from "../../utils/stocks";


const REFRESH_STATUS_LABELS = {
  idle: "待命",
  queued: "已排入",
  running: "更新中",
  refreshing: "更新中",
  success: "已更新",
  failed: "更新失敗，使用快取",
  retry_wait: "等待重試",
};
const MARKET_SESSION_LABELS = {
  always_on: "24 小時更新中",
  market_open: "盤中行情更新中",
  off_hours: "盤外低頻確認",
  open: "盤中更新中",
  pre_open: "24 小時更新中",
  post_close: "24 小時更新中",
  weekend: "24 小時更新中",
};
const REFRESH_CHANNEL_LABELS = {
  quote: "行情",
  fundamentals: "基本面",
  broker: "主力",
  history: "歷史",
};


export function DashboardSummary({ metadata, refreshStatus, stocks, now, pollSeconds = 5 }) {
  const latestDataTime = latestMetricTime(stocks);
  const latestOfficialDataDate = metadata?.latest_official_data_date;
  const refreshWindow = refreshStatus.refresh_window || metadata?.refresh_window || "24 小時分流排程 Asia/Taipei";
  const marketSessionLabel = MARKET_SESSION_LABELS[refreshStatus.market_session] || "24 小時分流排程";
  const refreshChannels = refreshStatus.channels || {};
  const running = Object.entries(refreshChannels)
    .filter(([, channel]) => (channel.current_symbols || []).length)
    .map(([key, channel]) => `${REFRESH_CHANNEL_LABELS[key] || key} ${channel.current_symbols.join("、")}`);
  const queued = Object.entries(refreshChannels)
    .filter(([, channel]) => channel.queue_length)
    .map(([key, channel]) => `${REFRESH_CHANNEL_LABELS[key] || key} ${channel.queue_length}`);
  const currentRefreshText = running.length ? running.join(" · ") : queued.length ? queued.join(" · ") : "無";

  return (
    <section className="summary-grid" aria-label="overview">
      <div className="metric">
        <span>背景快取</span>
        <strong>{REFRESH_STATUS_LABELS[refreshStatus.status] || refreshStatus.status || "待命"}</strong>
        <small>{refreshWindow} · 失敗使用快取</small>
      </div>
      <div className="metric">
        <span>自動更新</span>
        <strong>{marketSessionLabel}</strong>
        <small>盤中行情 {metadata?.refresh_interval_seconds || 60} 秒 · 盤外 15 分鐘 · 下次 {formatCountdown(refreshStatus.next_auto_refresh_at, now)}</small>
      </div>
      <div className="metric">
        <span>目前更新</span>
        <strong>{currentRefreshText}</strong>
        <small>行情／基本面／主力／歷史獨立 queue · 前端每 {pollSeconds} 秒讀快取</small>
      </div>
      <div className="metric">
        <span>最近資料</span>
        <strong>{latestOfficialDataDate ? formatTradingDate(latestOfficialDataDate) : formatDate(latestDataTime)}</strong>
        <small>TWSE / FinMind 資料 · {stocks.length} 檔標的 · WTX 交易時段每 10 秒</small>
      </div>
    </section>
  );
}
