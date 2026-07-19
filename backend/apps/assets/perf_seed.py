"""T1.8 — reusable 10k+ asset seed for the perf gate (F10).

Used by BOTH the `seed_perf_assets` management command (real docker-compose
runs, driven as the migration/OWNER role `cortex`) AND directly by pytest
perf tests (`apps/assets/tests/test_perf_10k.py`) — pytest-django's own DB
connection is ALSO the owner role (see `backend/conftest.py`'s module
docstring, "Trap 1"), so the exact same code path that disables/re-enables
the `search_vector` triggers via raw `ALTER TABLE ... DISABLE/ENABLE TRIGGER`
works unmodified in both contexts. Running this as the RLS-subject
`cortex_app` role will fail loudly with a Postgres permission error (only the
table owner/superuser may disable a trigger) — that is intentional: it is
cheaper and clearer to fail hard than to silently skip the disable and pay
the pathological per-row trigger cost T1.3 already flagged.

## Q7 (open question) — the custom-field defaults seeded here

`docs/tasks/M1-asset-registry.md`'s Q7 ("confirm the must-have custom fields
per category") is unanswered. This seed uses a documented, reasonable
default set (flagged for the real answer once available) so custom-field
JSONB values populate `search_vector` (weight C) across the corpus:

- **GPU Workstations** (Compute > GPU Workstations): gpu_model(text),
  vram_gb(int), cpu(text), ram_gb(int), os(text), hostname(text).
- **Edge Devices** (Compute > Edge Devices): board_model(text), ram_gb(int),
  os(text), hostname(text).
- **Drone Electronics** (Drone Kit > Drone Electronics): flight_controller
  (text), battery_mah(int), motor_kv(int), frame_size_in(float).
- **Components** (top-level, consumable): unit_cost(float), package_qty(int).
- **Tools** (top-level): calibration_interval_days(int).
- **Instruments** (top-level, requires_calibration): measurement_range(text),
  accuracy(text).

## The T1.3 perf trap this exists to avoid

`assets_asset_field_value`'s and `assets_tag_link`'s AFTER triggers each fire
a full `assets_compute_search_vector()` recompute (two correlated
sub-selects) for their parent asset on every child row inserted — inserting
N custom-field-values naively bulk-loaded costs O(N) full recomputes instead
of one. This module DISABLES those triggers (plus the `assets_asset` BEFORE
trigger, which would otherwise recompute a — still cheap but pointless —
name/serial-only vector on every one of the 10k+ `Asset` inserts) around the
whole bulk load, then does exactly ONE set-based backfill afterward using
the SAME `assets_compute_search_vector()` function the triggers call — so
the resulting vectors are byte-identical to what the triggers would have
produced row-by-row, just computed in one statement instead of thousands.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from django.db import connection, transaction
from django.utils import timezone

from apps.catalog.models import Category, CustomFieldDef, Location, Tag
from apps.projects.models import Project
from apps.tenancy.models import Tenant

from .models import Asset, AssetFieldValue, TagLink

# --- Trigger disable/enable (owner-only) ------------------------------------

_ASSET_TRIGGERS = [
    ("assets_asset", "assets_asset_search_vector_biu"),
    ("assets_asset_field_value", "assets_afv_search_vector_aiud"),
    ("assets_tag_link", "assets_tag_link_search_vector_aid"),
]

_BACKFILL_SQL = (
    "UPDATE assets_asset SET search_vector = "
    "assets_compute_search_vector(id, name, serial_number) WHERE tenant_id = %s;"
)


def _set_triggers(enabled: bool) -> None:
    verb = "ENABLE" if enabled else "DISABLE"
    with connection.cursor() as cur:
        for table, trigger in _ASSET_TRIGGERS:
            cur.execute(f"ALTER TABLE {table} {verb} TRIGGER {trigger};")


class triggers_disabled:  # noqa: N801 - context manager used as a verb
    """Disable the three `search_vector`-maintaining triggers around a bulk
    load, unconditionally re-enabling them on the way out (even on error) so
    a failed seed never leaves the DB with live rows and dead triggers.
    """

    def __enter__(self) -> None:
        _set_triggers(enabled=False)

    def __exit__(self, *exc_info: object) -> None:
        _set_triggers(enabled=True)


# --- Catalog fixtures (categories, custom fields, locations, projects, tags) --

# (parent_key | None, name, default_is_consumable, requires_approval, requires_calibration)
_CATEGORY_TREE: list[tuple[str | None, str, bool, bool, bool]] = [
    (None, "Compute", False, False, False),
    ("Compute", "GPU Workstations", False, False, False),
    ("Compute", "Edge Devices", False, False, False),
    (None, "Drone Kit", False, True, False),
    ("Drone Kit", "Drone Electronics", False, True, False),
    (None, "Components", True, False, False),
    (None, "Tools", False, False, False),
    (None, "Instruments", False, False, True),
]

# category name -> [(key, label, data_type, unit, enum_options, required, order)]
_FIELD_DEFS: dict[str, list[tuple[str, str, str, str, list, bool, int]]] = {
    "GPU Workstations": [
        ("gpu_model", "GPU model", "text", "", [], True, 0),
        ("vram_gb", "VRAM", "int", "GB", [], True, 1),
        ("cpu", "CPU", "text", "", [], False, 2),
        ("ram_gb", "RAM", "int", "GB", [], False, 3),
        ("os", "OS", "text", "", [], False, 4),
        ("hostname", "Hostname", "text", "", [], False, 5),
    ],
    "Edge Devices": [
        ("board_model", "Board model", "text", "", [], True, 0),
        ("ram_gb", "RAM", "int", "GB", [], False, 1),
        ("os", "OS", "text", "", [], False, 2),
        ("hostname", "Hostname", "text", "", [], False, 3),
    ],
    "Drone Electronics": [
        ("flight_controller", "Flight controller", "text", "", [], True, 0),
        ("battery_mah", "Battery capacity", "int", "mAh", [], False, 1),
        ("motor_kv", "Motor KV", "int", "kv", [], False, 2),
        ("frame_size_in", "Frame size", "float", "in", [], False, 3),
    ],
    "Components": [
        ("unit_cost", "Unit cost", "float", "USD", [], False, 0),
        ("package_qty", "Package quantity", "int", "", [], False, 1),
    ],
    "Tools": [
        ("calibration_interval_days", "Calibration interval", "int", "days", [], False, 0),
    ],
    "Instruments": [
        ("measurement_range", "Measurement range", "text", "", [], False, 0),
        ("accuracy", "Accuracy", "text", "", [], False, 1),
    ],
}

_LOCATION_ROOMS = ["Room 101", "Room 102", "Room 103"]
_LOCATION_SHELVES = ["Shelf A", "Shelf B", "Shelf C"]
_LOCATION_BINS = ["Bin 1", "Bin 2", "Bin 3"]

_PROJECT_NAMES = [
    "Perception Stack",
    "Swarm Drones",
    "Warehouse Arm",
    "Autonomy Testbed",
    "Field Robotics",
]

_TAG_NAMES = [
    "gpu",
    "drone",
    "loaner",
    "high-value",
    "spare",
    "calibration-due",
    "fragile",
    "battery-powered",
    "networked",
    "field-kit",
    "prototype",
    "leased",
    "hot",
    "quiet",
    "wireless",
]

_STATUS_WEIGHTS = [
    (Asset.Status.AVAILABLE, 55),
    (Asset.Status.IN_USE, 20),
    (Asset.Status.RESERVED, 8),
    (Asset.Status.MAINTENANCE, 7),
    (Asset.Status.RETIRED, 7),
    (Asset.Status.LOST, 3),
]

_GPU_MODELS = ["RTX 4090", "RTX 4080", "A100", "A6000", "RTX 3090", "L40S"]
_CPU_MODELS = ["Ryzen 9 7950X", "Xeon Gold 6338", "EPYC 7763", "Core i9-13900K"]
_OS_NAMES = ["Ubuntu 22.04", "Ubuntu 24.04", "Debian 12"]
_BOARD_MODELS = ["Jetson Orin Nano", "Jetson AGX Orin", "Raspberry Pi 5", "Coral Dev Board"]
_FC_MODELS = ["Pixhawk 6X", "Pixhawk Cube Orange", "Betaflight F7", "ArduPilot Matek"]
_MANUFACTURERS = ["NVIDIA", "Dell", "DJI", "Holybro", "Bosch", "Fluke", "Makita", "Keysight"]


def delete_seeded_tenant(tenant: Tenant) -> None:
    """Fully remove everything `seed_assets` (or any tenant-scoped seed) wrote
    for `tenant`, **including the `Tenant` row itself**, in FK-safe order —
    the counterpart T1.8's `perf_corpus` fixture was missing (QA T1.9 finding,
    see `apps.assets.tests.test_perf_10k`'s module docstring).

    Naively deleting the `Tenant` row directly raises `ProtectedError`:
    `Asset.category`/`Asset.location`/`Asset.project` and `Category.parent`/
    `Location.parent` are all `on_delete=PROTECT` (docs/data-model.md: a
    category/location in use, or a parent node with children, must not
    vanish out from under its dependents) — and Django's delete collector
    enforces PROTECT relations even when the protected referrer is ALSO
    about to be deleted via the `tenant` FK's `on_delete=CASCADE` in the
    same call (confirmed empirically while building this fixture: bulk
    `Tenant.objects.filter(pk=...).delete()` against a real seeded corpus
    fails with `ProtectedError` listing `Asset`/`Category`/`Location` rows
    that were, in fact, all slated for cascade-deletion in that same
    operation). So this deletes bottom-up by hand instead of relying on
    cascade from the `Tenant` delete:

    1. `Asset` rows first (their own `AssetFieldValue`/`TagLink` children
       CASCADE automatically) — this clears every PROTECT-guarding
       reference `Category`/`Location`/`Project` would otherwise still have.
    2. `Category`/`Location` self-referential trees, leaf-to-root (a node
       with no remaining children is a "leaf" for this purpose — repeatedly
       peel leaves off until the tree is empty; a plain `parent__isnull=False`
       -> `parent__isnull=True` two-pass would only work for a tree of depth
       exactly 2, and this seed's `Location` tree is 3 levels deep).
    3. `Project`/`Tag` (no remaining referrers once `Asset` is gone).
    4. `Membership` rows (`apps.rbac.models.Membership.role` is also
       `on_delete=PROTECT` — any tenant that has ever had a user/role
       assigned, e.g. `perf_corpus`'s admin, hits the exact same
       cascade-vs-PROTECT conflict as step 1-2 above when `Role` rows are
       cascade-collected off the `Tenant` delete below).
    5. The `Tenant` row itself — every OTHER `TenantScopedModel` table
       (`CustomFieldDef`, `Role`, `AuditLog`, `User`, etc.) has a plain
       `on_delete=CASCADE` `tenant` FK with no PROTECT relations of its own
       (once `Membership` is gone), so cascading from here is safe for the
       rest.

    Must run on the owner/superuser connection (same requirement as
    `seed_assets` — see this module's docstring), since it deletes rows
    across tenant-scoped tables directly via `all_objects` outside any
    `tenant_context()`.
    """
    Asset.all_objects.filter(tenant=tenant).delete()

    def _delete_tree_bottom_up(model) -> None:
        while True:
            qs = model.all_objects.filter(tenant=tenant)
            if not qs.exists():
                return
            parent_ids_in_use = set(
                model.all_objects.filter(tenant=tenant, parent__isnull=False).values_list(
                    "parent_id", flat=True
                )
            )
            leaves = qs.exclude(id__in=parent_ids_in_use)
            if not leaves.exists():  # pragma: no cover - would indicate a cycle
                raise RuntimeError(
                    f"delete_seeded_tenant: {model.__name__} rows remain for tenant "
                    f"{tenant.id} with no deletable leaves — unexpected tree structure."
                )
            leaves.delete()

    _delete_tree_bottom_up(Category)
    _delete_tree_bottom_up(Location)
    Project.all_objects.filter(tenant=tenant).delete()
    Tag.all_objects.filter(tenant=tenant).delete()

    from apps.rbac.models import Membership  # local import: avoid an app-loading cycle

    Membership.all_objects.filter(tenant=tenant).delete()
    Tenant.objects.filter(pk=tenant.id).delete()


@dataclass
class SeedResult:
    tenant_id: int
    category_ids: dict[str, int] = field(default_factory=dict)
    location_ids: list[int] = field(default_factory=list)
    project_ids: list[int] = field(default_factory=list)
    tag_ids: list[int] = field(default_factory=list)
    assets_created: int = 0
    field_values_created: int = 0
    tag_links_created: int = 0
    load_seconds: float = 0.0
    backfill_seconds: float = 0.0
    vacuum_seconds: float = 0.0

    @property
    def total_seconds(self) -> float:
        return self.load_seconds + self.backfill_seconds + self.vacuum_seconds


def _get_or_create_categories(tenant: Tenant) -> dict[str, Category]:
    by_name: dict[str, Category] = {}
    for parent_key, name, is_consumable, requires_approval, requires_calibration in _CATEGORY_TREE:
        parent = by_name.get(parent_key) if parent_key else None
        cat, _ = Category.all_objects.get_or_create(
            tenant=tenant,
            parent=parent,
            name=name,
            defaults={
                "default_is_consumable": is_consumable,
                "requires_approval": requires_approval,
                "requires_calibration": requires_calibration,
            },
        )
        by_name[name] = cat
    return by_name


def _get_or_create_field_defs(
    tenant: Tenant, categories: dict[str, Category]
) -> dict[str, list[CustomFieldDef]]:
    by_category: dict[str, list[CustomFieldDef]] = {}
    for cat_name, defs in _FIELD_DEFS.items():
        category = categories[cat_name]
        created = []
        for key, label, data_type, unit, enum_options, required, order in defs:
            fd, _ = CustomFieldDef.all_objects.get_or_create(
                tenant=tenant,
                category=category,
                key=key,
                defaults={
                    "label": label,
                    "data_type": data_type,
                    "unit": unit,
                    "enum_options": enum_options,
                    "required": required,
                    "order": order,
                },
            )
            created.append(fd)
        by_category[cat_name] = created
    return by_category


def _get_or_create_locations(tenant: Tenant) -> list[Location]:
    leaves: list[Location] = []
    for room_name in _LOCATION_ROOMS:
        room, _ = Location.all_objects.get_or_create(
            tenant=tenant, parent=None, name=room_name, defaults={"kind": "room"}
        )
        for shelf_name in _LOCATION_SHELVES:
            shelf, _ = Location.all_objects.get_or_create(
                tenant=tenant,
                parent=room,
                name=f"{room_name} {shelf_name}",
                defaults={"kind": "shelf"},
            )
            for bin_name in _LOCATION_BINS:
                bin_loc, _ = Location.all_objects.get_or_create(
                    tenant=tenant,
                    parent=shelf,
                    name=f"{room_name} {shelf_name} {bin_name}",
                    defaults={"kind": "bin"},
                )
                leaves.append(bin_loc)
    return leaves


def _get_or_create_projects(tenant: Tenant) -> list[Project]:
    projects = []
    for name in _PROJECT_NAMES:
        p, _ = Project.all_objects.get_or_create(
            tenant=tenant, name=name, defaults={"is_active": True}
        )
        projects.append(p)
    return projects


def _get_or_create_tags(tenant: Tenant) -> list[Tag]:
    tags = []
    for name in _TAG_NAMES:
        t, _ = Tag.all_objects.get_or_create(tenant=tenant, name=name)
        tags.append(t)
    return tags


def _weighted_status(rng: random.Random) -> str:
    total = sum(w for _, w in _STATUS_WEIGHTS)
    pick = rng.uniform(0, total)
    upto = 0.0
    for status, weight in _STATUS_WEIGHTS:
        upto += weight
        if pick <= upto:
            return status
    return _STATUS_WEIGHTS[-1][0]


def _field_value_for(rng: random.Random, cat_name: str, key: str, data_type: str, idx: int):
    if cat_name == "GPU Workstations":
        if key == "gpu_model":
            return rng.choice(_GPU_MODELS)
        if key == "vram_gb":
            return rng.choice([16, 24, 32, 48, 80])
        if key == "cpu":
            return rng.choice(_CPU_MODELS)
        if key == "ram_gb":
            return rng.choice([64, 128, 256])
        if key == "os":
            return rng.choice(_OS_NAMES)
        if key == "hostname":
            return f"gpu-node-{idx:05d}"
    if cat_name == "Edge Devices":
        if key == "board_model":
            return rng.choice(_BOARD_MODELS)
        if key == "ram_gb":
            return rng.choice([4, 8, 16])
        if key == "os":
            return rng.choice(_OS_NAMES)
        if key == "hostname":
            return f"edge-node-{idx:05d}"
    if cat_name == "Drone Electronics":
        if key == "flight_controller":
            return rng.choice(_FC_MODELS)
        if key == "battery_mah":
            return rng.choice([2200, 4000, 5200, 6000])
        if key == "motor_kv":
            return rng.choice([900, 1400, 2300])
        if key == "frame_size_in":
            return rng.choice([5.0, 7.0, 10.0])
    if cat_name == "Components":
        if key == "unit_cost":
            return round(rng.uniform(0.5, 200.0), 2)
        if key == "package_qty":
            return rng.choice([10, 25, 50, 100])
    if cat_name == "Tools":
        if key == "calibration_interval_days":
            return rng.choice([90, 180, 365])
    if cat_name == "Instruments":
        if key == "measurement_range":
            return rng.choice(["0-100V", "0-10A", "0-1000mm", "0-500N"])
        if key == "accuracy":
            return rng.choice(["+/-0.1%", "+/-0.5%", "+/-1%"])
    # Fallback (shouldn't be hit for the documented fields above).
    if data_type == "int":
        return idx
    if data_type == "float":
        return float(idx)
    return f"value-{idx}"


_NAME_TEMPLATES: dict[str, str] = {
    "GPU Workstations": "GPU Workstation",
    "Edge Devices": "Edge Device",
    "Drone Electronics": "Drone Electronics Kit",
    "Components": "Component Bin",
    "Tools": "Tool",
    "Instruments": "Instrument",
}


def seed_assets(
    tenant: Tenant,
    *,
    count: int = 10_000,
    batch_size: int = 1_000,
    rng_seed: int = 42,
) -> SeedResult:
    """Idempotent-ish bulk seed: catalog fixtures (categories/fields/
    locations/projects/tags) are `get_or_create`d (safe to re-run), and
    `count` NEW `Asset` rows are appended each call (re-running with the
    same `tenant` grows the corpus rather than erroring — fine for perf
    testing, where "at least 10k rows" is the requirement, not "exactly").

    Must run on a connection with owner privileges on `assets_asset`/
    `assets_asset_field_value`/`assets_tag_link` (see module docstring) —
    both pytest-django's default connection and the docker-compose `migrate`
    one-off service satisfy this.
    """
    rng = random.Random(rng_seed)
    result = SeedResult(tenant_id=tenant.id)

    categories = _get_or_create_categories(tenant)
    field_defs_by_category = _get_or_create_field_defs(tenant, categories)
    locations = _get_or_create_locations(tenant)
    projects = _get_or_create_projects(tenant)
    tags = _get_or_create_tags(tenant)

    result.category_ids = {name: c.id for name, c in categories.items()}
    result.location_ids = [loc.id for loc in locations]
    result.project_ids = [p.id for p in projects]
    result.tag_ids = [t.id for t in tags]

    # Only seed assets under the LEAF categories (the ones with field defs +
    # realistic name templates) — the two parent categories ("Compute",
    # "Drone Kit") exist purely as tree structure, matching a real lab's
    # taxonomy (docs/data-model.md: "self-referential tree").
    leaf_category_names = list(_NAME_TEMPLATES.keys())

    load_start = time.monotonic()
    with triggers_disabled():
        with transaction.atomic():
            created_in_batch = 0
            pending_assets: list[Asset] = []
            pending_meta: list[tuple[str, int]] = []  # (category_name, idx)

            def _flush() -> None:
                nonlocal created_in_batch
                if not pending_assets:
                    return
                Asset.all_objects.bulk_create(pending_assets, batch_size=batch_size)
                result.assets_created += len(pending_assets)

                field_values: list[AssetFieldValue] = []
                tag_links: list[TagLink] = []
                for asset, (cat_name, idx) in zip(pending_assets, pending_meta, strict=False):
                    for fd in field_defs_by_category.get(cat_name, []):
                        # ~85% chance a non-required field is populated, so
                        # the corpus has realistic sparse coverage too.
                        if not fd.required and rng.random() > 0.85:
                            continue
                        value = _field_value_for(rng, cat_name, fd.key, fd.data_type, idx)
                        field_values.append(
                            AssetFieldValue(
                                tenant_id=tenant.id, asset=asset, field_def=fd, value=value
                            )
                        )
                    n_tags = rng.choice([0, 0, 1, 1, 2, 3])
                    for tag in rng.sample(tags, k=min(n_tags, len(tags))):
                        tag_links.append(TagLink(tenant_id=tenant.id, asset=asset, tag=tag))

                if field_values:
                    AssetFieldValue.all_objects.bulk_create(field_values, batch_size=batch_size)
                    result.field_values_created += len(field_values)
                if tag_links:
                    TagLink.all_objects.bulk_create(tag_links, batch_size=batch_size)
                    result.tag_links_created += len(tag_links)

                pending_assets.clear()
                pending_meta.clear()

            for i in range(count):
                cat_name = leaf_category_names[i % len(leaf_category_names)]
                category = categories[cat_name]
                location = locations[rng.randrange(len(locations))]
                project = projects[rng.randrange(len(projects))] if rng.random() < 0.4 else None
                status = _weighted_status(rng)
                manufacturer = rng.choice(_MANUFACTURERS)
                serial = f"SN-{cat_name[:3].upper()}-{i:06d}"
                name = f"{_NAME_TEMPLATES[cat_name]} {i:06d} {manufacturer}"
                asset = Asset(
                    tenant_id=tenant.id,
                    category=category,
                    name=name,
                    description=f"Seeded {cat_name} asset #{i} for perf testing.",
                    is_consumable=category.default_is_consumable,
                    project=project,
                    serial_number=serial,
                    manufacturer=manufacturer,
                    model=f"{cat_name}-Model-{rng.randint(1, 9)}",
                    location=location,
                    status=status,
                    retired_at=timezone.now() if status == Asset.Status.RETIRED else None,
                )
                pending_assets.append(asset)
                pending_meta.append((cat_name, i))
                created_in_batch += 1
                if created_in_batch >= batch_size:
                    _flush()
                    created_in_batch = 0
            _flush()
    result.load_seconds = time.monotonic() - load_start

    backfill_start = time.monotonic()
    with connection.cursor() as cur:
        cur.execute(_BACKFILL_SQL, [tenant.id])
    result.backfill_seconds = time.monotonic() - backfill_start

    # VACUUM (ANALYZE), not just ANALYZE (perf-testing finding): the
    # backfill above is a single `UPDATE` touching EVERY just-bulk_create'd
    # row's `search_vector` (an indexed column, so this can never be a HOT
    # update) — that leaves one DEAD tuple per row in `assets_asset` and a
    # stale/duplicate entry in every one of its indexes (confirmed via
    # `EXPLAIN (ANALYZE, BUFFERS)`: the tenant_id index reported ~2x the
    # real row count until this ran). A bare `ANALYZE` alone would refresh
    # planner row-count estimates but leave the physical bloat/duplicate
    # index entries in place. `VACUUM` must run outside any open
    # transaction (autocommit) — safe here since this function's own
    # `transaction.atomic()` block (the bulk load) has already exited.
    vacuum_start = time.monotonic()
    with connection.cursor() as cur:
        cur.execute("VACUUM (ANALYZE) assets_asset;")
    result.vacuum_seconds = time.monotonic() - vacuum_start

    return result
