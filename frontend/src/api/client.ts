/**
 * The single typed API client module (CLAUDE.md: "Typed API layer... components
 * never build URLs ad hoc"). Base path `/api/v1`, same-origin via nginx.
 * Session-cookie auth; CSRF token echoed on writes; RFC-7807 errors normalized
 * into `ApiError`.
 */
import type { LoginRequest, Me } from "./types";
import { ApiError, type ProblemDetails } from "./problem";

const API_BASE = "/api/v1";

/** Must match `CSRF_COOKIE_NAME` in `backend/config/settings/base.py`. */
const CSRF_COOKIE_NAME = "lms_csrftoken";
/** Must match `CSRF_HEADER_NAME` (`HTTP_X_CSRFTOKEN`) in the same file — the
 * actual wire header is `X-CSRFToken`. */
const CSRF_HEADER_NAME = "X-CSRFToken";

const WRITE_METHODS = new Set(["POST", "PATCH", "PUT", "DELETE"]);

function readCookie(name: string): string | null {
  const match = document.cookie
    .split("; ")
    .find((row) => row.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.split("=").slice(1).join("=")) : null;
}

/** `GET /api/v1/auth/csrf` — unauthenticated, no body. Plants the
 * JS-readable `lms_csrftoken` cookie so the client has a token to echo back
 * on the very first write (login), which has no session yet to rely on. */
async function ensureCsrfCookie(): Promise<void> {
  if (readCookie(CSRF_COOKIE_NAME)) return;
  await request<{ detail: string }>("/auth/csrf", { method: "GET" });
}

async function toApiError(response: Response): Promise<ApiError> {
  const contentType = response.headers.get("Content-Type") || "";
  if (contentType.includes("application/problem+json") || contentType.includes("application/json")) {
    try {
      const body = (await response.json()) as Partial<ProblemDetails>;
      return new ApiError({
        type: body.type ?? "about:blank",
        title: body.title ?? response.statusText,
        status: body.status ?? response.status,
        detail: body.detail,
        errors: body.errors,
        retry_after: body.retry_after ?? response.headers.get("Retry-After") ?? undefined,
      });
    } catch {
      // fall through to the generic fallback below
    }
  }
  return new ApiError({
    type: "about:blank",
    title: response.statusText || "Request failed",
    status: response.status,
    detail: `Request failed with status ${response.status}.`,
  });
}

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers);

  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  if (WRITE_METHODS.has(method)) {
    const token = readCookie(CSRF_COOKIE_NAME);
    if (token) headers.set(CSRF_HEADER_NAME, token);
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    method,
    headers,
    credentials: "include", // same-origin session cookie (lms_sessionid)
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });

  if (!response.ok) {
    throw await toApiError(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export const api = {
  /** `POST /api/v1/auth/login` — plants the CSRF cookie first if needed
   * (fresh session, nothing to echo yet), then logs in. On success the
   * response body is the same shape as `GET /me` and a session cookie is set. */
  async login(payload: LoginRequest): Promise<Me> {
    await ensureCsrfCookie();
    return request<Me>("/auth/login", { method: "POST", body: payload });
  },

  /** `POST /api/v1/auth/logout` — authenticated, CSRF-enforced via the
   * existing session. `204 No Content`. */
  async logout(): Promise<void> {
    await request<void>("/auth/logout", { method: "POST" });
  },

  /** `GET /api/v1/me` — authenticated. Throws `ApiError` (401/403) if not. */
  async me(): Promise<Me> {
    return request<Me>("/me", { method: "GET" });
  },
};

export { ApiError };
