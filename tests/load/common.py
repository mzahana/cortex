"""Shared session/CSRF helpers for the T6.6 locustfiles.

Reuses the REAL session-auth + CSRF cookies the SPA's login flow produces
(`docs/api-and-ui.md` "Auth & identity", `apps.accounts.api.LoginView`)
rather than bypassing auth -- RBAC/RLS overhead on every measured request is
part of what T6.6 is measuring. The actual `POST /auth/login` call itself is
made ONCE per simulated user, OUT OF BAND, by `provision_sessions.py` before
the timed run (see that module's docstring for why: the login endpoint's
10/min-per-IP throttle makes 30 near-simultaneous logins from one
load-generator host unrepresentative of 30 real users arriving from distinct
IPs). Each locust `HttpUser.on_start()` below just injects one
pre-provisioned (sessionid, csrftoken) cookie pair -- every request made
DURING the timed run still carries a real, distinct, server-validated
session through the full session-auth + CSRF + tenant-context + RBAC + RLS
stack.
"""

from __future__ import annotations

import json
from pathlib import Path

CSRF_HEADER = "X-CSRFToken"
# `config/settings/base.py`: CSRF_COOKIE_NAME = "cortex_csrftoken" / SESSION_
# COOKIE_NAME = "cortex_sessionid" (not Django's "csrftoken"/"sessionid"
# defaults) -- must match exactly or cookie lookups below silently return
# `None` and every request 401s/403s.
CSRF_COOKIE_NAME = "cortex_csrftoken"
SESSION_COOKIE_NAME = "cortex_sessionid"

SESSIONS_FILE = Path(__file__).parent / "results" / "sessions.json"

_sessions_cache: list[dict[str, str]] | None = None


def _load_sessions() -> list[dict[str, str]]:
    global _sessions_cache
    if _sessions_cache is None:
        if not SESSIONS_FILE.exists():
            raise RuntimeError(
                f"{SESSIONS_FILE} not found -- run `python3 provision_sessions.py` "
                "first (see its module docstring)."
            )
        _sessions_cache = json.loads(SESSIONS_FILE.read_text())
    return _sessions_cache


_next_session_index = 0


def apply_session(client, *, index: int | None = None) -> None:
    """Inject the `index`-th pre-provisioned session's cookies into `client`
    (a locust `HttpSession`) -- no HTTP call made here at all. `index=None`
    round-robins through the pool via a module-level counter, so N locust
    users spread evenly across the N pre-provisioned sessions regardless of
    how many users are spawned.
    """
    global _next_session_index
    sessions = _load_sessions()
    if index is None:
        index = _next_session_index % len(sessions)
        _next_session_index += 1
    creds = sessions[index % len(sessions)]
    client.cookies.set(SESSION_COOKIE_NAME, creds["sessionid"])
    client.cookies.set(CSRF_COOKIE_NAME, creds["csrftoken"])


def csrf_headers(client) -> dict[str, str]:
    """Header dict for a state-changing request (e.g. `POST /checkouts/`).
    Django's CSRF check compares this header against the CURRENT cookie
    value, which stays fixed for the lifetime of a pre-provisioned session
    (no further login/rotation happens during the timed run).
    """
    token = client.cookies.get(CSRF_COOKIE_NAME)
    return {CSRF_HEADER: token, "Referer": client.base_url}
