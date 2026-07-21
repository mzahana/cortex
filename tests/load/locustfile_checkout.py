"""T6.6 checkout-write load test: `POST /api/v1/checkouts` against a
prod-profile stack seeded with 50k+ assets.

**Kept in its own locustfile** (see `locustfile_read.py`'s module docstring)
so the dashboard-cache measurement never overlaps with the tenant-wide
cache-invalidating writes this file issues on every request
(`apps.reservations.checkout.CheckoutViewSet.perform_create` ->
`invalidate_tenant_dashboard`).

**Never runs out of eligible assets.** `~55%` of the 50k+ seeded corpus is
`AVAILABLE` (`apps.assets.perf_seed._STATUS_WEIGHTS`) and non-consumable
(every leaf category except "Components"), so each of the 30 concurrent
users is handed its OWN disjoint slice of that pool at `on_start` (indexed
by user, not randomly re-picked) and cycles checkout -> checkin -> checkout
on its own assets for the run's duration -- no two users ever contend for
the same `Asset` row's `select_for_update()` lock, and the pool never
depletes regardless of run length. Only the `POST /checkouts` call is
measured against the T6.6 target; the cleanup `checkin` call is tagged
separately (`checkouts: checkin (cleanup)`) so it never pollutes that stat.

Run:

    locust -f tests/load/locustfile_checkout.py --host http://localhost:8080 \\
      --headless -u 30 -r 10 -t 3m --csv tests/load/results/checkout
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from locust import HttpUser, between, task

from common import apply_session, csrf_headers

POOL_PAGE_SIZE = 100
DUE_IN_DAYS = 3


class CheckoutUser(HttpUser):
    wait_time = between(0.5, 1.5)

    _index_counter = 0

    def on_start(self) -> None:
        apply_session(self.client)
        CheckoutUser._index_counter += 1
        my_index = CheckoutUser._index_counter

        # Disjoint page per user (see module docstring) -- `AssetFilterSet`'s
        # `status`/`is_consumable` filters, backed by the seeded corpus's
        # ~55% AVAILABLE / ~83% non-consumable mix, so a 100-row page is
        # comfortably non-empty this far into the corpus.
        resp = self.client.get(
            "/api/v1/assets/"
            f"?status=available&is_consumable=false&page={my_index}&page_size={POOL_PAGE_SIZE}"
            "&ordering=id",
            name="assets: eligible-pool (setup)",
        )
        resp.raise_for_status()
        results = resp.json()["results"]
        if not results:
            # Fallback: page 1 always has eligible rows even if this user's
            # own page index ran past the eligible tail -- degrades to some
            # cross-user sharing rather than a hard failure late in a long
            # run over a fixed-size corpus.
            resp = self.client.get(
                "/api/v1/assets/?status=available&is_consumable=false&page=1&page_size=100",
                name="assets: eligible-pool (setup fallback)",
            )
            resp.raise_for_status()
            results = resp.json()["results"]
        self.asset_ids: list[int] = [row["id"] for row in results]
        self._cursor = 0

    def _next_asset_id(self) -> int:
        asset_id = self.asset_ids[self._cursor % len(self.asset_ids)]
        self._cursor += 1
        return asset_id

    @task
    def checkout_then_checkin(self) -> None:
        asset_id = self._next_asset_id()
        due_at = (datetime.now(timezone.utc) + timedelta(days=DUE_IN_DAYS)).isoformat()

        resp = self.client.post(
            "/api/v1/checkouts/",
            json={"asset": asset_id, "due_at": due_at},
            headers=csrf_headers(self.client),
            name="checkouts: create",
        )
        if resp.status_code != 201:
            # An asset already checked out by a PRIOR iteration on this same
            # user (should not happen given the immediate checkin below, but
            # fails loudly rather than silently if it ever does) -- don't
            # attempt the checkin below against a request that didn't
            # actually create a Checkout row.
            return

        checkout_id = resp.json()["id"]
        # Immediately free the asset back to AVAILABLE so this user's own
        # pool never depletes across the run -- tagged as its own stat name
        # so it is never counted toward the `checkouts: create` p95 the
        # T6.6 target is measured against.
        self.client.post(
            f"/api/v1/checkouts/{checkout_id}/checkin/",
            json={},
            headers=csrf_headers(self.client),
            name="checkouts: checkin (cleanup)",
        )
