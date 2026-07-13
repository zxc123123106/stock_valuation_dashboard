import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, clearEtagCache, parseError, requestEtagJson, requestJson } from "./client";
import { shouldRetryRequest } from "../queryClient";


describe("API client", () => {
  afterEach(() => {
    clearEtagCache();
    vi.unstubAllGlobals();
  });

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

  it("reuses cached JSON when the server returns 304", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ revision: "one" }), {
        status: 200,
        headers: { "Content-Type": "application/json", ETag: '"one"' },
      }))
      .mockResolvedValueOnce(new Response(null, { status: 304, headers: { ETag: '"one"' } }));
    vi.stubGlobal("fetch", fetchMock);

    const first = await requestEtagJson("/api/dashboard/snapshot");
    const second = await requestEtagJson("/api/dashboard/snapshot", { cachedData: first });

    expect(second).toBe(first);
    expect(fetchMock.mock.calls[1][0]).toContain("revision=one");
    expect(fetchMock.mock.calls[1][1].headers.get("If-None-Match")).toBeNull();
  });

  it("only retries network and server errors, at most twice", () => {
    expect(shouldRetryRequest(0, new ApiError("bad request", 400))).toBe(false);
    expect(shouldRetryRequest(0, new ApiError("bad gateway", 502))).toBe(true);
    expect(shouldRetryRequest(1, new TypeError("network failed"))).toBe(true);
    expect(shouldRetryRequest(2, new ApiError("bad gateway", 502))).toBe(false);
  });
});
