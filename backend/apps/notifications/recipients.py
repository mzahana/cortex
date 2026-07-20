"""Recipient resolution for scoped notification events (T5.2).

`approval_request` (notify whoever can approve a reservation for the
asset's project) and the low-stock reminder (notify whoever can approve a
reorder for the asset's project) both need "every ACTIVE user who effectively
holds permission X in scope of project P" -- the union-of-memberships rule
(`docs/rbac.md` §1), same one `apps.rbac.services.get_effective_permissions`
implements for a SINGLE user, generalized here to "which users" rather than
"does this one user". Kept in `apps.notifications` (not `apps.rbac`) since
this is purely a notification-recipient concern, not a permission-check one --
no view/permission class calls this.

Must be called with `apps.tenancy.context.tenant_context(tenant_id)` already
active (same rule as every other tenant-scoped query in this codebase) --
callers here are always either a signal receiver firing from
`transaction.on_commit` inside an already-tenant-scoped request, or a beat
task that has explicitly entered `tenant_context()` for the tenant it's
currently scanning.
"""

from __future__ import annotations

from django.db.models import Q

from apps.rbac.models import Membership


def users_with_permission_in_project_scope(permission_key: str, project_id: int | None) -> list:
    """Every ACTIVE user effectively holding `permission_key` for
    `project_id` (`None` = the tenant's general pool): a tenant-wide
    membership granting it, OR a membership scoped to exactly `project_id`
    granting it. Deduplicated (a user with both a tenant-wide AND a
    project-scoped qualifying membership is returned once) and excludes
    deactivated accounts (a soft-deactivated user should never receive a new
    notification -- `docs/data-model.md` "never hard-deleted" for users with
    history, but `is_active=False` is exactly the "do not contact" signal).
    """
    scope_filter = Q(project__isnull=True)
    if project_id is not None:
        scope_filter |= Q(project_id=project_id)

    memberships = (
        Membership.objects.filter(
            scope_filter,
            role__role_permissions__permission__key=permission_key,
            user__is_active=True,
        )
        .select_related("user")
        .distinct()
    )
    seen: dict[int, object] = {}
    for membership in memberships:
        seen[membership.user_id] = membership.user
    return list(seen.values())
