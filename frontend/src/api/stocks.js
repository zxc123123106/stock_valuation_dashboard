import { requestJson } from "./client";


export const refreshStock = (symbol) => requestJson(`/api/stocks/${symbol}/refresh`, { method: "POST" });
export const refreshAllStocks = () => requestJson("/api/stocks/refresh", { method: "POST" });
export const deleteStock = (symbol) => requestJson(`/api/stocks/${symbol}`, { method: "DELETE" });
export const reorderStocks = (symbols) => requestJson("/api/stocks/reorder", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ symbols }),
});
export const saveStockPosition = (symbol, buyPrice) => requestJson(`/api/stocks/${symbol}/position`, {
  method: "PUT",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ buy_price: Number(buyPrice) }),
});
export const clearStockPosition = (symbol) => requestJson(`/api/stocks/${symbol}/position`, { method: "DELETE" });
export const saveBrokerSetting = (brokerId) => requestJson("/api/settings/broker", {
  method: "PUT",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ broker_id: brokerId }),
});
export const getTechnicalAnalysis = (symbol, signal) => requestJson(
  `/api/stocks/${symbol}/technical-analysis?limit=120`,
  { signal },
);
export const getFundamentalTrends = (symbol, signal) => requestJson(
  `/api/stocks/${symbol}/fundamentals/trends`,
  { signal },
);
export const getDataQuality = (symbol, signal) => requestJson(`/api/stocks/${symbol}/data-quality`, { signal });
