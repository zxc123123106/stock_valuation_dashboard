import { useCallback, useEffect, useRef, useState } from "react";

import {
  getBrokerSetting,
  getMetadata,
  getRefreshStatus,
  getStocks,
  getWtxFutures,
} from "../api/dashboard";


export function useDashboardData({ pollSeconds = 5, reorderingRef }) {
  const [stocks, setStocks] = useState([]);
  const [futuresData, setFuturesData] = useState(null);
  const [metadata, setMetadata] = useState(null);
  const [brokerSetting, setBrokerSetting] = useState(null);
  const [refreshStatus, setRefreshStatus] = useState({ status: "idle", symbols: [], queue_length: 0 });
  const [symbolInput, setSymbolInput] = useState("2330");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [now, setNow] = useState(() => new Date());
  const inflightRef = useRef(null);

  const loadData = useCallback(async ({ showLoading = false, silent = false } = {}) => {
    if (inflightRef.current) return inflightRef.current;
    if (showLoading) setLoading(true);
    if (!silent) setError("");

    const request = (async () => {
      try {
        const [nextStocks, nextMetadata, nextStatus, nextBroker, nextFutures] = await Promise.all([
          getStocks(),
          getMetadata().catch(() => null),
          getRefreshStatus(),
          getBrokerSetting(),
          getWtxFutures().catch(() => null),
        ]);
        if (!reorderingRef.current) setStocks(nextStocks);
        setMetadata(nextMetadata);
        setRefreshStatus(nextStatus);
        setBrokerSetting(nextBroker);
        setFuturesData(nextFutures);
      } catch (requestError) {
        if (!silent) setError(requestError.message);
      } finally {
        if (showLoading) setLoading(false);
        inflightRef.current = null;
      }
    })();
    inflightRef.current = request;
    return request;
  }, [reorderingRef]);

  useEffect(() => {
    loadData({ showLoading: true });
  }, [loadData]);

  useEffect(() => {
    const timer = window.setInterval(() => loadData({ silent: true }), pollSeconds * 1000);
    return () => window.clearInterval(timer);
  }, [loadData, pollSeconds]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const handleFocus = () => loadData({ silent: true });
    const handleVisibility = () => {
      if (document.visibilityState === "visible") handleFocus();
    };
    window.addEventListener("focus", handleFocus);
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      window.removeEventListener("focus", handleFocus);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [loadData]);

  return {
    stocks, setStocks, futuresData, metadata, brokerSetting, setBrokerSetting,
    refreshStatus, symbolInput, setSymbolInput, loading, error, setError,
    message, setMessage, now, loadData,
  };
}
