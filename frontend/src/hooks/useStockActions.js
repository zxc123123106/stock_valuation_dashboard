import { useCallback } from "react";

import {
  clearStockPosition,
  deleteStock as deleteStockRequest,
  refreshAllStocks,
  refreshStock,
  saveBrokerSetting,
  saveStockPosition,
} from "../api/stocks";


export function useStockActions({ loadData, setStocks, setBrokerSetting, setError, setMessage }) {
  const replaceStock = useCallback((nextStock) => {
    setStocks((current) => current.map((stock) => stock.symbol === nextStock.symbol ? nextStock : stock));
  }, [setStocks]);

  const queueRefreshSymbol = useCallback(async (symbol) => {
    const normalized = symbol.trim();
    if (!normalized) {
      setError("請輸入股票代號");
      return;
    }
    setError(""); setMessage("");
    try {
      const result = await refreshStock(normalized);
      await loadData();
      setMessage(`${result.symbol || normalized} 已排入背景更新`);
    } catch (error) {
      setError(error.message);
    }
  }, [loadData, setError, setMessage]);

  const queueRefreshAll = useCallback(async () => {
    setError(""); setMessage("");
    try {
      const result = await refreshAllStocks();
      await loadData();
      setMessage(result.symbols.length ? "全部數據已排入全量更新" : "目前沒有可更新的標的");
    } catch (error) {
      setError(error.message);
    }
  }, [loadData, setError, setMessage]);

  const deleteStock = useCallback(async (symbol) => {
    if (!window.confirm(`永久刪除 ${symbol}？這會從本機 SQLite 刪除標的與相關快取資料。`)) return;
    setError(""); setMessage("");
    try {
      await deleteStockRequest(symbol);
      await loadData();
      setMessage(`${symbol} 已從資料庫刪除`);
    } catch (error) {
      setError(error.message);
    }
  }, [loadData, setError, setMessage]);

  const savePosition = useCallback(async (symbol, buyPrice) => {
    setError(""); setMessage("");
    try {
      replaceStock(await saveStockPosition(symbol, buyPrice));
      setMessage(`${symbol} 買入價已更新`);
    } catch (error) {
      setError(error.message);
    }
  }, [replaceStock, setError, setMessage]);

  const clearPosition = useCallback(async (symbol) => {
    setError(""); setMessage("");
    try {
      replaceStock(await clearStockPosition(symbol));
      setMessage(`${symbol} 已賣出，買入價已清除`);
    } catch (error) {
      setError(error.message);
    }
  }, [replaceStock, setError, setMessage]);

  const updateBroker = useCallback(async (brokerId) => {
    setError(""); setMessage("");
    try {
      const nextSetting = await saveBrokerSetting(brokerId);
      setBrokerSetting(nextSetting);
      await loadData({ silent: true });
      setMessage(`券商已切換為 ${nextSetting.selected.name}`);
    } catch (error) {
      setError(error.message);
    }
  }, [loadData, setBrokerSetting, setError, setMessage]);

  return { queueRefreshSymbol, queueRefreshAll, deleteStock, savePosition, clearPosition, updateBroker };
}
