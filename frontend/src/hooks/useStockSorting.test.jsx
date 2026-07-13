import { act, renderHook, waitFor } from "@testing-library/react";
import { useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { useStockSorting } from "./useStockSorting";
import { reorderStocks } from "../api/stocks";


vi.mock("../api/stocks", () => ({ reorderStocks: vi.fn() }));

function useHarness() {
  const [stocks, setStocks] = useState([
    { symbol: "2330", display_order: 10 },
    { symbol: "0050", display_order: 20 },
  ]);
  const [error, setError] = useState("");
  const [, setMessage] = useState("");
  const reorderingRef = useRef(false);
  const sorting = useStockSorting({ stocks, setStocks, setError, setMessage, reorderingRef });
  return { ...sorting, stocks, error };
}


describe("useStockSorting", () => {
  it("rolls an optimistic arrow move back when persistence fails", async () => {
    reorderStocks.mockRejectedValueOnce(new Error("排序儲存失敗"));
    const { result } = renderHook(() => useHarness());
    act(() => result.current.moveStock("0050", -1));
    await waitFor(() => expect(result.current.error).toBe("排序儲存失敗"));
    expect(result.current.stocks.map((stock) => stock.symbol)).toEqual(["2330", "0050"]);
  });
});
