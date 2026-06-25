import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { createRoot } from "react-dom/client";
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  TouchSensor,
  closestCenter,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  CandlestickSeries,
  CrosshairMode,
  LineSeries,
  createChart,
} from "lightweight-charts";
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock3,
  Database,
  GripVertical,
  Loader2,
  Plus,
  RefreshCcw,
  Search,
  Sparkles,
  Trash2,
  Wifi,
  X,
} from "lucide-react";
import "./styles.css";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";
const POLL_SECONDS = 5;
const BACKGROUND_REFRESH_SECONDS = 60;
const MA_PERIODS = [5, 10, 20, 60, 120, 240];
const MA_VISIBILITY_STORAGE_KEY = "stock-dashboard-visible-ma-lines";
const AI_MODE_STORAGE_PREFIX = "stock-dashboard-ai-analysis-mode";
const FUNDAMENTAL_CATEGORY_STORAGE_PREFIX = "stock-dashboard-fundamental-category";
const FUTURES_DATA_GAP_THRESHOLD_MS = 30 * 60 * 1000;
const FUNDAMENTAL_CATEGORY_KEYS = ["eps", "monthly_revenue", "gross_margin", "operating_margin", "net_margin"];
const FUNDAMENTAL_CATEGORY_LABELS = {
  eps: "EPS",
  monthly_revenue: "月營收",
  gross_margin: "毛利率",
  operating_margin: "營益率",
  net_margin: "淨利率",
};
const MA_LINE_COLORS = {
  5: "#f08f7f",
  10: "#e2c879",
  20: "#7fd8ff",
  60: "#b99cff",
  120: "#8be0b2",
  240: "#f2a7d8",
};

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

const MARKET_SESSION_LABELS = {
  always_on: "24 小時更新中",
  open: "盤中更新中",
  pre_open: "24 小時更新中",
  post_close: "24 小時更新中",
  weekend: "24 小時更新中",
};

function formatNumber(value, digits = 2) {
  return new Intl.NumberFormat("zh-TW", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value ?? 0);
}

function formatOptionalNumber(value, digits = 2) {
  if (value === null || value === undefined) {
    return "待更新";
  }
  return formatNumber(value, digits);
}

function formatOptionalChartNumber(value, digits = 2) {
  if (value === null || value === undefined) {
    return "—";
  }
  return formatNumber(value, digits);
}

function formatOptionalPe(value) {
  if (value === null || value === undefined) {
    return "不適用";
  }
  return formatNumber(value);
}

function formatOptionalSignedPercent(value, digits = 2) {
  if (value === null || value === undefined) {
    return "—";
  }
  return `${formatSignedNumber(value, digits)}%`;
}

function formatOptionalPercent(value, digits = 2) {
  if (value === null || value === undefined) {
    return "—";
  }
  return `${formatNumber(value, digits)}%`;
}

function formatPeRange(minValue, maxValue) {
  if (minValue === null || minValue === undefined || maxValue === null || maxValue === undefined) {
    return "待更新";
  }
  return `${formatNumber(minValue)}～${formatNumber(maxValue)}`;
}

function formatSignedNumber(value, digits = 2) {
  return new Intl.NumberFormat("zh-TW", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    signDisplay: "exceptZero",
  }).format(value ?? 0);
}

function valueToneClass(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return value >= 0 ? "positive" : "negative";
}

function percentageToneClass(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return value >= 0 ? "percentage-positive" : "percentage-negative";
}

function defaultMaVisibility() {
  return Object.fromEntries(MA_PERIODS.map((period) => [period, false]));
}

function loadMaVisibility() {
  const fallback = defaultMaVisibility();
  try {
    const stored = window.localStorage.getItem(MA_VISIBILITY_STORAGE_KEY);
    if (!stored) {
      return fallback;
    }
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
    // Ignore storage failures; the chart still works for the current session.
  }
}

function comparisonPercent(currentPrice, indicatorPrice) {
  if (
    currentPrice === null ||
    currentPrice === undefined ||
    indicatorPrice === null ||
    indicatorPrice === undefined ||
    Number(indicatorPrice) === 0
  ) {
    return null;
  }
  return ((Number(currentPrice) - Number(indicatorPrice)) / Number(indicatorPrice)) * 100;
}

function comparisonToneClass(value) {
  if (value === null || value === undefined || value === 0) {
    return "percentage-zero";
  }
  return percentageToneClass(value);
}

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

