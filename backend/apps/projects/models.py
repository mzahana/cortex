"""`Project` (T0.4 stub, reconciled to its full M1/T1.1 shape here).

T0.4 introduced a minimal stub (`tenant`, `name`, `is_active`) because
`rbac.Membership.project` needed *something* concrete to point at to
demonstrate the project-scoped-vs-tenant-wide permission distinction — see
`docs/data-model.md`, which places the full `Project` entity in M1
(`docs/tasks/M1-asset-registry.md`).

**T1.1 reconciliation:** per `docs/data-model.md` §2 ("Project — `id,
tenant_id, name, lead_user_id, is_active`"), this is the SAME model/table
(`projects_project`) extended with `lead_user` via a normal, additive
migration (`0002_project_lead_user.py`) — no new app, no new table, no data
migration needed since the column is nullable. `data-model.md` lists no
other Project fields (no `code`/description/etc.), so none are added here;
if a future milestone needs more, extend the same table again.

`lead_user` is nullable (`SET_NULL` on delete) because a project may exist
before a lead is assigned, and a user leaving the lab shouldn't cascade-delete
the project itself.
"""

from __future__ import annotations

from django.db import models

from apps.tenancy.models import TenantScopedModel


class Project(TenantScopedModel):
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
