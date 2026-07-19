"""Asset registry core (T1.2): `Asset`, `AssetFieldValue`, `Attachment`,
`TagLink`. `docs/data-model.md` §2 is the field authority.

All four models are tenant-owned -> `TenantScopedModel` (T0.4's fail-closed
base manager) — same golden-path rule as `apps.catalog` (T1.1). Postgres RLS
+ the `(tenant_id, status|category_id|location_id|project_id)` composite
indexes, the `search_vector` GIN index + maintaining trigger, `pg_trgm` GIN
on `name`/`serial_number`, and the JSONB GIN on `asset_field_value.value` are
**T1.3** (db-migration-specialist) — flagged below on each field they apply
to. Nothing here blocks that.

**New tenant-owned tables for T1.3** (explicit list per this task's
instructions): `asset`, `asset_field_value`, `attachment`, `tag_link`
(db_table names: `assets_asset`, `assets_asset_field_value`,
`assets_attachment`, `assets_tag_link`). `apps.audit.AuditLog`
(`audit_audit_log`) is also new in this change — see that app's module
docstring for why, and flagged there too for T1.3/M5 to add RLS + indexes.
"""

from __future__ import annotations

import secrets
import uuid as uuid_lib

from django.contrib.postgres.search import SearchVectorField
from django.db import models

from apps.catalog.models import Category, CustomFieldDef, Location, Tag
from apps.tenancy.models import TenantScopedModel

QR_TOKEN_BYTES = 24  # ~192 bits of entropy; unguessable per the M4 scan-resolve requirement.


def _generate_qr_token() -> str:
    """High-entropy, URL-safe token — stable identity encoded in the printed
    label QR (T1.2 scope: generate + store only; `GET /api/v1/resolve/{qr_token}`
    itself is M4's job). Uniqueness is enforced per-tenant by the DB
    constraint below; callers (`Asset.save()`) retry on collision, though at
    this entropy a collision is astronomically unlikely.
    """
    return secrets.token_urlsafe(QR_TOKEN_BYTES)


