"""Catalog models (T1.1): `Category`, `CustomFieldDef`, `Location`, `Tag`.

All tenant-owned -> every model inherits `TenantScopedModel` (the fail-closed
tenant-scoped manager, T0.4), so ordinary `Model.objects...` access is always
filtered by the tenant in the current request/task context — see
`apps.tenancy.managers` / `apps.tenancy.context`. Postgres RLS on these tables
is **T1.3** (db-migration-specialist); nothing here blocks it — see the list
of new tenant-owned tables in the T1.1 report.

`TagLink` (asset <-> tag many-to-many) is deliberately **not** defined here:
`docs/data-model.md`'s ERD FKs it to `Asset`, which does not exist until
T1.2 (`apps.assets`). Creating it now against a not-yet-existing model would
fail Django's own system checks. `Tag` itself (needed for the read-only
`/api/v1/tags` endpoint, docs/api-and-ui.md) is fully defined here; `TagLink`
is added alongside `Asset` in T1.2. Flagged for db-migration-specialist /
code-reviewer.
"""

from __future__ import annotations

from django.db import models

from apps.tenancy.models import TenantScopedModel


class Category(TenantScopedModel):
    """Self-referential category tree (Compute, Edge, Drone Kit, Drone
    Electronics, Components, Tools, Instruments, ...). `docs/data-model.md`
    §2. Tenant-wide admin config (not project-scoped) — see
    `apps.catalog.permissions`.
    """

    name = models.CharField(max_length=255)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
        help_text="PROTECT: a parent category with children cannot be deleted "
        "until its children are re-parented/removed first (avoids silently "
        "orphaning or cascade-wiping a whole subtree).",
    )
    default_is_consumable = models.BooleanField(
        default=False,
        help_text="Default for Asset.is_consumable when an asset is created "
        "under this category (docs/data-model.md §4).",
    )
    requires_approval = models.BooleanField(
        default=False,
        help_text="Drives the per-category reservation/checkout approval "
        "config (docs/rbac.md §4).",
    )
    requires_calibration = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "catalog_category"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "parent", "name"], name="uniq_category_tenant_parent_name"
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "parent"]),
        ]
        ordering = ["tenant_id", "name"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


class CustomFieldDef(TenantScopedModel):
    """Per-category typed field definition for the flexible spec model
    (`docs/data-model.md` §2), e.g. Compute -> `gpu_model`, `vram_gb`.
    """

    class DataType(models.TextChoices):
        TEXT = "text", "Text"
        INT = "int", "Integer"
        FLOAT = "float", "Float"
        BOOL = "bool", "Boolean"
        DATE = "date", "Date"
        ENUM = "enum", "Enum"
        JSON = "json", "JSON"

    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="field_defs")
    key = models.SlugField(max_length=100, help_text="Machine key, e.g. 'vram_gb'.")
    label = models.CharField(max_length=255)
    data_type = models.CharField(max_length=8, choices=DataType.choices)
    unit = models.CharField(max_length=32, blank=True, default="")
    enum_options = models.JSONField(
        default=list,
        blank=True,
        help_text="List of allowed values when data_type='enum'; ignored otherwise.",
    )
    required = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "catalog_custom_field_def"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "category", "key"], name="uniq_field_def_tenant_category_key"
            )
        ]
        indexes = [models.Index(fields=["tenant", "category"])]
        ordering = ["category_id", "order", "id"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.category_id}:{self.key}"


class Location(TenantScopedModel):
    """Self-referential location tree: Room -> Shelf -> Cabinet -> Bin
    (`docs/data-model.md` §2). Tenant-wide admin config, same scope rule as
    `Category` — see `apps.catalog.permissions`.
    """

    name = models.CharField(max_length=255)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    # ASSUMPTION (flagged): `docs/data-model.md` lists `kind` as a free
    # descriptive field ("Room -> Shelf -> Cabinet -> Bin" are examples, not
    # an enforced enum) with no documented value set, so this is a plain,
    # optional string rather than a DB-level choices constraint — a tenant
    # may use whatever vocabulary fits its physical space.
    kind = models.CharField(max_length=50, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "catalog_location"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "parent", "name"], name="uniq_location_tenant_parent_name"
            )
        ]
        indexes = [models.Index(fields=["tenant", "parent"])]
        ordering = ["tenant_id", "name"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


class Tag(TenantScopedModel):
    """Free-form tag (`docs/data-model.md` §2). Read-only via the API
    (`GET /api/v1/tags`, docs/api-and-ui.md) — tags are created/attached
    inline as part of tagging an asset (T1.2's `TagLink`), not through a
    dedicated admin CRUD surface.
    """

    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "catalog_tag"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="uniq_tag_tenant_name")
        ]
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name
