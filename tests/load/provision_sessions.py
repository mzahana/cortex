"""T6.6 pre-test session provisioning.

**Why this exists (empirical finding, not a workaround around security):**
`POST /api/v1/auth/login` is throttled to **10/min per client IP**
(`config/settings/base.py DEFAULT_THROTTLE_RATES["login"] = "10/min"`,
T0.6) -- a deliberate anti-brute-force control. A real deployment's 30
concurrent lab members arrive from ~30 distinct IPs behind Cloudflare, so
they'd never collectively hit that per-IP limit. A load-generator host
issuing 30 near-simultaneous logins from ONE IP, however, hits it
immediately (confirmed while building this suite: ~82% of logins 429'd at
`-u 30 -r 6`) -- that's the auth throttle correctly doing its job, not a
target-endpoint problem, and re-running the throttled request in a loop
would just be attacking our own rate limiter instead of measuring
`docs/architecture.md` §4's list/search/detail/dashboard/checkout targets.

So: log in **sequentially, respecting the 10/min budget**, ONCE per
locust-simulated-user, *before* the timed run starts, and save each
resulting (sessionid, csrftoken) cookie pair to `results/sessions.json`.
`locustfile_read.py`/`locustfile_checkout.py` then inject one pre-provisioned
session per simulated user in `on_start` (no `POST /auth/login` call inside
the timed window at all) -- every SUBSEQUENT request in the actual load test
still carries a real, distinct, server-validated Django session cookie +
CSRF token through the full session-auth + CSRF + tenant-context + RBAC +
RLS stack; only the (out-of-band, not a T6.6 target) login round-trip itself
is pre-computed.

Usage:
    python3 provision_sessions.py --host http://localhost:8080 --count 30
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

TENANT_SLUG = "perf-seed-lab"
EMAIL_TEMPLATE = "loadtest-admin-{i:03d}@perf.test"
ADMIN_PASSWORD = "LoadTestPass123!"
CSRF_COOKIE_NAME = "cortex_csrftoken"
SESSION_COOKIE_NAME = "cortex_sessionid"
# Throttle is 10/min -- space logins out at 7s (~8.6/min) to stay comfortably
# under it even with clock-boundary jitter.
LOGIN_INTERVAL_SECONDS = 7.0


def provision_one(host: str, *, index: int) -> dict[str, str]:
    """Log in as the `index`-th distinct seeded load-test user (see
    `seed_load_test_admin`'s module docstring for why each simulated user
    MUST be a distinct `User` row, not a shared login: `UserRateThrottle`
    keys its 1000/min bucket by `request.user.pk`).
    """
    session = requests.Session()
    csrf_resp = session.get(f"{host}/api/v1/auth/csrf", timeout=10)
    csrf_resp.raise_for_status()
    token = session.cookies.get(CSRF_COOKIE_NAME)

    email = EMAIL_TEMPLATE.format(i=index)
    login_resp = session.post(
        f"{host}/api/v1/auth/login",
        json={"tenant": TENANT_SLUG, "email": email, "password": ADMIN_PASSWORD},
        headers={"X-CSRFToken": token, "Referer": host},
        timeout=10,
    )
    login_resp.raise_for_status()

    return {
        "sessionid": session.cookies.get(SESSION_COOKIE_NAME),
        "csrftoken": session.cookies.get(CSRF_COOKIE_NAME),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://localhost:8080")
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument(
        "--out", default=str(Path(__file__).parent / "results" / "sessions.json")
    )
    args = parser.parse_args()

    sessions: list[dict[str, str]] = []
    for i in range(args.count):
        sessions.append(provision_one(args.host, index=i))
        print(f"provisioned session {i + 1}/{args.count} (user {EMAIL_TEMPLATE.format(i=i)})")
        if i < args.count - 1:
            time.sleep(LOGIN_INTERVAL_SECONDS)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(sessions, indent=2))
    print(f"wrote {len(sessions)} sessions to {out_path}")


if __name__ == "__main__":
    main()
