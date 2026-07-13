import { requestJson } from "./client";


export const getLatestAIAnalysis = (symbol, signal) => requestJson(
  `/api/stocks/${symbol}/ai-analysis/latest`,
  { signal },
);
export const generateAIAnalysis = (symbol, forceRefresh = true) => requestJson(`/api/stocks/${symbol}/ai-analysis`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ force_refresh: forceRefresh }),
});
export const submitAIAnalysisFeedback = (symbol, mode, payload) => requestJson(
  `/api/stocks/${symbol}/ai-analysis/${mode}/feedback`,
  {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  },
);
