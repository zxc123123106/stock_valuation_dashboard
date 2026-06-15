import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  Trash2,
  Wifi,
} from "lucide-react";
import "./styles.css";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";
const POLL_SECONDS = 5;
const BACKGROUND_REFRESH_SECONDS = 60;

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
  open: "盤中更新中",
  pre_open: "開盤外停止",
  post_close: "開盤外停止",
  weekend: "週末停止",
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

function formatOptionalSignedPercent(value, digits = 2) {
  if (value === null || value === undefined) {
    return "—";
  }
  return `${formatSignedNumber(value, digits)}%`;
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
      const [stockResponse, metadataResponse, statusResponse, brokerResponse] = await Promise.all([
        fetch(`${API_BASE_URL}/api/stocks`),
        fetch(`${API_BASE_URL}/api/metadata`),
        fetch(`${API_BASE_URL}/api/refresh/status`),
        fetch(`${API_BASE_URL}/api/settings/broker`),
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
  const valuationCount = stocks.reduce((total, stock) => total + stock.valuations.length, 0);
  const refreshWindow = refreshStatus.refresh_window || metadata?.refresh_window || "平日 09:00-14:00 Asia/Taipei";
  const marketSessionLabel = MARKET_SESSION_LABELS[refreshStatus.market_session] || "開盤外停止";
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
          <small>股價每 {metadata?.refresh_interval_seconds || BACKGROUND_REFRESH_SECONDS} 秒 · PE/EPS 每日 09:00 · 下次 {formatCountdown(refreshStatus.next_auto_refresh_at, now)}</small>
        </div>
        <div className="metric">
          <span>目前更新</span>
          <strong>{currentRefreshText}</strong>
          <small>前端每 {POLL_SECONDS} 秒讀快取 · {metadata?.data_source || "SQLite 快取資料"}</small>
        </div>
        <div className="metric">
          <span>最近資料</span>
          <strong>{formatDate(latestDataTime)}</strong>
          <small>{stocks.length} 檔標的 · {valuationCount} 筆估值 · 收盤補抓 {formatDate(lastCloseVerification)}</small>
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
    style,
  },
  ref,
) {
  const metric = stock.metric;
  const pendingRefresh = isPendingRefresh(refreshState);
  const statusLabel = REFRESH_STATUS_LABELS[refreshState?.status] || refreshState?.status;
  const isEtf = stock.asset_type === "ETF";
  const [brokerTradingExpanded, setBrokerTradingExpanded] = useState(false);
  const [technicalExpanded, setTechnicalExpanded] = useState(false);

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
            <div className="quote-current-row pe-current-row">
              <span className="metric-label">本益比</span>
              <strong>{formatOptionalNumber(metric?.current_pe)}</strong>
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

      {isEtf && (
        <div className="valuation-table broker-only">
          <BrokerTradingDisclosure
            brokerTrading={stock.broker_trading}
            expanded={brokerTradingExpanded}
            onToggle={() => setBrokerTradingExpanded((current) => !current)}
          />
        </div>
      )}

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
              <React.Fragment key={`${stock.symbol}-${valuation.eps_type}`}>
                <div className="valuation-row">
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
                {valuation.eps_type === "LAST_YEAR" && (
                  <BrokerTradingDisclosure
                    brokerTrading={stock.broker_trading}
                    expanded={brokerTradingExpanded}
                    onToggle={() => setBrokerTradingExpanded((current) => !current)}
                  />
                )}
              </React.Fragment>
            ))
          ) : (
            <div className="valuation-empty">
              <Wifi size={15} />
              背景快取建立中
            </div>
          )}
        </div>
      )}

      <TechnicalAnalysisDisclosure
        symbol={stock.symbol}
        metricUpdatedAt={metric?.price_updated_at}
        expanded={technicalExpanded}
        onToggle={() => setTechnicalExpanded((current) => !current)}
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
        <small>日線 · MA20</small>
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
        rightOffset: 4,
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

    const ma20Series = chart.addSeries(LineSeries, {
      color: "#e2c879",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: false,
    });
    ma20Series.setData(
      candles
        .filter((candle) => candle.ma20 !== null && candle.ma20 !== undefined)
        .map((candle) => ({ time: candle.date, value: candle.ma20 })),
    );

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

    chart.subscribeCrosshairMove(handleCrosshairMove);
    chart.timeScale().fitContent();
    return () => {
      chart.unsubscribeCrosshairMove(handleCrosshairMove);
      chart.remove();
    };
  }, [candles, latestCandle]);

  const summary = selectedCandle || latestCandle;
  return (
    <div className="technical-chart-shell">
      <div className="technical-summary">
        <div className="technical-summary-date">
          <span>日期</span>
          <strong>{formatTradingDate(summary?.date)}</strong>
          {summary?.is_provisional && <em>暫定 K 棒</em>}
        </div>
        <TechnicalSummaryValue label="開" value={summary?.open} />
        <TechnicalSummaryValue label="高" value={summary?.high} />
        <TechnicalSummaryValue label="低" value={summary?.low} />
        <TechnicalSummaryValue label="收" value={summary?.close} />
        <TechnicalSummaryValue label="MA20" value={summary?.ma20} accent />
      </div>
      <div className="technical-chart" ref={containerRef} />
    </div>
  );
}

function TechnicalSummaryValue({ label, value, accent = false }) {
  return (
    <div>
      <span>{label}</span>
      <strong className={accent ? "ma20-value" : ""}>{formatOptionalNumber(value)}</strong>
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
