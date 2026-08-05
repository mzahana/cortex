"""RBAC models (T0.4): `Role`, `Permission`, `RolePermission`, `Membership`.

See `docs/rbac.md` §1 for the concepts and `docs/data-model.md` for fields.

Design note (documented deviation from `data-model.md`'s literal wording):
`data-model.md` describes `Role.tenant_id` as "nullable for system roles".
Here, every `Role` — including the 4 system roles — carries a **non-null**
`tenant_id`: each tenant gets its own copy of Admin/ProjectLead/Member/Viewer
(seeded automatically when the tenant is created, see `signals.py`). This
keeps `Role` a normal `TenantScopedModel` with no null-tenant special case,
which matters for two reasons: (1) the fail-closed base manager filters on
`tenant_id` uniformly, no exception branch for "shared" rows; (2) T0.5's RLS
policies key off `tenant_id` on every tenant-owned table without carving out
nullable-tenant rows. Net behavior for RBAC is identical to the nullable
design (`is_system=True` still distinguishes system roles from
tenant-authored custom roles). Flagged for db-migration-specialist/reviewer.
"""

from __future__ import annotations

from django.db import models

from apps.tenancy.models import TenantScopedModel


class Permission(models.Model):
    """An atomic action key (`asset.create`, `stock.adjust`, ...).

    NOT tenant-owned: the set of possible permission keys is a fixed,
    system-wide vocabulary (docs/rbac.md §3) shared by every tenant's Roles.
    """

    key = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=255)

    class Meta:
        db_table = "rbac_permission"
        ordering = ["key"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.key


class Role(TenantScopedModel):
    key = models.SlugField(max_length=50)
    name = models.CharField(max_length=100)
    is_system = models.BooleanField(
        default=False,
        help_text="True for the 4 seeded system roles; False for tenant-authored custom roles.",
    )
    is_customized = models.BooleanField(
        default=False,
        help_text=(
            "True once an admin has edited this role's permission set away from the "
            "`SYSTEM_ROLE_PERMISSIONS` defaults. Load-bearing: `apps.rbac.seed."
            "seed_roles_for_tenant` is idempotent and `get_or_create`s every default "
            "grant, so without this flag a re-run (migration backfill, management "
            "command) would silently re-add grants an admin deliberately removed. "
            "'Reset to defaults' clears it."
        ),
    )
    permissions: models.ManyToManyField = models.ManyToManyField(
        Permission, through="RolePermission", related_name="roles"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "rbac_role"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "key"], name="uniq_role_tenant_key")
        ]
        indexes = [models.Index(fields=["tenant", "key"])]
        ordering = ["tenant_id", "key"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.name} ({self.tenant_id})"


class RolePermission(TenantScopedModel):
    """Role <-> Permission grant.

    Carries `tenant_id` directly (denormalized from `role.tenant_id`) so this
    join table is itself a normal `TenantScopedModel` — queryable through the
    fail-closed base manager and RLS-protected (T0.5) like everything else,
    rather than relying on a join through `role` for isolation.
    """

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="role_permissions")
    permission = models.ForeignKey(
        Permission, on_delete=models.CASCADE, related_name="role_permissions"
    )

    class Meta:
        db_table = "rbac_role_permission"
        constraints = [
            models.UniqueConstraint(fields=["role", "permission"], name="uniq_role_permission")
        ]
        indexes = [models.Index(fields=["tenant", "role"])]

    def save(self, *args, **kwargs):
        # Keep the denormalized tenant_id in lockstep with the owning role —
        # never trust a caller-supplied tenant here.
        if self.role_id and not self.tenant_id:
            self.tenant_id = self.role.tenant_id
        super().save(*args, **kwargs)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.role.key} -> {self.permission.key}"


class Membership(TenantScopedModel):
    """Binds a `User` to a `Role` within a scope (docs/rbac.md §1):

    - `project = None` -> tenant-wide membership (always effective).
    - `project = <Project>` -> project-scoped membership (only effective for
      that project's assets — see `apps.rbac.services.get_effective_permissions`).
    """

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="memberships")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="memberships")
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "rbac_membership"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "role", "project"], name="uniq_membership_user_role_project"
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "user"]),
            models.Index(fields=["tenant", "project"]),
        ]

    def save(self, *args, **kwargs):
        # Tenant is derived from the user, never accepted as caller input.
        if self.user_id and not self.tenant_id:
            self.tenant_id = self.user.tenant_id
        super().save(*args, **kwargs)

    def __str__(self) -> str:  # pragma: no cover - trivial
        scope = f"project={self.project_id}" if self.project_id else "tenant-wide"
        return f"{self.user_id} - {self.role.key} ({scope})"


class UserPermissionOverride(TenantScopedModel):
    """A per-user exception to what this user's ROLES grant (docs/rbac.md §6).

    Roles stay the default-bearing bundle; this is the "and for THIS person,
    also allow / never allow X" adjustment an admin makes from Users & Roles
    without having to author a whole custom role for one deviation.

    **Deliberately tenant-wide only — no `project` FK.** A project-scoped
    override would have to be threaded through
    `apps.rbac.services.get_viewable_project_scope`'s `(tenant_wide,
    project_ids)` contract, which ~10 list endpoints unpack to build their
    querysets; a per-project DENY in particular can't be expressed in that
    2-tuple at all (a tenant-wide holder minus one project), so it would
    silently over-show rows on every one of those lists — the R4-adjacent
    mistake this codebase is least willing to make. Tenant-wide semantics
    are exact and trivially auditable instead:

    - `GRANT` — the user holds this key everywhere in the tenant (general
      pool + every project), exactly as if a tenant-wide role granted it.
    - `DENY`  — the user holds this key nowhere, and DENY beats every grant
      (their roles', and a GRANT override): it is "never allow", the shape
      an admin reaches for when revoking something.

    If per-project overrides are ever wanted, they are an additive change
    (new nullable `project` FK + a scope contract that can carry exclusions)
    — not something this model's semantics have to anticipate now.
    """

    class Effect(models.TextChoices):
        GRANT = "grant", "Always allow"
        DENY = "deny", "Never allow"

    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="permission_overrides"
    )
    permission = models.ForeignKey(
        Permission, on_delete=models.CASCADE, related_name="user_overrides"
    )
    effect = models.CharField(max_length=8, choices=Effect.choices)
    created_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rbac_user_permission_override"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "permission"], name="uniq_user_permission_override"
            )
        ]
        indexes = [models.Index(fields=["tenant", "user"])]
        ordering = ["user_id", "permission_id"]

    def save(self, *args, **kwargs):
        # Tenant derived from the user, never accepted as caller input —
        # same rule as `Membership.save`.
        if self.user_id and not self.tenant_id:
            self.tenant_id = self.user.tenant_id
        super().save(*args, **kwargs)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.user_id} {self.effect} {self.permission_id}"
