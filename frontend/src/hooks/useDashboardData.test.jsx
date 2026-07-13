import { act, renderHook, waitFor } from "@testing-library/react";
import { focusManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useDashboardData } from "./useDashboardData";
import * as dashboardApi from "../api/dashboard";
import { queryWrapper } from "../test/queryClient";


vi.mock("../api/dashboard", () => ({
  getDashboardSnapshot: vi.fn(),
  getBrokerSetting: vi.fn(),
  getWtxFutures: vi.fn(),
}));

function seedResponses() {
  dashboardApi.getDashboardSnapshot.mockResolvedValue({
    revision: "one",
    stocks: [{ symbol: "2330", display_order: 10 }],
    metadata: { api_version: "0.1.0" },
    refresh_status: { status: "idle", symbols: [], queue_length: 0 },
  });
  dashboardApi.getBrokerSetting.mockResolvedValue({ selected: { broker_id: "CATHAY" } });
  dashboardApi.getWtxFutures.mockResolvedValue({ symbol: "WTX&" });
}

describe("useDashboardData", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    seedResponses();
    focusManager.setFocused(true);
  });

  afterEach(() => {
    focusManager.setFocused(undefined);
    vi.useRealTimers();
  });

  it("loads one dashboard snapshot plus independent broker and WTX queries", async () => {
    const { result } = renderHook(
      () => useDashboardData({ pollSeconds: 60, futuresPollSeconds: 60 }),
      { wrapper: queryWrapper() },
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.stocks[0].symbol).toBe("2330");
    expect(dashboardApi.getDashboardSnapshot).toHaveBeenCalledTimes(1);
    expect(dashboardApi.getBrokerSetting).toHaveBeenCalledTimes(1);
    expect(dashboardApi.getWtxFutures).toHaveBeenCalledTimes(1);
  });

  it("refreshes dashboard and WTX once when the window regains focus", async () => {
    renderHook(
      () => useDashboardData({ pollSeconds: 60, futuresPollSeconds: 60 }),
      { wrapper: queryWrapper() },
    );
    await waitFor(() => expect(dashboardApi.getDashboardSnapshot).toHaveBeenCalled());
    const dashboardCalls = dashboardApi.getDashboardSnapshot.mock.calls.length;
    const futuresCalls = dashboardApi.getWtxFutures.mock.calls.length;
    const brokerCalls = dashboardApi.getBrokerSetting.mock.calls.length;
    act(() => focusManager.setFocused(false));
    act(() => focusManager.setFocused(true));
    await waitFor(() => expect(dashboardApi.getDashboardSnapshot).toHaveBeenCalledTimes(dashboardCalls + 1));
    expect(dashboardApi.getWtxFutures).toHaveBeenCalledTimes(futuresCalls + 1);
    expect(dashboardApi.getBrokerSetting).toHaveBeenCalledTimes(brokerCalls);
  });
});
