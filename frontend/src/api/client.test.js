import { afterEach, describe, expect, it, vi } from "vitest";

import { parseError, requestJson } from "./client";


describe("API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses a FastAPI detail message", async () => {
    const response = new Response(JSON.stringify({ detail: "資料不存在" }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
    await expect(parseError(response)).resolves.toBe("資料不存在");
  });

  it("throws a normalized error for unsuccessful requests", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("bad gateway", { status: 502 })));
    await expect(requestJson("/api/test")).rejects.toThrow("API 502");
  });
});
