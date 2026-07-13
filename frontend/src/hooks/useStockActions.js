import { useCallback } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import {
  clearStockPosition,
  deleteStock as deleteStockRequest,
  refreshAllStocks,
  refreshStock,
  saveBrokerSetting,
  saveStockPosition,
} from "../api/stocks";
import { queryKeys, replaceDashboardStock, updateDashboardStocks } from "../api/queryKeys";


export function useStockActions({ setError, setMessage }) {
  const queryClient = useQueryClient();
  const refreshOneMutation = useMutation({ mutationFn: refreshStock });
  const refreshAllMutation = useMutation({ mutationFn: refreshAllStocks });
  const deleteMutation = useMutation({ mutationFn: deleteStockRequest });
  const savePositionMutation = useMutation({
    mutationFn: ({ symbol, buyPrice }) => saveStockPosition(symbol, buyPrice),
  });
  const clearPositionMutation = useMutation({ mutationFn: clearStockPosition });
  const brokerMutation = useMutation({ mutationFn: saveBrokerSetting });

  const invalidateDashboard = useCallback(
    () => queryClient.invalidateQueries({ queryKey: queryKeys.dashboard }),
    [queryClient],
  );

  const queueRefreshSymbol = useCallback(async (symbol) => {
    const normalized = symbol.trim();
    if (!normalized) {
      setError("請輸入股票代號");
      return;
    }
    setError(""); setMessage("");
    try {
      const result = await refreshOneMutation.mutateAsync(normalized);
      await invalidateDashboard();
      setMessage(`${result.symbol || normalized} 已排入背景更新`);
    } catch (error) {
      setError(error.message);
    }
  }, [invalidateDashboard, refreshOneMutation, setError, setMessage]);

  const queueRefreshAll = useCallback(async () => {
    setError(""); setMessage("");
    try {
      const result = await refreshAllMutation.mutateAsync();
      await invalidateDashboard();
      setMessage(result.symbols.length ? "全部數據已排入全量更新" : "目前沒有可更新的標的");
    } catch (error) {
      setError(error.message);
    }
  }, [invalidateDashboard, refreshAllMutation, setError, setMessage]);

  const deleteStock = useCallback(async (symbol) => {
    if (!window.confirm(`永久刪除 ${symbol}？這會從本機 SQLite 刪除標的與相關快取資料。`)) return;
    setError(""); setMessage("");
    try {
      await deleteMutation.mutateAsync(symbol);
      updateDashboardStocks(queryClient, (stocks) => stocks.filter((stock) => stock.symbol !== symbol));
      await invalidateDashboard();
      setMessage(`${symbol} 已從資料庫刪除`);
    } catch (error) {
      setError(error.message);
    }
  }, [deleteMutation, invalidateDashboard, queryClient, setError, setMessage]);

  const savePosition = useCallback(async (symbol, buyPrice) => {
    setError(""); setMessage("");
    try {
      const nextStock = await savePositionMutation.mutateAsync({ symbol, buyPrice });
      replaceDashboardStock(queryClient, nextStock);
      await invalidateDashboard();
      setMessage(`${symbol} 買入價已更新`);
    } catch (error) {
      setError(error.message);
    }
  }, [invalidateDashboard, queryClient, savePositionMutation, setError, setMessage]);

  const clearPosition = useCallback(async (symbol) => {
    setError(""); setMessage("");
    try {
      const nextStock = await clearPositionMutation.mutateAsync(symbol);
      replaceDashboardStock(queryClient, nextStock);
      await invalidateDashboard();
      setMessage(`${symbol} 已賣出，買入價已清除`);
    } catch (error) {
      setError(error.message);
    }
  }, [clearPositionMutation, invalidateDashboard, queryClient, setError, setMessage]);

  const updateBroker = useCallback(async (brokerId) => {
    setError(""); setMessage("");
    try {
      const nextSetting = await brokerMutation.mutateAsync(brokerId);
      queryClient.setQueryData(queryKeys.brokerSetting, nextSetting);
      await invalidateDashboard();
      setMessage(`券商已切換為 ${nextSetting.selected.name}`);
    } catch (error) {
      setError(error.message);
    }
  }, [brokerMutation, invalidateDashboard, queryClient, setError, setMessage]);

  return { queueRefreshSymbol, queueRefreshAll, deleteStock, savePosition, clearPosition, updateBroker };
}
