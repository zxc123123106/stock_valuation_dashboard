import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";


export function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity },
      mutations: { retry: false },
    },
  });
}


export function queryWrapper(queryClient = createTestQueryClient()) {
  return function QueryWrapper({ children }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}
