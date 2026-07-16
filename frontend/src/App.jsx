import React, { useState } from "react";

import { FuturesTrackerCard } from "./components/futures/FuturesWidget";
import { DashboardHeader } from "./components/dashboard/DashboardHeader";
import { DashboardSummary } from "./components/dashboard/DashboardSummary";
import { DashboardToolbar } from "./components/dashboard/DashboardToolbar";
import { StockList } from "./components/stocks/StockList";
import { DataManagementPanel } from "./components/dataManagement/DataManagementPanel";
import { useDashboardData } from "./hooks/useDashboardData";
import { useStockActions } from "./hooks/useStockActions";
import { useStockSorting } from "./hooks/useStockSorting";
import { AlertCircle } from "lucide-react";

const POLL_SECONDS = 5;
export default function App() {
  const [dataManagementOpen, setDataManagementOpen] = useState(false);
  const {
    stocks,
    futuresData,
    metadata,
    brokerSetting,
    refreshStatus,
    symbolInput,
    setSymbolInput,
    loading,
    error,
    setError,
    message,
    setMessage,
    now,
  } = useDashboardData({ pollSeconds: POLL_SECONDS, futuresPollSeconds: 10 });
  const {
    queueRefreshSymbol,
    queueRefreshAll,
    deleteStock,
    savePosition,
    clearPosition,
    updateBroker,
  } = useStockActions({
    setError,
    setMessage,
  });
  const {
    sensors,
    orderedStocks,
    reordering,
    activeDragSymbol,
    setActiveDragSymbol,
    registerStockCard,
    moveStock,
    handleDragStart,
    handleDragEnd,
  } = useStockSorting({
    stocks,
    setError,
    setMessage,
  });

  return (
    <main className="shell">
      <DashboardHeader onRefreshAll={queueRefreshAll} />
      <DashboardSummary
        metadata={metadata}
        refreshStatus={refreshStatus}
        stocks={stocks}
        now={now}
        pollSeconds={POLL_SECONDS}
      />
      <DashboardToolbar
        symbol={symbolInput}
        onSymbolChange={setSymbolInput}
        brokerSetting={brokerSetting}
        onBrokerChange={updateBroker}
        onSubmit={queueRefreshSymbol}
        onOpenDataManagement={() => setDataManagementOpen(true)}
      />

      <DataManagementPanel
        open={dataManagementOpen}
        onClose={() => setDataManagementOpen(false)}
        onMessage={setMessage}
        onError={setError}
      />

      {error && (
        <div className="notice error">
          <AlertCircle size={18} />
          <span>資料暫時無法更新：{error}</span>
        </div>
      )}
      {message && !error && <div className="notice success">{message}</div>}

      <FuturesTrackerCard data={futuresData} />
      <StockList
        loading={loading}
        stocks={orderedStocks}
        refreshStatus={refreshStatus}
        now={now}
        sensors={sensors}
        reordering={reordering}
        activeDragSymbol={activeDragSymbol}
        setActiveDragSymbol={setActiveDragSymbol}
        registerStockCard={registerStockCard}
        moveStock={moveStock}
        handleDragStart={handleDragStart}
        handleDragEnd={handleDragEnd}
        queueRefreshSymbol={queueRefreshSymbol}
        deleteStock={deleteStock}
        savePosition={savePosition}
        clearPosition={clearPosition}
      />

      <footer>
        <span>本看板僅用於資料整理與估值比較，不構成任何投資建議。</span>
        <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">Charts by TradingView</a>
      </footer>
    </main>
  );

}



















