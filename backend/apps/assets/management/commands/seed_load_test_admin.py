"""T6.6 load-test fixture: `python manage.py seed_load_test_admin
[--tenant-slug S] [--count N] [--password P]`.

Creates (idempotent, get_or_create-based, same pattern as
`apps.accounts.management.commands.seed_t0_6`) `--count` DISTINCT tenant-wide
**Admin** users in the perf-seed tenant that `seed_perf_assets` (T1.8)
already populated with 10k-50k `Asset` rows, so `tests/load/`'s locustfiles
have real session-auth credentials to log in with (`asset.view` +
`checkout.manage` tenant-wide -- see `docs/rbac.md`'s ADMIN_PERMISSIONS)
rather than bypassing auth/RBAC/RLS, which the T6.6 task instructions
explicitly require ("reuse the real login flow ... since RBAC/RLS overhead
is part of what's being measured").

**Why `--count` DISTINCT users, not one shared login (empirical T6.6
finding):** `rest_framework.throttling.UserRateThrottle` (`config/settings/
base.py`: `"user": "1000/min"`) keys its bucket by **`request.user.pk`**, not
by session. A first draft of this suite logged all 30 simulated concurrent
locust users in as the SAME single Admin account (30 sessions, 1 user row)
-- every one of those sessions shared ONE 1000/min throttle bucket, so the
aggregate test traffic across all 30 "users" hit `429 Too Many Requests`
well below the real per-endpoint capacity being measured. A real deployment's
30 concurrent lab members are 30 distinct `User` rows, each with its own
1000/min bucket, so this throttle is a non-issue for them -- distinct seeded
users here matches that reality instead of artificially self-throttling the
test.

Must run on a connection that can see the tenant at all -- the RLS-subject
`cortex_app` role works fine here (unlike `seed_perf_assets`, this does not
touch the trigger-disabled bulk-insert path), but for consistency with the
rest of the load-test seed pipeline this is documented to run via the same
`migrate` (owner-role) one-off service `seed_perf_assets` already requires:

    docker compose run --rm migrate python manage.py seed_load_test_admin --count 30
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandParser

from apps.accounts.models import User
from apps.rbac.models import Membership, Role
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.tenancy.context import tenant_context
from apps.tenancy.models import Tenant

DEFAULT_TENANT_SLUG = "perf-seed-lab"
EMAIL_TEMPLATE = "loadtest-admin-{i:03d}@perf.test"
# Dev/load-test-only credential -- this tenant/user only ever exists in a
# local or CI-throwaway stack seeded by `seed_perf_assets` (T1.8), never a
# real deployment (same class of fixture as `seed_t0_6.DEV_PASSWORD`).
DEFAULT_PASSWORD = "LoadTestPass123!"


class Command(BaseCommand):
    help = "Seed N distinct tenant-wide Admin users in the perf-seed tenant for tests/load/."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--tenant-slug", default=DEFAULT_TENANT_SLUG)
        parser.add_argument("--count", type=int, default=30)
        parser.add_argument("--password", default=DEFAULT_PASSWORD)

    def handle(self, *args, **options) -> None:
        tenant_slug = options["tenant_slug"]
        count = options["count"]
        password = options["password"]

        try:
            tenant = Tenant.objects.get(slug=tenant_slug)
        except Tenant.DoesNotExist as exc:
            raise SystemExit(
                f"Tenant {tenant_slug!r} does not exist -- run "
                f"`manage.py seed_perf_assets --tenant-slug {tenant_slug}` first."
            ) from exc

        created_count = 0
        with tenant_context(tenant.id):
            admin_role = Role.all_objects.get(tenant=tenant, key=ROLE_ADMIN)
            for i in range(count):
                email = EMAIL_TEMPLATE.format(i=i)
                user, created = User.all_objects.get_or_create(
                    tenant=tenant, email=email, defaults={"name": f"Load Test Admin {i:03d}"}
                )
                user.set_password(password)
                user.is_active = True
                user.save(update_fields=["password", "is_active"])
                created_count += int(created)

                # Every `User` gets an automatic tenant-wide Member membership
                # from `apps.rbac.signals.assign_default_membership` on
                # insert (see `MembershipFactory`'s docstring for the same
                # rule) -- upgrade it to Admin in place instead of creating a
                # second one.
                membership = Membership.all_objects.get(user=user, project__isnull=True)
                if membership.role_id != admin_role.id:
                    membership.role = admin_role
                    membership.save(update_fields=["role"])

        self.stdout.write(self.style.SUCCESS("=== load-test admin seed complete ==="))
        self.stdout.write(
            f"tenant_slug={tenant_slug} count={count} newly_created={created_count} "
            f"password={password}"
        )
        self.stdout.write(
            f"emails={EMAIL_TEMPLATE.format(i=0)}..{EMAIL_TEMPLATE.format(i=count - 1)}"
        )
