import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useDashboardData } from "./useDashboardData";
import * as dashboardApi from "../api/dashboard";


vi.mock("../api/dashboard", () => ({
  getStocks: vi.fn(),
  getMetadata: vi.fn(),
  getRefreshStatus: vi.fn(),
  getBrokerSetting: vi.fn(),
  getWtxFutures: vi.fn(),
}));

function seedResponses() {
  dashboardApi.getStocks.mockResolvedValue([{ symbol: "2330", display_order: 10 }]);
  dashboardApi.getMetadata.mockResolvedValue({ api_version: "0.1.0" });
  dashboardApi.getRefreshStatus.mockResolvedValue({ status: "idle", symbols: [], queue_length: 0 });
  dashboardApi.getBrokerSetting.mockResolvedValue({ selected: { broker_id: "CATHAY" } });
  dashboardApi.getWtxFutures.mockResolvedValue({ symbol: "WTX&" });
}

describe("useDashboardData", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    seedResponses();
  });

  it("loads the dashboard and refreshes on focus", async () => {
    const reorderingRef = { current: false };
    const { result } = renderHook(() => useDashboardData({ pollSeconds: 60, reorderingRef }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.stocks[0].symbol).toBe("2330");
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(dashboardApi.getStocks).toHaveBeenCalledTimes(2));
  });

  it("deduplicates overlapping refresh calls", async () => {
    let resolveStocks;
    dashboardApi.getStocks.mockImplementationOnce(() => new Promise((resolve) => { resolveStocks = resolve; }));
    const reorderingRef = { current: false };
    const { result } = renderHook(() => useDashboardData({ pollSeconds: 60, reorderingRef }));
    await waitFor(() => expect(dashboardApi.getStocks).toHaveBeenCalledTimes(1));
    act(() => {
      result.current.loadData();
      result.current.loadData();
    });
    expect(dashboardApi.getStocks).toHaveBeenCalledTimes(1);
    await act(async () => resolveStocks([{ symbol: "0050", display_order: 10 }]));
    await waitFor(() => expect(result.current.stocks[0].symbol).toBe("0050"));
  });
});
