import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useDataQuality } from "./useDataQuality";
import { getDataQuality } from "../api/stocks";


vi.mock("../api/stocks", () => ({ getDataQuality: vi.fn() }));

describe("useDataQuality", () => {
  it("only requests quality details while the panel is open", async () => {
    getDataQuality.mockResolvedValue({ overall_status: "HEALTHY", items: [] });
    const { result, rerender } = renderHook(
      ({ open }) => useDataQuality("2330", open, 60),
      { initialProps: { open: false } },
    );
    expect(getDataQuality).not.toHaveBeenCalled();
    rerender({ open: true });
    await waitFor(() => expect(result.current.quality?.overall_status).toBe("HEALTHY"));
    expect(getDataQuality).toHaveBeenCalledTimes(1);
  });
});
