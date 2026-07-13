import { useCallback, useEffect, useState } from "react";

import { getDataQuality } from "../api/stocks";


export function useDataQuality(symbol, open, pollSeconds = 5) {
  const [quality, setQuality] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const loadQuality = useCallback(async ({ signal, showLoading = false } = {}) => {
    if (showLoading) setLoading(true);
    setError("");
    try {
      setQuality(await getDataQuality(symbol, signal));
    } catch (requestError) {
      if (requestError.name !== "AbortError") setError(requestError.message);
    } finally {
      if (!signal?.aborted && showLoading) setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    setQuality(null);
    setError("");
  }, [symbol]);

  useEffect(() => {
    if (!open) return undefined;
    const controller = new AbortController();
    loadQuality({ signal: controller.signal, showLoading: true });
    const intervalId = window.setInterval(() => loadQuality(), pollSeconds * 1000);
    return () => {
      controller.abort();
      window.clearInterval(intervalId);
    };
  }, [loadQuality, open, pollSeconds]);

  return { quality, loading, error, reload: loadQuality };
}
