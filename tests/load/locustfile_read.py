"""T6.6 read-path load test: list / search / detail / dashboard against a
prod-profile stack seeded with 50k+ assets (`docs/tasks/M6-import-export-
deploy.md` T6.6).

Kept in its OWN locustfile, separate from `locustfile_checkout.py`
(`CheckoutUser`), specifically so the dashboard-summary target can be
measured against a **warm cache** without a concurrent write task
invalidating it mid-run (see `apps.dashboard.cache`'s per-tenant version-
counter invalidation: any checkout/checkin bumps it for the WHOLE tenant,
not just the mutated project's scope) -- see this repo's T6.6 task
instructions ("make sure your test actually warms the cache first ... and
doesn't inadvertently invalidate it between requests").

Run (from the repo root, against the `docker-compose.loadtest.yml` overlay
published on host port 8080 -- see `tests/load/README.md`):

    locust -f tests/load/locustfile_read.py --host http://localhost:8080 \\
      --headless -u 30 -r 10 -t 3m --csv tests/load/results/read

All four `AssetReadUser` users share the SAME login (tenant-wide Admin), so
they also share the SAME dashboard cache key (`apps.dashboard.cache.
summary_cache_key` is per-tenant-per-RBAC-SCOPE, not per-user) -- the
`on_test_start` warm-up below primes it once for the whole run.
"""

from __future__ import annotations

import random

from locust import HttpUser, between, events, task

from common import apply_session

# Realistic search vocabulary drawn straight from `apps.assets.perf_seed`'s
# own seeded corpus (category names, manufacturer names, GPU/board/FC model
# names) -- exercises BOTH the FTS (`websearch_to_tsquery`) and pg_trgm
# fuzzy-match arms of `AssetSearchFilter` with real hits, not empty-result
# noise.
SEARCH_TERMS = [
    "RTX 4090",
    "Jetson Orin",
    "Pixhawk",
    "GPU Workstation",
    "Edge Device",
    "Drone Electronics Kit",
    "NVIDIA",
    "DJI",
    "Fluke",
    "Ubuntu 24.04",
    "SN-GPU",
    "Betaflight",
]


@events.test_start.add_listener
def _warm_dashboard_cache(environment, **kwargs):
    """Prime `GET /dashboard/summary`'s Redis cache entry ONCE before the
    timed run starts, logged in-band as `dashboard: warm-up (setup)` (not
    counted against the `dashboard: summary` target stat below) -- exactly
    the "warm the cache first" step the T6.6 task instructions call for.
    """
    import locust.env

    assert isinstance(environment, locust.env.Environment)
    if environment.host is None:
        return
    from locust.clients import HttpSession

    client = HttpSession(
        base_url=environment.host,
        request_event=environment.events.request,
        user=None,
    )
    apply_session(client, index=0)
    resp = client.get("/api/v1/dashboard/summary", name="dashboard: warm-up (setup)")
    resp.raise_for_status()


class AssetReadUser(HttpUser):
    wait_time = between(0.2, 1.0)

    def on_start(self) -> None:
        apply_session(self.client)
        # Each simulated user's own detail-lookup pool: one page (100 ids,
        # the max `BoundedPageNumberPagination.max_page_size`) of PLAIN
        # (unfiltered) assets, offset by this user's own index so the 30
        # concurrent users spread across ~30 distinct pages of the 50k+
        # corpus rather than all hammering page 1's same 100 rows.
        page = (self._user_index() % 400) + 1
        resp = self.client.get(
            f"/api/v1/assets/?page={page}&page_size=100",
            name="assets: list (detail-pool setup)",
        )
        resp.raise_for_status()
        results = resp.json()["results"]
        self.detail_ids = [row["id"] for row in results] or [1]

    _index_counter = 0

    def _user_index(self) -> int:
        AssetReadUser._index_counter += 1
        return AssetReadUser._index_counter

    @task(40)
    def list_assets(self) -> None:
        page = random.randint(1, 400)
        self.client.get(f"/api/v1/assets/?page={page}&page_size=25", name="assets: list")

    @task(30)
    def search_assets(self) -> None:
        term = random.choice(SEARCH_TERMS)
        self.client.get(f"/api/v1/assets/?search={term}&page_size=25", name="assets: search")

    @task(20)
    def asset_detail(self) -> None:
        asset_id = random.choice(self.detail_ids)
        self.client.get(f"/api/v1/assets/{asset_id}/", name="assets: detail")

    @task(10)
    def dashboard_summary(self) -> None:
        self.client.get("/api/v1/dashboard/summary", name="dashboard: summary")
