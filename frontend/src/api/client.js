export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";


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
    throw new Error(await parseError(response));
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}
