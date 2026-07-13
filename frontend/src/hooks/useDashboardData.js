import { useEffect, useState } from "react";
import { useIsMutating, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getBrokerSetting,
  getDashboardSnapshot,
  getWtxFutures,
} from "../api/dashboard";
import { queryKeys } from "../api/queryKeys";


export function useDashboardData({ pollSeconds = 5, futuresPollSeconds = 10 } = {}) {
  const queryClient = useQueryClient();
  const reordering = useIsMutating({ mutationKey: ["stocks", "reorder"] }) > 0;
  const [symbolInput, setSymbolInput] = useState("2330");
  const [actionError, setError] = useState("");
  const [message, setMessage] = useState("");
  const [now, setNow] = useState(() => new Date());

  const dashboardQuery = useQuery({
    queryKey: queryKeys.dashboard,
    queryFn: ({ signal }) => getDashboardSnapshot({
      signal,
      cachedData: queryClient.getQueryData(queryKeys.dashboard),
    }),
    refetchInterval: reordering ? false : pollSeconds * 1000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: "always",
    staleTime: Math.max(0, pollSeconds * 1000 - 1000),
    placeholderData: (previous) => previous,
  });
  const brokerQuery = useQuery({
    queryKey: queryKeys.brokerSetting,
    queryFn: ({ signal }) => getBrokerSetting({ signal }),
    staleTime: Infinity,
  });
  const futuresQuery = useQuery({
    queryKey: queryKeys.futuresWtx,
    queryFn: ({ signal }) => getWtxFutures({ signal }),
    refetchInterval: futuresPollSeconds * 1000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: "always",
    staleTime: Math.max(0, futuresPollSeconds * 1000 - 1000),
    placeholderData: (previous) => previous,
  });

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const snapshot = dashboardQuery.data;
  const queryError = dashboardQuery.error || brokerQuery.error;
  return {
    stocks: snapshot?.stocks || [],
    futuresData: futuresQuery.data || null,
    metadata: snapshot?.metadata || null,
    brokerSetting: brokerQuery.data || null,
    refreshStatus: snapshot?.refresh_status || { status: "idle", symbols: [], queue_length: 0 },
    symbolInput,
    setSymbolInput,
    loading: dashboardQuery.isPending,
    error: actionError || queryError?.message || "",
    setError,
    message,
    setMessage,
    now,
  };
}
