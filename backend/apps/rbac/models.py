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
