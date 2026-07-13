import { act, renderHook, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { useStockSorting } from "./useStockSorting";
import { reorderStocks } from "../api/stocks";
import { queryKeys } from "../api/queryKeys";
import { createTestQueryClient, queryWrapper } from "../test/queryClient";


vi.mock("../api/stocks", () => ({ reorderStocks: vi.fn() }));

function useHarness(stocks) {
  const [error, setError] = useState("");
  const [, setMessage] = useState("");
  const sorting = useStockSorting({ stocks, setError, setMessage });
  return { ...sorting, error };
}


describe("useStockSorting", () => {
  it("rolls an optimistic arrow move back when persistence fails", async () => {
    const stocks = [
      { symbol: "2330", display_order: 10 },
      { symbol: "0050", display_order: 20 },
    ];
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.dashboard, { stocks, metadata: {}, refresh_status: {} });
    reorderStocks.mockRejectedValueOnce(new Error("排序儲存失敗"));
    const { result } = renderHook(() => useHarness(stocks), { wrapper: queryWrapper(queryClient) });
    act(() => result.current.moveStock("0050", -1));
    await waitFor(() => expect(result.current.error).toBe("排序儲存失敗"));
    expect(queryClient.getQueryData(queryKeys.dashboard).stocks.map((stock) => stock.symbol)).toEqual([
      "2330",
      "0050",
    ]);
  });
});
