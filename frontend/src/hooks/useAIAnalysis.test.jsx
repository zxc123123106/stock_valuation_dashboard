import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useAIAnalysis } from "./useAIAnalysis";


describe("useAIAnalysis", () => {
  it("keeps pending state by stock when a panel closes and reopens", () => {
    const { result } = renderHook(() => useAIAnalysis());
    act(() => result.current.setPending("4958", true));
    expect(result.current.pendingBySymbol).toEqual({ "4958": true });
    act(() => result.current.setPending("4958", false));
    expect(result.current.pendingBySymbol).toEqual({});
  });
});
