export function latestMetricTime(stocks) {
  const dates = stocks.map((stock) => stock.metric?.price_updated_at).filter(Boolean);
  if (!dates.length) return null;
  return dates.reduce((latest, value) => (new Date(value) > new Date(latest) ? value : latest));
}


export function applyDisplayOrder(stocks) {
  return stocks.map((stock, index) => ({ ...stock, display_order: (index + 1) * 10 }));
}


export function isPendingRefresh(state) {
  return ["queued", "running", "refreshing"].includes(state?.status);
}


export function isVisibleRefreshState(state, now) {
  if (!state) return false;
  if (["queued", "running", "refreshing", "failed", "retry_wait"].includes(state.status)) return true;
  return Boolean(
    state.status === "success" &&
    state.finished_at &&
    now.getTime() - new Date(state.finished_at).getTime() < 15000
  );
}