function formatFundamentalMetric(value, valueType = "number", categoryKey = "") {
  if (value === null || value === undefined) {
    return "待更新";
  }
  if (valueType === "percent") {
    return formatOptionalSignedPercent(value);
  }
  if (categoryKey === "monthly_revenue") {
    return `${formatNumber(Number(value) / 100000000)} 億`;
  }
  return formatNumber(value);
}

function fundamentalToneClass(value, valueType = "number") {
  if (value === null || value === undefined) {
    return "";
  }
  return valueType === "percent" ? percentageToneClass(value) : "constant-value";
}

function trendDisplayValue(value, categoryKey) {
  if (value === null || value === undefined) {
    return "待更新";
  }
  if (categoryKey === "monthly_revenue") {
    return `${formatNumber(Number(value) / 100000000)} 億`;
  }
  if (categoryKey === "gross_margin" || categoryKey === "operating_margin" || categoryKey === "net_margin") {
    return `${formatNumber(value)}%`;
  }
  return formatNumber(value);
}

function trendNumericValue(value, categoryKey) {
  if (value === null || value === undefined) {
    return null;
  }
  if (categoryKey === "monthly_revenue") {
    return Number(value) / 100000000;
  }
  return Number(value);
}

function formatTradingDate(value) {
  if (!value) {
    return "待更新";
  }
  const [year, month, day] = String(value).split("-");
  return year && month && day ? `${year}/${month}/${day}` : String(value);
}

