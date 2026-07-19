"""T1.8 perf-gate seed CLI: `python manage.py seed_perf_assets [--tenant-slug S]
[--count N] [--batch-size B]`.

**Must run as the migration/OWNER role** (`DATABASE_URL`, not
`APP_DATABASE_URL`) — see `apps.assets.perf_seed` module docstring for why
(disabling the `search_vector` triggers needs table-owner privilege). In
docker-compose, reuse the `migrate` one-off service, which already connects
as the owner:

    docker compose run --rm migrate python manage.py seed_perf_assets --count 12000

Idempotent-ish: catalog fixtures (categories/fields/locations/projects/tags)
are `get_or_create`d; re-running with the same `--tenant-slug` APPENDS `count`
more assets rather than erroring (fine for a perf corpus, where "at least
10k" is the requirement).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandParser

from apps.assets.perf_seed import seed_assets
from apps.tenancy.models import Tenant

DEFAULT_TENANT_SLUG = "perf-seed-lab"


class Command(BaseCommand):
    help = "Seed a 10k+ realistic Asset corpus for the T1.8 perf gate (F10)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--tenant-slug", default=DEFAULT_TENANT_SLUG)
        parser.add_argument("--tenant-name", default="Perf Seed Lab")
        parser.add_argument("--count", type=int, default=10_000)
        parser.add_argument("--batch-size", type=int, default=1_000)
        parser.add_argument("--rng-seed", type=int, default=42)

    def handle(self, *args, **options) -> None:
        tenant, created = Tenant.objects.get_or_create(
            slug=options["tenant_slug"], defaults={"name": options["tenant_name"]}
        )
        if created:
            self.stdout.write(f"Created tenant {tenant.slug!r} (id={tenant.id}).")
        else:
            self.stdout.write(f"Reusing existing tenant {tenant.slug!r} (id={tenant.id}).")

        result = seed_assets(
            tenant,
            count=options["count"],
            batch_size=options["batch_size"],
            rng_seed=options["rng_seed"],
        )

        self.stdout.write(self.style.SUCCESS("=== T1.8 perf seed complete ==="))
        self.stdout.write(f"tenant_id={result.tenant_id} slug={tenant.slug}")
        self.stdout.write(f"assets_created={result.assets_created}")
        self.stdout.write(f"field_values_created={result.field_values_created}")
        self.stdout.write(f"tag_links_created={result.tag_links_created}")
        self.stdout.write(f"load_seconds={result.load_seconds:.2f}")
        self.stdout.write(f"backfill_seconds={result.backfill_seconds:.2f}")
        self.stdout.write(f"vacuum_seconds={result.vacuum_seconds:.2f}")
        self.stdout.write(f"total_seconds={result.total_seconds:.2f}")
