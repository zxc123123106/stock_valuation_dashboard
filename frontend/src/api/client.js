export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

const etagCache = new Map();


export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}


export async function parseError(response) {
  try {
    const body = await response.json();
    return body.detail || `API ${response.status}`;
  } catch {
    return `API ${response.status}`;
  }
}


export async function requestJson(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, options);
  if (!response.ok) {
    throw new ApiError(await parseError(response), response.status);
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}


export async function requestEtagJson(path, { cachedData, skipEtag = false, ...options } = {}) {
  const url = `${API_BASE_URL}${path}`;
  const cached = etagCache.get(url);
  const headers = new Headers(options.headers || {});
  let requestUrl = url;
  if (!skipEtag && cached?.etag) {
    const crossOrigin = typeof window !== "undefined" && new URL(url).origin !== window.location.origin;
    if (crossOrigin) {
      const revision = cached.etag.replace(/^W\//, "").replaceAll('"', "");
      requestUrl += `${requestUrl.includes("?") ? "&" : "?"}revision=${encodeURIComponent(revision)}`;
    } else {
      headers.set("If-None-Match", cached.etag);
    }
  }

  const response = await fetch(requestUrl, { ...options, headers });
  if (response.status === 304) {
    const reusable = cachedData ?? cached?.data;
    if (reusable !== undefined) return reusable;
    return requestEtagJson(path, { ...options, skipEtag: true });
  }
  if (!response.ok) {
    throw new ApiError(await parseError(response), response.status);
  }
  const data = await response.json();
  etagCache.set(url, { etag: response.headers.get("ETag"), data });
  return data;
}


export function clearEtagCache() {
  etagCache.clear();
}
