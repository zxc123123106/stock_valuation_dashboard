import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "./api/client";


export function shouldRetryRequest(failureCount, error) {
  if (failureCount >= 2) return false;
  return !(error instanceof ApiError) || error.status >= 500;
}


export function createDashboardQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetryRequest,
        refetchOnWindowFocus: true,
        refetchOnReconnect: true,
        refetchIntervalInBackground: false,
      },
      mutations: {
        retry: false,
      },
    },
  });
}


export const queryClient = createDashboardQueryClient();
