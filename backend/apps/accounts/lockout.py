"""Per-account login backoff (T0.6; revised after code-review Findings #1/#2).

Two independent, deliberately different-scoped counters:

- **HARD LOCK**, keyed `(tenant, email, client_ip)`: after `MAX_FAILURES`
  wrong passwords **from the same IP** against the same `(tenant, email)`,
  that `(account, ip)` pair is locked for `LOCKOUT_SECONDS`. This is what
  actually blocks a brute-force grind.

  Keying the hard lock on the attacker's own IP (not on the account alone)
  closes **Finding #1**: with the account-only key this replaces, anyone who
  merely knew a victim's `tenant` + `email` could lock the victim out of
  their own account with 5 wrong guesses, repeatably, from anywhere — an
  unauthenticated denial-of-service on a real user, with no credential of
  theirs required. Scoping the lock to `(tenant, email, ip)` means an
  attacker can only ever lock themselves out of guessing *that* account from
  *that* IP; the legitimate account owner, logging in from their own usual
  IP, is completely unaffected. A distributed attacker rotating IPs to dodge
  this still has to grind past DRF's per-IP `ScopedRateThrottle` (10/min,
  throttle scope `"login"`) on every new source address — that throttle is
  the layer that actually rate-limits a rotating attacker; see
  `apps.accounts.api.LoginView`.

- **SOFT** per-account counter, keyed `(tenant, email)` only, with no lock
  attached — purely a cross-IP signal ("this account is being guessed at
  from several different addresses"), counted but never enforced in M0.
  Exposed via `soft_failure_count()` for a future admin alert/notification
  (M1+ — e.g. `notify.self`/audit surface); it never blocks a login by
  itself, so it cannot be used as a victim-DoS vector.

**Best-effort guarantee, documented (Finding #2).** Both counters live in the
shared Redis **cache** (`django_redis`, `maxmemory-policy allkeys-lru`, see
`docker/redis.conf`) alongside the session store and DRF throttle counters.
Under memory pressure Redis can evict either counter's key early, silently
resetting the count/lock — this module makes NO durability guarantee, only a
best-effort one, which is judged acceptable for M0 (an internal lab tool, not
an internet-facing target with a realistic incentive to grind an account
through the eviction path). Deliberately **not** moved to a dedicated
non-evicting store for M0: `maxmemory-policy` is a single, server-wide Redis
setting (`docker/redis.conf`), not selectable per logical DB index, so
pointing these keys at a different `REDIS_URL` db number would not actually
change their eviction risk. A real fix would need either a second,
non-evicting Redis instance or a persistent (Postgres) `LoginFailure` table
— deferred unless real-world eviction pressure shows up in practice; flagged
for M1/devops-engineer rather than solved speculatively here.
"""

from __future__ import annotations

from django.core.cache import cache

MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60  # 15 minutes
FAILURE_WINDOW_SECONDS = 15 * 60


def _hard_key(tenant_slug: str, email: str, client_ip: str) -> str:
    return f"login:fail:{tenant_slug}:{email.strip().lower()}:{client_ip}"


def _soft_key(tenant_slug: str, email: str) -> str:
    return f"login:fail:acct:{tenant_slug}:{email.strip().lower()}"


def is_locked(tenant_slug: str, email: str, client_ip: str) -> bool:
    """Only ever true for the (account, IP) pair that generated the
    failures -- never blocks the account from a different IP."""
    return cache.get(_hard_key(tenant_slug, email, client_ip), 0) >= MAX_FAILURES


def register_failure(tenant_slug: str, email: str, client_ip: str) -> int:
    """Bump both counters. Returns the (tenant, email, ip) count -- what
    `is_locked` checks against `MAX_FAILURES` on the next attempt."""
    hard_key = _hard_key(tenant_slug, email, client_ip)
    count = cache.get(hard_key, 0) + 1
    # Once locked, keep the lockout window fixed at LOCKOUT_SECONDS from the
    # failure that tripped it rather than letting further attempts against a
    # locked (account, ip) pair extend it indefinitely.
    timeout = LOCKOUT_SECONDS if count >= MAX_FAILURES else FAILURE_WINDOW_SECONDS
    cache.set(hard_key, count, timeout=timeout)

    soft_key = _soft_key(tenant_slug, email)
    soft_count = cache.get(soft_key, 0) + 1
    cache.set(soft_key, soft_count, timeout=FAILURE_WINDOW_SECONDS)

    return count


def soft_failure_count(tenant_slug: str, email: str) -> int:
    """Cross-IP failure signal for this account -- informational only, never
    enforced by any login path in M0. Reserved for a future admin alert."""
    return cache.get(_soft_key(tenant_slug, email), 0)


def clear_failures(tenant_slug: str, email: str, client_ip: str) -> None:
    cache.delete(_hard_key(tenant_slug, email, client_ip))
    cache.delete(_soft_key(tenant_slug, email))
