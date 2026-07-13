import { describe, expect, it } from "vitest";

import { applyDisplayOrder, isPendingRefresh, isVisibleRefreshState, latestMetricTime } from "./stocks";


describe("stock utilities", () => {
  it("assigns stable display-order increments", () => {
    expect(applyDisplayOrder([{ symbol: "2330" }, { symbol: "0050" }])).toEqual([
      { symbol: "2330", display_order: 10 },
      { symbol: "0050", display_order: 20 },
    ]);
  });

  it("finds the latest quote timestamp", () => {
    expect(latestMetricTime([
      { metric: { price_updated_at: "2026-07-13T01:00:00Z" } },
      { metric: { price_updated_at: "2026-07-13T02:00:00Z" } },
    ])).toBe("2026-07-13T02:00:00Z");
  });

  it("keeps transient refresh states visible", () => {
    expect(isPendingRefresh({ status: "running" })).toBe(true);
    expect(isVisibleRefreshState({ status: "failed" }, new Date())).toBe(true);
  });
});
