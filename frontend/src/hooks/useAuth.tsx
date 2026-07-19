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

type AuthStatus = "loading" | "authenticated" | "unauthenticated";

interface AuthContextValue {
  status: AuthStatus;
  me: Me | null;
  login: (payload: LoginRequest) => Promise<void>;
  logout: () => Promise<void>;
  /** Re-fetch `/me` (e.g. after a permission-changing action elsewhere). */
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

/**
 * Loads `GET /me` once on mount to establish session state. A 401/403 means
 * "not logged in" — this is a normal, expected outcome (CLAUDE.md: never
 * treat a 403 as a bug), not an error to surface to the user; it just routes
 * them to Login.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [me, setMe] = useState<Me | null>(null);

  const loadMe = useCallback(async () => {
    try {
      const result = await api.me();
      setMe(result);
      setStatus("authenticated");
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        setMe(null);
        setStatus("unauthenticated");
        return;
      }
      // Any other failure (network, 5xx) — still land on "unauthenticated"
      // so the shell doesn't hang forever; Login will surface the real error
      // on the next explicit action.
      setMe(null);
      setStatus("unauthenticated");
    }
  }, []);

  useEffect(() => {
    void loadMe();
  }, [loadMe]);

  const login = useCallback(async (payload: LoginRequest) => {
    const result = await api.login(payload);
    setMe(result);
    setStatus("authenticated");
  }, []);

  const logout = useCallback(async () => {
    await api.logout();
    setMe(null);
    setStatus("unauthenticated");
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ status, me, login, logout, refresh: loadMe }),
    [status, me, login, logout, loadMe],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
