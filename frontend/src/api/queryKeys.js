export const queryKeys = {
  dashboard: ["dashboard", "snapshot"],
  brokerSetting: ["settings", "broker"],
  futuresWtx: ["futures", "wtx"],
  dataQuality: (symbol) => ["stocks", symbol, "data-quality"],
  fundamentalTrends: (symbol, fetchedAt) => ["stocks", symbol, "fundamental-trends", fetchedAt || "pending"],
  technicalAnalysis: (symbol, updatedAt) => ["stocks", symbol, "technical-analysis", updatedAt || "pending"],
  aiAnalysis: (symbol) => ["stocks", symbol, "ai-analysis"],
  dataManagementStatus: ["data-management", "status"],
  databaseBackups: ["data-management", "backups"],
};


export function updateDashboardStocks(queryClient, updater) {
  queryClient.setQueryData(queryKeys.dashboard, (current) => {
    if (!current) return current;
    const nextStocks = typeof updater === "function" ? updater(current.stocks || []) : updater;
    return { ...current, stocks: nextStocks };
  });
}


export function replaceDashboardStock(queryClient, nextStock) {
  updateDashboardStocks(
    queryClient,
    (stocks) => stocks.map((stock) => stock.symbol === nextStock.symbol ? nextStock : stock),
  );
}
