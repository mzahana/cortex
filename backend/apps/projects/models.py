"""`Project` and the M7 project/grant hub (budgets, expenses, documents).

`Project` began as a T0.4 stub (`tenant`, `name`, `is_active`) because
`rbac.Membership.project` needed *something* concrete to point at to
demonstrate the project-scoped-vs-tenant-wide permission distinction — see
`docs/data-model.md`, which places the full `Project` entity in M1
(`docs/tasks/M1-asset-registry.md`).

**T1.1 reconciliation** added `lead_user` (see `0002_project_lead_user...`).

**M7 (Project & Grant Management)** — `docs/tasks/M7-project-grants.md`,
`docs/data-model.md` §2 — turns the thin config row into a fundable grant
hub: budget/funding metadata on `Project`, a tenant-wide `ExpenseCategory`
config list, a first-class project-scoped `Expense` ledger with invoice
scans (`ExpenseAttachment`), and project documents (`ProjectDocument`). All
new tables are tenant-owned -> every one subclasses `TenantScopedModel` (the
fail-closed tenant-scoped manager, T0.4) and gets a Postgres RLS policy in
`0004_m7_rls_indexes` (the R4 backstop), so the app-level filter and the DB
policy can never drift. `Expense`, its attachments, and `ProjectDocument` are
all **project-scoped** so the RBAC union-of-memberships scope rule
(`docs/rbac.md` §1/§3) and RLS both apply.

`lead_user` is nullable (`SET_NULL` on delete) because a project may exist
before a lead is assigned, and a user leaving the lab shouldn't cascade-delete
the project itself.
"""

from __future__ import annotations

from django.db import models

from apps.tenancy.models import TenantScopedModel


class Project(TenantScopedModel):
    """A lab project, optionally a funded grant (M7).

    The M7 fields are all nullable or defaulted so the additive column
    migration (`0003_m7_project_fields`) is safe on existing rows — a project
    created before M7 simply carries empty funding metadata and stays a plain
    asset-grouping lens (`Asset.project_id` NULL = general pool, unchanged).
    """

    class FundingSource(models.TextChoices):
        INTERNAL = "internal", "Internal"
        EXTERNAL = "external", "External"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        CLOSED = "closed", "Closed"

    name = models.CharField(max_length=255)
    # `docs/data-model.md`: "Project — ... lead_user_id ...". Points at
    # `accounts.User` (tenant-owned); the FK itself needs no extra tenant
    # check here (RLS + `TenantScopedManager` already prevent a cross-tenant
    # `User` row from being assigned by anything going through the normal
    # tenant-scoped write path — see `apps.catalog.serializers` for how the
    # API layer additionally scopes the *selectable* queryset, R4/F1).
    lead_user = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="led_projects",
    )
    is_active = models.BooleanField(default=True)

    # --- M7 grant metadata (all additive: nullable or defaulted) -----------
    code = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Optional short project/grant code, e.g. 'NSF-2026-014'.",
    )
    funding_source = models.CharField(
        max_length=8,
        choices=FundingSource.choices,
        blank=True,
        default="",
        help_text="Internal budget vs. external grant (docs/data-model.md §2).",
    )
    sponsor = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Awarding body / funding sponsor for an external grant.",
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    budget_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Single awarded budget total; spend is broken down by "
        "ExpenseCategory in the report (no per-category allocations in M7).",
    )
    currency = models.CharField(max_length=3, blank=True, default="")
    status = models.CharField(
        max_length=8,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "projects_project"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="uniq_project_tenant_name")
        ]
        indexes = [models.Index(fields=["tenant", "lead_user"])]
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


class ExpenseCategory(TenantScopedModel):
    """Tenant-wide spend category (Equipment, Consumables, Services, ...).

    Modeled like `catalog.Location`/`catalog.Category`: tenant-wide admin
    config (NOT project-scoped), one row per tenant per name. Each `Expense`
    carries a category so the M7 report can break spend down by category.
    A starter set is seeded per tenant in `0005_seed_expense_categories`;
    `is_active` lets a tenant retire a category without deleting historical
    expenses that reference it (the FK is `SET_NULL`).
    """

    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "projects_expense_category"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], name="uniq_expense_category_tenant_name"
            )
        ]
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


class Expense(TenantScopedModel):
    """A single cost booked against a project (M7 expense ledger).

    First-class record for ANY cost — asset purchase, consumable, shipping,
    service, software, travel — so the budget rollup (`budget_total` vs. sum of
    `amount`) and the per-category breakdown are exact. Project-scoped so the
    RBAC scope rule and RLS both constrain it to the owning project/tenant. An
    asset purchase MAY link back to its `Asset` (`SET_NULL`, so deleting the
    asset never destroys the financial record).
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="expenses")
    category = models.ForeignKey(
        ExpenseCategory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expenses",
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, blank=True, default="")
    date = models.DateField()
    vendor = models.CharField(max_length=255, blank=True, default="")
    invoice_number = models.CharField(max_length=128, blank=True, default="")
    description = models.TextField(blank=True, default="")
    asset = models.ForeignKey(
        "assets.Asset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expenses",
    )
    created_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_expenses",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "projects_expense"
        indexes = [
            models.Index(fields=["tenant", "project"]),
            models.Index(fields=["tenant", "category"]),
            models.Index(fields=["tenant", "date"]),
        ]
        ordering = ["-date", "-id"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.project_id}:{self.amount}"


class ProjectDocument(TenantScopedModel):
    """Project document metadata: proposal, contract, progress report, other.

    **The binary lives on the storage backend (django-storages,
    `config/settings/base.py` STORAGES); only `storage_key` (the relative path
    returned by the backend) plus metadata is ever written to this table** —
    same convention as `apps.assets.models.Attachment`, reusing
    `apps.assets.services.save_attachment_file` as the only writer of
    `storage_key`. Project-scoped (FK CASCADE) so RLS + the RBAC scope rule
    apply.
    """

    class Kind(models.TextChoices):
        PROPOSAL = "proposal", "Proposal"
        CONTRACT = "contract", "Contract"
        PROGRESS_REPORT = "progress_report", "Progress report"
        OTHER = "other", "Other"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="documents")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OTHER)
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
        related_name="uploaded_project_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "projects_project_document"
        indexes = [models.Index(fields=["tenant", "project"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.filename


class ExpenseAttachment(TenantScopedModel):
    """Invoice/receipt scan for an `Expense` (M7).

    **DECISION — dedicated model, NOT a reused generic attachment.** An invoice
    scan anchors to an `Expense` (FK CASCADE), whereas `ProjectDocument`
    anchors to a `Project`; a single generic table would need two nullable,
    mutually-exclusive parent FKs, losing the NOT NULL + CASCADE integrity that
    guarantees every attachment row is strictly project-scoped through exactly
    one parent, and muddying the `(tenant, <parent>)` index. `apps.assets`
    already follows this "one dedicated attachment table per anchor" pattern
    (`assets.Attachment` -> `Asset`), so a peer table here keeps the codebase
    consistent and each table's RLS/index story clean. Recorded in
    `docs/tasks/M7-project-grants.md` data-model section.

    Same storage convention as `apps.assets.models.Attachment`: **the binary
    lives on the storage backend; only `storage_key` + metadata in the DB.**
    """

    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="attachments")
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
        related_name="uploaded_expense_attachments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "projects_expense_attachment"
        indexes = [models.Index(fields=["tenant", "expense"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.filename
