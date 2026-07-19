/**
 * RFC-7807 (`application/problem+json`) error shape, matching
 * `backend/apps/common/errors.py` exactly:
 *
 *   { "type": "about:blank" | "https://.../problems/<slug>",
 *     "title": "<short machine-ish description>",
 *     "status": <http status code>,
 *     "detail": "<human-readable detail>",
 *     "errors": {...}        // only present for field-validation errors
 *     "retry_after": "<n>"   // only present when throttled (429)
 *   }
 */
export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail?: string;
  errors?: Record<string, unknown>;
  retry_after?: string;
}

/** Thrown by the API client for any non-2xx response. Always carries a
 * `ProblemDetails` (falling back to a synthesized one if the server didn't
 * actually return problem+json — e.g. a network error or an nginx-level
 * failure that never reached Django). */
export class ApiError extends Error {
  readonly problem: ProblemDetails;

  constructor(problem: ProblemDetails) {
    super(problem.detail || problem.title);
    this.name = "ApiError";
    this.problem = problem;
  }

  get status(): number {
    return this.problem.status;
  }

  /** True for the uniform 401 the login endpoint returns for any bad
   * tenant/email/password combination (never reveals which was wrong). */
  get isInvalidCredentials(): boolean {
    return this.status === 401;
  }

  /** True for the 429 returned on lockout or endpoint-level rate limiting. */
  get isRateLimited(): boolean {
    return this.status === 429;
  }

  /** True for the server-side 403 that gates an action the client couldn't
   * (or shouldn't) have hidden — a normal, handled outcome per CLAUDE.md,
   * never evidence of a client bug. */
  get isForbidden(): boolean {
    return this.status === 403;
  }
}

export function isProblemDetails(value: unknown): value is ProblemDetails {
  return (
    typeof value === "object" &&
    value !== null &&
    "status" in value &&
    "title" in value
  );
}
