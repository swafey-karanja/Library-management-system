import axios from "axios";

/**
 * Axios client for the Django REST API (see core/urls.py in the backend —
 * everything is mounted under /api, with JWT auth under /api/v1/auth).
 *
 * NEXT_PUBLIC_API_URL is read from .env.local so it's easy to point at a
 * different backend (e.g. a staging server) without touching code.
 */
export const api = axios.create({
  baseURL: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000",
  headers: {
    "Content-Type": "application/json",
  },
});

const ACCESS_TOKEN_KEY = "access_token";
const REFRESH_TOKEN_KEY = "refresh_token";

export function getAccessToken() {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(ACCESS_TOKEN_KEY);
}

export function setTokens(access: string, refresh?: string) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(ACCESS_TOKEN_KEY, access);
  if (refresh) window.localStorage.setItem(REFRESH_TOKEN_KEY, refresh);
}

export function clearTokens() {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(ACCESS_TOKEN_KEY);
  window.localStorage.removeItem(REFRESH_TOKEN_KEY);
}

// Attach "Authorization: Bearer <token>" to every request, matching the
// backend's SIMPLE_JWT AUTH_HEADER_TYPES config.
api.interceptors.request.use((config) => {
  const token = getAccessToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// On a 401, try to refresh the access token once using the refresh token,
// then retry the original request. Falls back to clearing tokens.
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config;
    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true;
      const refreshToken =
        typeof window !== "undefined"
          ? window.localStorage.getItem(REFRESH_TOKEN_KEY)
          : null;

      if (refreshToken) {
        try {
          const { data } = await axios.post(
            `${api.defaults.baseURL}/api/v1/auth/token/refresh/`,
            { refresh: refreshToken }
          );
          setTokens(data.access);
          originalRequest.headers.Authorization = `Bearer ${data.access}`;
          return api(originalRequest);
        } catch {
          clearTokens();
        }
      }
    }
    return Promise.reject(error);
  }
);

/**
 * Django REST Framework errors show up in a couple of different shapes
 * depending on the endpoint:
 *   - hand-written views (e.g. login) return {"detail": "..."}
 *   - serializer validation errors return {"field_name": ["message", ...]}
 * This normalizes either into one readable string for a toast.
 */
export function getErrorMessage(error: unknown): string {
  if (typeof error === "object" && error !== null && "response" in error) {
    const response = (error as { response?: { data?: unknown } }).response;
    const data = response?.data;

    if (typeof data === "string") return data;

    if (data && typeof data === "object") {
      const obj = data as Record<string, unknown>;
      if (typeof obj.detail === "string") return obj.detail;

      // Fall back to the first field's first error message,
      // e.g. {"email": ["A user with this email already exists."]}
      const firstKey = Object.keys(obj)[0];
      if (firstKey) {
        const value = obj[firstKey];
        const message = Array.isArray(value) ? value[0] : value;
        if (typeof message === "string") {
          return firstKey === "detail" || firstKey === "non_field_errors"
            ? message
            : `${firstKey}: ${message}`;
        }
      }
    }
  }

  return "Something went wrong. Please try again.";
}
