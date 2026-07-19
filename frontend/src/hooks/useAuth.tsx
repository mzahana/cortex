import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api, ApiError } from "../api/client";
import type { LoginRequest, Me } from "../api/types";

/** `error` is distinct from `unauthenticated`: it means the request never got
 * a real auth answer at all (network failure, backend unreachable, 5xx) —
 * carried M0 hardening (T1.5 note 6). Treating that the same as "not logged
 * in" would silently bounce a user with a perfectly valid session to Login
 * whenever the backend hiccups, which looks like a logout bug. */
type AuthStatus = "loading" | "authenticated" | "unauthenticated" | "error";

interface AuthContextValue {
  status: AuthStatus;
  me: Me | null;
  /** Set only when `status === "error"` — a human-readable reason the last
   * `/me` call failed to reach a real auth decision (not a 401/403). */
  error: string | null;
  login: (payload: LoginRequest) => Promise<void>;
  logout: () => Promise<void>;
  /** Re-fetch `/me` (e.g. after a permission-changing action elsewhere, or a
   * user-triggered retry from the "backend unreachable" state). */
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

/**
 * Loads `GET /me` once on mount to establish session state. A 401/403 means
 * "not logged in" — this is a normal, expected outcome (CLAUDE.md: never
 * treat a 403 as a bug), not an error to surface to the user; it just routes
 * them to Login. Any other failure (network error, 5xx, backend unreachable)
 * is a distinct `"error"` state — see `AuthStatus` doc comment.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadMe = useCallback(async () => {
    try {
      const result = await api.me();
      setMe(result);
      setError(null);
      setStatus("authenticated");
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        setMe(null);
        setError(null);
        setStatus("unauthenticated");
        return;
      }
      setMe(null);
      setError(
        err instanceof ApiError
          ? err.problem.detail || err.problem.title
          : "Unable to reach the server. Check your connection and try again.",
      );
      setStatus("error");
    }
  }, []);

  useEffect(() => {
    void loadMe();
  }, [loadMe]);

  const login = useCallback(async (payload: LoginRequest) => {
    const result = await api.login(payload);
    setMe(result);
    setError(null);
    setStatus("authenticated");
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      // Best-effort: even if the logout request itself fails (network,
      // already-expired session, ...) we still want the client to drop its
      // local session state rather than leave the user stuck on a screen
      // that thinks they're authenticated when the button they pressed said
      // otherwise.
    } finally {
      setMe(null);
      setError(null);
      setStatus("unauthenticated");
    }
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ status, me, error, login, logout, refresh: loadMe }),
    [status, me, error, login, logout, loadMe],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
