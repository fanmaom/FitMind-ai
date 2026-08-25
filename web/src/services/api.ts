const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000/api/v1";
const TOKEN_KEY = "fitness-agent-token";

export const getToken = () => typeof window === "undefined" ? "" : localStorage.getItem(TOKEN_KEY) ?? "";
export const setToken = (token: string) => localStorage.setItem(TOKEN_KEY, token);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}), ...init.headers },
  });
  if (!response.ok) throw new Error((await response.text()) || `请求失败 ${response.status}`);
  if (response.status === 204) return undefined as T;
  return response.json();
}

export { API_BASE };
