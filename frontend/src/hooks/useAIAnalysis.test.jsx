import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { hasRunningAiAnalysis, mergeAiAnalysisResponse, useAIAnalysis } from "./useAIAnalysis";
import * as aiApi from "../api/ai";
import { queryWrapper } from "../test/queryClient";


vi.mock("../api/ai", () => ({
  getLatestAIAnalysis: vi.fn(),
  generateAIAnalysis: vi.fn(),
  submitAIAnalysisFeedback: vi.fn(),
}));

describe("useAIAnalysis", () => {
  it("keeps backend running state after the panel closes and blocks duplicate generation", async () => {
    aiApi.getLatestAIAnalysis.mockResolvedValue({
      analyses: { unheld: null, held: null },
      rule_based: { unheld: null, held: null },
      running: { unheld: true, held: true },
    });
    const stock = { symbol: "4958", position: { buy_price: 500 } };
    const { result, rerender } = renderHook(
      ({ open }) => useAIAnalysis(stock, open, 60),
      { initialProps: { open: true }, wrapper: queryWrapper() },
    );
    await waitFor(() => expect(result.current.running).toBe(true));
    rerender({ open: false });
    await act(async () => result.current.generate());
    expect(result.current.running).toBe(true);
    expect(aiApi.generateAIAnalysis).not.toHaveBeenCalled();
  });

  it("preserves an existing mode when a partial latest response arrives", () => {
    const merged = mergeAiAnalysisResponse(
      { analyses: { unheld: { id: 1 }, held: { id: 2 } }, rule_based: {} },
      { analyses: { unheld: null, held: { id: 3 } }, rule_based: {} },
    );
    expect(merged.analyses.unheld.id).toBe(1);
    expect(merged.analyses.held.id).toBe(3);
  });

  it("treats a persisted queued run as running after the panel is reopened", () => {
    expect(hasRunningAiAnalysis({ running: {}, run: { status: "queued" } })).toBe(true);
    expect(hasRunningAiAnalysis({ running: {}, run: { status: "success" } })).toBe(false);
  });

  it("keeps snapshot metadata while merging partial mode updates", () => {
    const merged = mergeAiAnalysisResponse(
      {
        analyses: { unheld: { id: 1 }, held: null },
        rule_based: {},
        run: { id: 7, status: "running" },
        data_as_of: [{ category: "QUOTE" }],
      },
      { analyses: { unheld: null, held: null }, rule_based: {}, stale_items: ["行情：使用快取"] },
    );
    expect(merged.run.id).toBe(7);
    expect(merged.data_as_of).toEqual([{ category: "QUOTE" }]);
    expect(merged.stale_items).toEqual(["行情：使用快取"]);
  });
});
