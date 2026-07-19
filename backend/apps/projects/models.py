"""Minimal `Project` stub (T0.4).

`docs/data-model.md` places the full `Project` entity (with `Category`,
`Location`, `Tag`, etc.) in M1 (`docs/tasks/M1-asset-registry.md`). T0.4 needs
*something* concrete for `rbac.Membership.project` to point at, because
demonstrating the project-scoped-vs-tenant-wide permission distinction (the
T0.4 exit criterion) requires real `Project` rows.

This intentionally only carries the fields needed for RBAC scoping right now
(`tenant`, `name`, `is_active`). **Flagged for M1 / db-migration-specialist**:
M1 extends this model with `lead_user` and any additional fields/indexes from
`data-model.md`, via a normal migration on top of this one — it does not need
to be a new app/table.
"""

from __future__ import annotations

from django.db import models

from apps.tenancy.models import TenantScopedModel


class Project(TenantScopedModel):
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "projects_project"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="uniq_project_tenant_name")
        ]
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name
