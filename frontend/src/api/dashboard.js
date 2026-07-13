import { requestEtagJson, requestJson } from "./client";


export function getDashboardSnapshot(options) {
  return requestEtagJson("/api/dashboard/snapshot", options);
}


export function getStocks(options) {
  return requestJson("/api/stocks", options);
}

export function getMetadata(options) {
  return requestJson("/api/metadata", options);
}

export function getRefreshStatus(options) {
  return requestJson("/api/refresh/status", options);
}

export function getBrokerSetting(options) {
  return requestJson("/api/settings/broker", options);
}

export function getWtxFutures(options) {
  return requestJson("/api/futures/wtx", options);
}