function formatDate(value) {
  if (!value) {
    return "待更新";
  }

  return new Intl.DateTimeFormat("zh-TW", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatTaipeiTime(value) {
  if (value === null || value === undefined) {
    return "";
  }
  const timestamp =
    typeof value === "number"
      ? value > 10_000_000_000
        ? value
        : value * 1000
      : new Date(value).getTime();
  return new Intl.DateTimeFormat("zh-TW", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Taipei",
  }).format(new Date(timestamp));
}

function formatCountdown(value, now) {
  if (!value) {
    return "待排程";
  }

  const seconds = Math.max(0, Math.ceil((new Date(value).getTime() - now.getTime()) / 1000));
  if (seconds < 60) {
    return `${seconds} 秒`;
  }

  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${minutes} 分 ${remainder} 秒`;
}

async function parseError(response) {
  try {
    const body = await response.json();
    return body.detail || `API ${response.status}`;
  } catch {
    return `API ${response.status}`;
  }
}

function latestMetricTime(stocks) {
  const dates = stocks
    .map((stock) => stock.metric?.price_updated_at)
    .filter(Boolean);
  if (!dates.length) {
    return null;
  }

  return dates.reduce((latest, value) => (new Date(value) > new Date(latest) ? value : latest), dates[0]);
}

function applyDisplayOrder(stocks) {
  return stocks.map((stock, index) => ({
    ...stock,
    display_order: (index + 1) * 10,
  }));
}

function isPendingRefresh(state) {
  return state?.status === "queued" || state?.status === "running" || state?.status === "refreshing";
}

function isVisibleRefreshState(state, now) {
  if (!state) {
    return false;
  }
  if (state.status === "queued" || state.status === "running" || state.status === "refreshing" || state.status === "failed" || state.status === "retry_wait") {
    return true;
  }
  if (state.status === "success" && state.finished_at) {
    return now.getTime() - new Date(state.finished_at).getTime() < 15000;
  }
  return false;
}

function App() {
  const [stocks, setStocks] = useState([]);
  const [futuresData, setFuturesData] = useState(null);
  const [metadata, setMetadata] = useState(null);
  const [brokerSetting, setBrokerSetting] = useState(null);
  const [refreshStatus, setRefreshStatus] = useState({ status: "idle", symbols: [], queue_length: 0 });
  const [symbolInput, setSymbolInput] = useState("2330");
  const [loading, setLoading] = useState(true);
  const [reordering, setReordering] = useState(false);
  const [activeDragSymbol, setActiveDragSymbol] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [now, setNow] = useState(() => new Date());
  const [aiAnalysisPendingBySymbol, setAiAnalysisPendingBySymbol] = useState({});

  const reorderingRef = useRef(false);
  const autoScrollFrameRef = useRef(0);
  const autoScrollSpeedRef = useRef(0);
  const stockCardRefs = useRef(new Map());

  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: 6 },
    }),
    useSensor(TouchSensor, {
      activationConstraint: { delay: 140, tolerance: 8 },
    }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  const orderedStocks = useMemo(
    () =>
      [...stocks].sort(
        (left, right) =>
          (left.display_order ?? 0) - (right.display_order ?? 0) ||
          left.symbol.localeCompare(right.symbol),
      ),
    [stocks],
  );

  const refreshStateBySymbol = useMemo(
    () => new Map((refreshStatus?.symbols || []).map((state) => [state.symbol, state])),
    [refreshStatus],
  );

  const loadData = useCallback(async ({ showLoading = false, silent = false } = {}) => {
    if (showLoading) {
      setLoading(true);
    }
    if (!silent) {
      setError("");
    }

    try {
      const [stockResponse, metadataResponse, statusResponse, brokerResponse, futuresResponse] = await Promise.all([
        fetch(`${API_BASE_URL}/api/stocks`),
        fetch(`${API_BASE_URL}/api/metadata`),
        fetch(`${API_BASE_URL}/api/refresh/status`),
        fetch(`${API_BASE_URL}/api/settings/broker`),
        fetch(`${API_BASE_URL}/api/futures/wtx`),
      ]);

      if (!stockResponse.ok) {
        throw new Error(await parseError(stockResponse));
      }
      if (!statusResponse.ok) {
        throw new Error(await parseError(statusResponse));
      }
      if (!brokerResponse.ok) {
        throw new Error(await parseError(brokerResponse));
      }

      const nextStocks = await stockResponse.json();
      if (!reorderingRef.current) {
        setStocks(nextStocks);
      }
      setMetadata(metadataResponse.ok ? await metadataResponse.json() : null);
      setRefreshStatus(await statusResponse.json());
      setBrokerSetting(await brokerResponse.json());
      setFuturesData(futuresResponse.ok ? await futuresResponse.json() : null);
    } catch (requestError) {
      if (!silent) {
        setError(requestError.message);
      }
    } finally {
      if (showLoading) {
        setLoading(false);
      }
    }
  }, []);

  const registerStockCard = useCallback((symbol, node) => {
    if (node) {
      stockCardRefs.current.set(symbol, node);
    } else {
      stockCardRefs.current.delete(symbol);
    }
  }, []);

  const scrollStockToCenter = useCallback((symbol) => {
    window.requestAnimationFrame(() => {
      stockCardRefs.current.get(symbol)?.scrollIntoView({
        block: "center",
        behavior: "smooth",
      });
    });
  }, []);

  const setAiAnalysisPending = useCallback((symbol, pending) => {
    setAiAnalysisPendingBySymbol((current) => {
      if (pending) {
        return { ...current, [symbol]: true };
      }
      if (!current[symbol]) {
        return current;
      }
      const next = { ...current };
      delete next[symbol];
      return next;
    });
  }, []);

  async function queueRefreshSymbol(symbol) {
    const normalized = symbol.trim();
    if (!normalized) {
      setError("請輸入股票代號");
      return;
    }

    setError("");
    setMessage("");

    try {
      const response = await fetch(`${API_BASE_URL}/api/stocks/${normalized}/refresh`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }

      const result = await response.json();
      await loadData({ showLoading: false });
      setMessage(`${result.symbol || normalized} 已排入背景更新`);
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function queueRefreshAll() {
    setError("");
    setMessage("");

    try {
      const response = await fetch(`${API_BASE_URL}/api/stocks/refresh`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }

      const result = await response.json();
      await loadData({ showLoading: false });
      setMessage(result.symbols.length ? "全部數據已排入全量更新" : "目前沒有可更新的標的");
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function deleteStock(symbol) {
    if (!window.confirm(`永久刪除 ${symbol}？這會從本機 SQLite 刪除標的與相關快取資料。`)) {
      return;
    }

    setError("");
    setMessage("");

    try {
      const response = await fetch(`${API_BASE_URL}/api/stocks/${symbol}`, {
        method: "DELETE",
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }

      await loadData({ showLoading: false });
      setMessage(`${symbol} 已從資料庫刪除`);
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  function replaceStock(nextStock) {
    setStocks((currentStocks) =>
      currentStocks.map((stock) => (stock.symbol === nextStock.symbol ? nextStock : stock)),
    );
  }

  async function savePosition(symbol, buyPrice) {
    setError("");
    setMessage("");

    try {
      const response = await fetch(`${API_BASE_URL}/api/stocks/${symbol}/position`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ buy_price: Number(buyPrice) }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }

      replaceStock(await response.json());
      setMessage(`${symbol} 買入價已更新`);
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function clearPosition(symbol) {
    setError("");
    setMessage("");

    try {
      const response = await fetch(`${API_BASE_URL}/api/stocks/${symbol}/position`, {
        method: "DELETE",
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }

      replaceStock(await response.json());
      setMessage(`${symbol} 已賣出，買入價已清除`);
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function updateBroker(brokerId) {
    setError("");
    setMessage("");

    try {
      const response = await fetch(`${API_BASE_URL}/api/settings/broker`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ broker_id: brokerId }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }

      const nextSetting = await response.json();
      setBrokerSetting(nextSetting);
      await loadData({ showLoading: false, silent: true });
      setMessage(`券商已切換為 ${nextSetting.selected.name}`);
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function persistOrder(nextStocks, previousStocks, focusSymbol = "") {
    reorderingRef.current = true;
    setStocks(nextStocks);
    if (focusSymbol) {
      scrollStockToCenter(focusSymbol);
    }
    setReordering(true);
    setError("");
    setMessage("");

    try {
      const response = await fetch(`${API_BASE_URL}/api/stocks/reorder`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ symbols: nextStocks.map((stock) => stock.symbol) }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }

      setStocks(await response.json());
      if (focusSymbol) {
        scrollStockToCenter(focusSymbol);
      }
      setMessage("排序已更新");
    } catch (requestError) {
      setStocks(previousStocks);
      setError(requestError.message);
    } finally {
      reorderingRef.current = false;
      setReordering(false);
    }
  }

  function moveStock(symbol, direction) {
    if (reordering) {
      return;
    }

    const oldIndex = orderedStocks.findIndex((stock) => stock.symbol === symbol);
    const newIndex = oldIndex + direction;
    if (oldIndex < 0 || newIndex < 0 || newIndex >= orderedStocks.length) {
      return;
    }

    const nextStocks = applyDisplayOrder(arrayMove(orderedStocks, oldIndex, newIndex));
    persistOrder(nextStocks, orderedStocks, symbol);
  }

  function handleDragStart(event) {
    setActiveDragSymbol(event.active.id);
  }

  function handleDragEnd(event) {
    const { active, over } = event;
    setActiveDragSymbol("");

    if (!over || active.id === over.id || reordering) {
      return;
    }

    const oldIndex = orderedStocks.findIndex((stock) => stock.symbol === active.id);
    const newIndex = orderedStocks.findIndex((stock) => stock.symbol === over.id);
    if (oldIndex < 0 || newIndex < 0) {
      return;
    }

    const nextStocks = applyDisplayOrder(arrayMove(orderedStocks, oldIndex, newIndex));
    persistOrder(nextStocks, orderedStocks);
  }

  useEffect(() => {
    loadData({ showLoading: true });
  }, [loadData]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      loadData({ showLoading: false, silent: true });
    }, POLL_SECONDS * 1000);

    return () => window.clearInterval(timer);
  }, [loadData]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    function handleFocus() {
      loadData({ showLoading: false, silent: true });
    }

    window.addEventListener("focus", handleFocus);
    document.addEventListener("visibilitychange", handleFocus);
    return () => {
      window.removeEventListener("focus", handleFocus);
      document.removeEventListener("visibilitychange", handleFocus);
    };
  }, [loadData]);

  useEffect(() => {
    if (!activeDragSymbol) {
      autoScrollSpeedRef.current = 0;
      if (autoScrollFrameRef.current) {
        window.cancelAnimationFrame(autoScrollFrameRef.current);
        autoScrollFrameRef.current = 0;
      }
      return undefined;
    }

    const edgeSize = 96;
    const maxSpeed = 22;

    function tickAutoScroll() {
      const speed = autoScrollSpeedRef.current;
      if (!speed) {
        autoScrollFrameRef.current = 0;
        return;
      }

      window.scrollBy(0, speed);
      autoScrollFrameRef.current = window.requestAnimationFrame(tickAutoScroll);
    }

    function scheduleAutoScroll() {
      if (!autoScrollFrameRef.current) {
        autoScrollFrameRef.current = window.requestAnimationFrame(tickAutoScroll);
      }
    }

    function handlePointerMove(event) {
      const touch = event.touches?.[0];
      const clientY = touch ? touch.clientY : event.clientY;
      if (typeof clientY !== "number") {
        return;
      }

      const distanceFromBottom = window.innerHeight - clientY;
      if (clientY < edgeSize) {
        autoScrollSpeedRef.current = -Math.ceil(((edgeSize - clientY) / edgeSize) * maxSpeed);
        scheduleAutoScroll();
      } else if (distanceFromBottom < edgeSize) {
        autoScrollSpeedRef.current = Math.ceil(((edgeSize - distanceFromBottom) / edgeSize) * maxSpeed);
        scheduleAutoScroll();
      } else {
        autoScrollSpeedRef.current = 0;
      }
    }

    window.addEventListener("pointermove", handlePointerMove, { passive: true });
    window.addEventListener("touchmove", handlePointerMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("touchmove", handlePointerMove);
      autoScrollSpeedRef.current = 0;
      if (autoScrollFrameRef.current) {
        window.cancelAnimationFrame(autoScrollFrameRef.current);
        autoScrollFrameRef.current = 0;
      }
    };
  }, [activeDragSymbol]);

  const activeStock = orderedStocks.find((stock) => stock.symbol === activeDragSymbol);
  const latestDataTime = latestMetricTime(stocks);
  const latestOfficialDataDate = metadata?.latest_official_data_date;
  const refreshWindow = refreshStatus.refresh_window || metadata?.refresh_window || "24 小時不間斷 Asia/Taipei";
  const marketSessionLabel = MARKET_SESSION_LABELS[refreshStatus.market_session] || "24 小時更新中";
  const lastCloseVerification = refreshStatus.last_close_verification_at || metadata?.last_close_verification_at;
  const currentRefreshText = refreshStatus.current_symbol
    ? refreshStatus.current_symbol
    : refreshStatus.queue_length
      ? `${refreshStatus.queue_length} 筆排隊`
      : "無";

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Stock Valuation Dashboard</p>
          <h1>股票估值統計看板</h1>
        </div>
        <button
          className="icon-button"
          type="button"
          onClick={queueRefreshAll}
          title="更新全部數據"
          aria-label="更新全部數據"
        >
          <RefreshCcw size={18} />
        </button>
      </header>

      <section className="summary-grid" aria-label="overview">
        <div className="metric">
          <span>背景快取</span>
          <strong>{REFRESH_STATUS_LABELS[refreshStatus.status] || refreshStatus.status || "待命"}</strong>
          <small>{refreshWindow} · 失敗使用快取</small>
        </div>
        <div className="metric">
          <span>自動更新</span>
          <strong>{marketSessionLabel}</strong>
          <small>每 {metadata?.refresh_interval_seconds || BACKGROUND_REFRESH_SECONDS} 秒更新 · PE/EPS 每日一次 · 下次 {formatCountdown(refreshStatus.next_auto_refresh_at, now)}</small>
        </div>
        <div className="metric">
          <span>目前更新</span>
          <strong>{currentRefreshText}</strong>
          <small>前端每 {POLL_SECONDS} 秒讀快取 · {metadata?.data_source || "SQLite 快取資料"}</small>
        </div>
        <div className="metric">
          <span>最近資料</span>
          <strong>{latestOfficialDataDate ? formatTradingDate(latestOfficialDataDate) : formatDate(latestDataTime)}</strong>
          <small>TWSE / FinMind 資料 · {stocks.length} 檔標的 · 18:00 全量補抓 {formatDate(lastCloseVerification)}</small>
        </div>
      </section>

      <form className="toolbar" aria-label="stock controls" onSubmit={(event) => {
        event.preventDefault();
        queueRefreshSymbol(symbolInput);
      }}>
        <label className="search-box">
          <Search size={18} />
          <input
            inputMode="numeric"
            value={symbolInput}
            onChange={(event) => setSymbolInput(event.target.value)}
            placeholder="股票代號"
          />
        </label>
        <button className="text-button primary" type="submit">
          <Plus size={17} />
          加入/更新
        </button>
        <label className="broker-select-field">
          <span>券商</span>
          <select
            value={brokerSetting?.selected_broker || "CATHAY"}
            onChange={(event) => updateBroker(event.target.value)}
            aria-label="選擇券商"
          >
            {(brokerSetting?.brokers || []).map((broker) => (
              <option key={broker.broker_id} value={broker.broker_id}>
                {broker.name}
              </option>
            ))}
          </select>
          <ChevronDown className="broker-select-icon" size={17} aria-hidden="true" />
        </label>
      </form>

      {error && (
        <div className="notice error">
          <AlertCircle size={18} />
          <span>資料暫時無法更新：{error}</span>
        </div>
      )}

      {message && !error && <div className="notice success">{message}</div>}

      <FuturesTrackerCard data={futuresData} />

      <section className="stock-grid" aria-label="stocks">
        {loading ? (
          <div className="empty">
            <Database size={18} />
            載入中
          </div>
        ) : orderedStocks.length ? (
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            autoScroll
            onDragStart={handleDragStart}
            onDragCancel={() => setActiveDragSymbol("")}
            onDragEnd={handleDragEnd}
          >
            <SortableContext items={orderedStocks.map((stock) => stock.symbol)} strategy={verticalListSortingStrategy}>
              {orderedStocks.map((stock, index) => {
                const state = refreshStateBySymbol.get(stock.symbol);
                return (
                  <SortableStockCard
                    key={stock.symbol}
                    stock={stock}
                    index={index}
                    total={orderedStocks.length}
                    refreshState={state}
                    showRefreshState={isVisibleRefreshState(state, now)}
                    sortingDisabled={reordering || isPendingRefresh(state)}
                    actionDisabled={reordering}
                    aiAnalysisPending={Boolean(aiAnalysisPendingBySymbol[stock.symbol])}
                    onAiAnalysisPendingChange={(pending) => setAiAnalysisPending(stock.symbol, pending)}
                    onRegisterRef={registerStockCard}
                    onMoveUp={() => moveStock(stock.symbol, -1)}
                    onMoveDown={() => moveStock(stock.symbol, 1)}
                    onRefresh={() => queueRefreshSymbol(stock.symbol)}
                    onDelete={() => deleteStock(stock.symbol)}
                    onSavePosition={(buyPrice) => savePosition(stock.symbol, buyPrice)}
                    onClearPosition={() => clearPosition(stock.symbol)}
                  />
                );
              })}
            </SortableContext>
            <DragOverlay>
              {activeStock ? (
                <StockCard
                  stock={activeStock}
                  refreshState={refreshStateBySymbol.get(activeStock.symbol)}
                  showRefreshState={false}
                  overlay
                  sortingDisabled
                  actionDisabled
                  aiAnalysisPending={Boolean(aiAnalysisPendingBySymbol[activeStock.symbol])}
                  onAiAnalysisPendingChange={() => {}}
                  canMoveUp={false}
                  canMoveDown={false}
                  onMoveUp={() => {}}
                  onMoveDown={() => {}}
                  onRefresh={() => {}}
                  onDelete={() => {}}
                  onSavePosition={() => {}}
                  onClearPosition={() => {}}
                />
              ) : null}
            </DragOverlay>
          </DndContext>
        ) : (
          <div className="empty">
            <Database size={18} />
            無資料
          </div>
        )}
      </section>

      <footer>
        <span>本看板僅用於資料整理與估值比較，不構成任何投資建議。</span>
        <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">Charts by TradingView</a>
      </footer>
    </main>
  );
}

function SortableStockCard({
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

const StockCard = React.forwardRef(function StockCard(
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
  const aiButtonRef = useRef(null);

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
            expanded={fundamentalExpanded}
            onToggle={() => setFundamentalExpanded((current) => !current)}
          />
        )}
        <BrokerTradingDisclosure
          brokerTrading={stock.broker_trading}
          expanded={brokerTradingExpanded}
          onToggle={() => setBrokerTradingExpanded((current) => !current)}
        />
        <TechnicalAnalysisDisclosure
          symbol={stock.symbol}
          metricUpdatedAt={metric?.price_updated_at}
          expanded={technicalExpanded}
          onToggle={() => setTechnicalExpanded((current) => !current)}
        />
      </div>
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

function FuturesTrackerCard({ data }) {
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

function TechnicalAnalysisDisclosure({ symbol, metricUpdatedAt, expanded, onToggle }) {
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
        <small>日線 · MA</small>
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

function hasRunningAiAnalysis(response) {
  return Boolean(response?.running?.unheld || response?.running?.held);
}

function mergeAiAnalysisResponse(current, next) {
  if (!current || !next) {
    return next;
  }
  return {
    ...next,
    analyses: {
      unheld: next.analyses?.unheld || current.analyses?.unheld || null,
      held: next.analyses?.held || current.analyses?.held || null,
    },
  };
}

function AIAnalysisPopover({
  stock,
  open,
  anchorRef,
  onClose,
  analysisPending = false,
  onAnalysisPendingChange = () => {},
}) {
  const hasPosition = Boolean(stock.position);
  const panelRef = useRef(null);
  const generationActiveRef = useRef(false);
  const [analysisResponse, setAnalysisResponse] = useState(null);
  const [activeMode, setActiveMode] = useState(() => loadAiMode(stock.symbol, hasPosition));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [panelStyle, setPanelStyle] = useState({});

  const loadLatestAnalysis = useCallback(
    async ({ signal, showLoading = false } = {}) => {
      if (showLoading) {
        setLoading(true);
      }
      setError("");
      try {
        const response = await fetch(`${API_BASE_URL}/api/stocks/${stock.symbol}/ai-analysis/latest`, {
          signal,
        });
        if (!response.ok) {
          throw new Error(await parseError(response));
        }
        const payload = await response.json();
        setAnalysisResponse((current) => mergeAiAnalysisResponse(current, payload));
        const running = hasRunningAiAnalysis(payload);
        if (running) {
          onAnalysisPendingChange(true);
        } else if (!generationActiveRef.current) {
          onAnalysisPendingChange(false);
        }
        return payload;
      } catch (requestError) {
        if (requestError.name !== "AbortError") {
          setError(requestError.message);
        }
        return null;
      } finally {
        if (!signal?.aborted && showLoading) {
          setLoading(false);
        }
      }
    },
    [onAnalysisPendingChange, stock.symbol],
  );

  useEffect(() => {
    setAnalysisResponse(null);
    setError("");
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
    const controller = new AbortController();
    loadLatestAnalysis({ signal: controller.signal, showLoading: true });
    return () => controller.abort();
  }, [loadLatestAnalysis, open, stock.symbol]);

  useEffect(() => {
    if (!analysisPending && !hasRunningAiAnalysis(analysisResponse)) {
      return undefined;
    }
    const intervalId = window.setInterval(() => {
      loadLatestAnalysis();
    }, 3000);
    return () => window.clearInterval(intervalId);
  }, [analysisPending, analysisResponse, loadLatestAnalysis]);

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
    if (analysisPending || hasRunningAiAnalysis(analysisResponse)) {
      return;
    }
    generationActiveRef.current = true;
    onAnalysisPendingChange(true);
    setLoading(true);
    setError("");
    try {
      const hasExistingAnalysis = Boolean(
        analysisResponse?.analyses?.unheld || analysisResponse?.analyses?.held,
      );
      const response = await fetch(`${API_BASE_URL}/api/stocks/${stock.symbol}/ai-analysis`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force_refresh: hasExistingAnalysis }),
      });
      if (!response.ok) {
        throw new Error(await parseError(response));
      }
      const payload = await response.json();
      setAnalysisResponse((current) => mergeAiAnalysisResponse(current, payload));
      if (!hasRunningAiAnalysis(payload)) {
        onAnalysisPendingChange(false);
      }
    } catch (requestError) {
      setError(requestError.message);
      onAnalysisPendingChange(false);
    } finally {
      generationActiveRef.current = false;
      setLoading(false);
    }
  }

  if (!open) {
    return null;
  }

  const modeKey = activeMode === "HELD" ? "held" : "unheld";
  const result = analysisResponse?.analyses?.[modeKey];
  const analysis = result?.analysis;
  const modeError = analysisResponse?.errors?.[modeKey];
  const hasAnyAnalysis = Boolean(analysisResponse?.analyses?.unheld || analysisResponse?.analyses?.held);
  const running = analysisPending || hasRunningAiAnalysis(analysisResponse);

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
            {running ? "分析處理中" : hasAnyAnalysis ? "更新分析" : "產生分析"}
          </button>
        </div>
        {(error || modeError) && (
          <div className="ai-analysis-error">
            <AlertCircle size={15} />
            {modeError || error}
          </div>
        )}
        {(loading || running) && !analysis ? (
          <div className="ai-analysis-empty">
            <Loader2 className="spin" size={15} />
            未持有與持有中分析處理中
          </div>
        ) : analysis ? (
          <>
            <div className="ai-status-row">
              <span>{activeMode === "HELD" ? "持有判斷" : "進場判斷"}</span>
              <strong>{analysis.overall_status}</strong>
            </div>
            <p>{analysis.summary}</p>
            <div className="ai-analysis-lists">
              <AIAnalysisList title="正面因素" items={analysis.positive_points} />
              <AIAnalysisList title="風險因素" items={analysis.risk_points} />
              <AIAnalysisList title="後續觀察" items={analysis.watch_points} />
            </div>
            <small>
              {result.provider} · {result.model}
              {result.cached ? " · 使用快取" : " · 新產生"}
              {" · "}
              {formatDate(result.generated_at)}
            </small>
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
        {displayItems.map((item) => (
          <li key={`${title}-${item}`}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function FundamentalDisclosure({ symbol, fundamental, expanded, onToggle }) {
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
        <small>{toggleDate ? formatDate(toggleDate) : "待更新"}</small>
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

function BrokerTradingDisclosure({ brokerTrading, expanded, onToggle }) {
  return (
    <div className="broker-trading">
      <button className="broker-toggle" type="button" onClick={onToggle} aria-expanded={expanded}>
        <span>
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          主力進出
        </span>
        <small>{brokerTrading?.trade_date || "待更新"}</small>
      </button>
      {expanded && (
        <div className="broker-panel">
          {brokerTrading ? (
            <>
              <div className="broker-summary">
                <BrokerSummaryItem label="主力買賣" value={brokerTrading.main_net_volume} tone="net" />
                <BrokerSummaryItem label="主力買" value={brokerTrading.main_buy_volume} tone="buy" />
                <BrokerSummaryItem label="主力賣" value={brokerTrading.main_sell_volume} tone="sell" />
              </div>
              <div className="broker-rankings">
                <BrokerRanking title="買超券商" rows={brokerTrading.buy_brokers} />
                <BrokerRanking title="賣超券商" rows={brokerTrading.sell_brokers} />
              </div>
              <small className="broker-source">
                {brokerTrading.source} · {formatDate(brokerTrading.fetched_at)}
              </small>
            </>
          ) : (
            <div className="valuation-empty">
              <Wifi size={15} />
              主力進出待更新
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function BrokerSummaryItem({ label, value, tone }) {
  const toneClass = tone === "buy" ? "broker-buy" : tone === "sell" ? "broker-sell" : percentageToneClass(value);
  return (
    <div>
      <span>{label}</span>
      <strong className={toneClass}>{formatSignedNumber(value, 0)}</strong>
      <small>張</small>
    </div>
  );
}

function BrokerRanking({ title, rows = [] }) {
  const netLabel = title === "買超券商" ? "買超張數" : "賣超張數";
  return (
    <div className="broker-ranking">
      <strong>{title}</strong>
      {rows.length ? (
        <>
          <div className="broker-ranking-row broker-ranking-head">
            <span>排名</span>
            <span>券商</span>
            <span>買進</span>
            <span>賣出</span>
            <span>{netLabel}</span>
          </div>
          {rows.map((row) => (
            <div className="broker-ranking-row" key={`${title}-${row.rank}-${row.broker_name}`}>
              <span>{row.rank}</span>
              <span>{row.broker_name}</span>
              <span className="broker-trade-volume">{formatNumber(row.buy_volume, 0)}</span>
              <span className="broker-trade-volume">{formatNumber(row.sell_volume, 0)}</span>
              <span className={percentageToneClass(row.net_volume)}>{formatSignedNumber(row.net_volume, 0)}</span>
            </div>
          ))}
        </>
      ) : (
        <small>待更新</small>
      )}
    </div>
  );
}

function PositionEditor({ stock, disabled, onSavePosition, onClearPosition, compact = false }) {
  const [draftBuyPrice, setDraftBuyPrice] = useState(stock.position?.buy_price?.toString() || "");

  useEffect(() => {
    setDraftBuyPrice(stock.position?.buy_price?.toString() || "");
  }, [stock.position?.buy_price]);

  function commitBuyPrice() {
    const trimmed = draftBuyPrice.trim();
    const parsed = Number(trimmed);
    if (!trimmed || Number.isNaN(parsed) || parsed === stock.position?.buy_price) {
      return;
    }
    onSavePosition(trimmed);
  }

  return (
    <div className={`position-row${compact ? " compact" : ""}`}>
      <label className="buy-price-field">
        <span>成交均價</span>
        <input
          inputMode="decimal"
          value={draftBuyPrice}
          onChange={(event) => setDraftBuyPrice(event.target.value)}
          onBlur={commitBuyPrice}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.currentTarget.blur();
            }
          }}
          disabled={disabled}
        />
      </label>
      <button className="text-button sell-button" type="button" onClick={onClearPosition} disabled={disabled || !stock.position}>
        賣出
      </button>
    </div>
  );
}

const rootElement = document.getElementById("root");
window.__stockDashboardRoot ||= createRoot(rootElement);
window.__stockDashboardRoot.render(<App />);