class Asset(TenantScopedModel):
    """The central asset record (`docs/data-model.md` §2). Project-scoped for
    RBAC purposes via `project` (`NULL` = general pool) — see
    `apps.assets.permissions` for how `asset.create/edit/retire/attach` are
    enforced against it (docs/rbac.md §1 union-of-memberships scope rule).
    """

    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        IN_USE = "in_use", "In use"
        RESERVED = "reserved", "Reserved"
        MAINTENANCE = "maintenance", "Maintenance"
        RETIRED = "retired", "Retired"
        LOST = "lost", "Lost"

    # --- Identity ------------------------------------------------------------
    uuid = models.UUIDField(
        default=uuid_lib.uuid4,
        editable=False,
        unique=True,
        help_text="Public code (docs/data-model.md: 'id (uuid, public code)').",
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="assets",
        help_text="PROTECT: a category in use by assets cannot be deleted out from " "under them.",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")

    # --- Type flags ------------------------------------------------------------
    is_consumable = models.BooleanField(
        default=False,
        help_text="Defaulted from `category.default_is_consumable` at creation "
        "(docs/data-model.md §4); never both consumable and durable.",
    )
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assets",
        help_text="NULL = general pool (docs/data-model.md §4). PROTECT: reassign "
        "an active project's assets before it can be deleted.",
    )

    # --- Physical / commercial ------------------------------------------------
    serial_number = models.CharField(max_length=255, blank=True, default="")
    manufacturer = models.CharField(max_length=255, blank=True, default="")
    model = models.CharField(max_length=255, blank=True, default="")
    location = models.ForeignKey(
        Location,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assets",
    )
    purchase_date = models.DateField(null=True, blank=True)
    purchase_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True, default="")
    warranty_expiry = models.DateField(null=True, blank=True)
    supplier = models.CharField(max_length=255, blank=True, default="")

    # --- State -----------------------------------------------------------------
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.AVAILABLE)
    condition = models.TextField(blank=True, default="")
    retired_at = models.DateTimeField(null=True, blank=True)

    # --- Workload (compute) -----------------------------------------------------
    current_workload_user = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="workload_assets",
    )

    # --- Label / scan ------------------------------------------------------------
    qr_token = models.CharField(max_length=64, default=_generate_qr_token, editable=False)

    # --- Search (T1.3 maintains this via trigger; GIN index also T1.3) ----------
    search_vector = SearchVectorField(null=True, editable=False)

    tags: models.ManyToManyField = models.ManyToManyField(
        Tag, through="TagLink", related_name="assets"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "assets_asset"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "uuid"], name="uniq_asset_tenant_uuid"),
            models.UniqueConstraint(
                fields=["tenant", "qr_token"], name="uniq_asset_tenant_qr_token"
            ),
        ]
        indexes = [
            # T1.3 also adds: GIN(search_vector), pg_trgm GIN(name),
            # pg_trgm GIN(serial_number). Kept here (not deferred) since they
            # cost nothing at this row count and match `docs/data-model.md`
            # §3 exactly; T1.3 layers the GIN/trgm ones on top.
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "category"]),
            models.Index(fields=["tenant", "location"]),
            models.Index(fields=["tenant", "project"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name

    def save(self, *args, **kwargs):
        # Retry on the astronomically-unlikely qr_token collision within the
        # same tenant (unique constraint above) rather than ever storing a
        # collided/blank token.
        from django.db import IntegrityError, transaction

        if not self.qr_token:
            self.qr_token = _generate_qr_token()
        attempts = 0
        while True:
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                attempts += 1
                if attempts >= 5:
                    raise
                self.qr_token = _generate_qr_token()


class AssetFieldValue(TenantScopedModel):
    """Typed value of one `CustomFieldDef` for one `Asset`
    (`docs/data-model.md` §2). Validated against the asset's category's
    field defs in `apps.assets.services.validate_custom_field_values` before
    ever reaching this model — this table trusts already-coerced values.
    """

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="field_values")
    field_def = models.ForeignKey(
        CustomFieldDef, on_delete=models.CASCADE, related_name="asset_values"
    )
    value = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "assets_asset_field_value"
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "field_def"], name="uniq_asset_field_value_asset_field_def"
            )
        ]
        indexes = [models.Index(fields=["tenant", "asset"])]
        # T1.3 also adds: GIN(value) (JSONB) for spec filters (docs/data-model.md §3).

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.asset_id}:{self.field_def_id}"


def _attachment_kind_choices():
    return [("photo", "Photo"), ("doc", "Document")]


class Attachment(TenantScopedModel):
    """Photo/doc metadata (`docs/data-model.md` §2). **The binary lives on
    the mounted media volume via the storage backend (django-storages,
    `config/settings/base.py` STORAGES); only `storage_key` (the relative
    path returned by the storage backend) plus metadata is ever written to
    this table** — see `apps.assets.services.save_attachment_file`, the only
    writer of `storage_key`.
    """

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="attachments")
    kind = models.CharField(max_length=10, choices=_attachment_kind_choices(), default="photo")
    storage_key = models.CharField(
        max_length=500,
        help_text="Relative path/key on the storage backend (volume or, later, "
        "S3-compatible object storage) — never the binary itself.",
    )
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=127, blank=True, default="")
    size = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_attachments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "assets_attachment"
        indexes = [models.Index(fields=["tenant", "asset"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.filename


class TagLink(TenantScopedModel):
    """Asset <-> Tag many-to-many through table (`docs/data-model.md` §2).
    Deferred from T1.1 to here since it FKs `Asset` (see
    `apps.catalog.models` module docstring).
    """

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="tag_links")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="asset_links")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "assets_tag_link"
        constraints = [
            models.UniqueConstraint(fields=["asset", "tag"], name="uniq_tag_link_asset_tag")
        ]
        indexes = [models.Index(fields=["tenant", "asset"]), models.Index(fields=["tenant", "tag"])]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.asset_id}:{self.tag_id}"
