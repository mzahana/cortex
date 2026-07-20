"""`GET/POST /api/v1/memberships`, `GET/PATCH/DELETE /api/v1/memberships/{id}`
(T5.3 audit-finalize gap-fill; `docs/api-and-ui.md`: "Assign role/scope").

This is the "user.manage"/"role.assign" mutation site `docs/rbac.md` §5 and
CLAUDE.md's "Audit everything mutating" invariant assume exists, but which no
M0-M4 task actually built (see `apps.rbac.serializers` module docstring for
the full gap-analysis note). Built minimally here so F8's "role change"
acceptance criterion has a real endpoint to exercise:

- `create` -- add a member (`user.manage`): grants `role` to `user`, scoped
  to `project` (`None` = tenant-wide). Audited under `user.manage`.
- `update`/`partial_update` -- **role change** (`role.assign`): the ONLY
  editable field is `role` (see `MembershipSerializer.get_fields`, which
  makes `user`/`project` read-only on an existing instance). Audited under
  `role.assign`, before/after capturing the OLD and NEW `role.key`.
- `destroy` -- remove a member (`user.manage`). Audited under `user.manage`.

Tenant scoping (golden-path step 2): `get_queryset()` builds
`Membership.objects...` (tenant-scoped, fail-closed manager) fresh per
request, never a class-level `queryset = ...` — same reasoning as
`apps.catalog.api`/`apps.assets.api`.

RBAC (golden-path step 3): `apps.rbac.permissions.MembershipPermission` --
see its docstring for the exact Admin-vs-ProjectLead scope rule (footnote 3).

List scoping (docs/rbac.md §1): an Admin (tenant-wide `user.manage`/
`role.assign` grant) sees every Membership in the tenant; a ProjectLead
(project-scoped grant only) sees only Memberships scoped to their own
project(s) -- never another project's, never tenant-wide ones (which are
Admin-only territory, footnote 3).
"""

from __future__ import annotations

import django_filters as filters
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, viewsets
from rest_framework.filters import OrderingFilter

from apps.audit.services import client_ip, write_audit_log
from apps.common.pagination import BoundedPageNumberPagination
from apps.rbac.permission_keys import ROLE_ASSIGN, USER_MANAGE

from .models import Membership
from .permissions import MembershipPermission
from .serializers import MembershipSerializer
from .services import get_viewable_project_scope


class MembershipFilterSet(filters.FilterSet):
    """`?user=`/`?role=`/`?project=` -- plain id-equality filters, deliberately
    NOT `django_filters`' auto-generated `ModelChoiceFilter` from a bare
    `filterset_fields` list/`Meta.fields` dict-notation, which builds its
    `queryset` kwarg from each FK's tenant-scoped manager -- same crash risk
    (`TenantContextError` at class-definition time for a module-level
    `FilterSet`) `apps.assets.api.AssetFilterSet`'s own docstring documents.
    A bare id-equality filter needs no queryset: `MembershipViewSet.
    get_queryset()` already returns a tenant-scoped `Membership` base
    queryset, so a cross-tenant id can never match any row here regardless.
    """

    user = filters.NumberFilter(field_name="user_id")
    role = filters.NumberFilter(field_name="role_id")
    project = filters.NumberFilter(field_name="project_id")

    class Meta:
        model = Membership
        fields = ["user", "role", "project"]


def _membership_snapshot(membership: Membership) -> dict:
    return {
        "user_id": membership.user_id,
        "role_key": membership.role.key,
        "project_id": membership.project_id,
    }


class MembershipViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = MembershipSerializer
    permission_classes = [MembershipPermission]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = MembershipFilterSet
    ordering_fields = ["created_at"]
    pagination_class = BoundedPageNumberPagination

    def get_queryset(self):
        # Tenant-scoped manager, resolved per-request (see module docstring).
        qs = Membership.objects.select_related("user", "role", "project").order_by("-created_at")
        if self.action == "list":
            tenant_wide, project_ids = get_viewable_project_scope(self.request.user, USER_MANAGE)
            if not tenant_wide:
                qs = qs.filter(project_id__in=project_ids) if project_ids else qs.none()
        return qs

    def perform_create(self, serializer):
        membership: Membership = serializer.save()
        write_audit_log(
            tenant_id=membership.tenant_id,
            actor=self.request.user,
            action=USER_MANAGE,
            entity_type="membership",
            entity_id=membership.id,
            before=None,
            after=_membership_snapshot(membership),
            ip=client_ip(self.request),
        )

    def perform_update(self, serializer):
        instance: Membership = serializer.instance
        before = _membership_snapshot(instance)
        membership: Membership = serializer.save()
        write_audit_log(
            tenant_id=membership.tenant_id,
            actor=self.request.user,
            # Updating an existing Membership is ONLY ever a role change
            # (see `MembershipSerializer.get_fields` -- `user`/`project` are
            # read-only past creation), so this is always the `role.assign`
            # action key, distinct from the `user.manage` create/destroy
            # entries above (docs/rbac.md §5).
            action=ROLE_ASSIGN,
            entity_type="membership",
            entity_id=membership.id,
            before=before,
            after=_membership_snapshot(membership),
            ip=client_ip(self.request),
        )

    def perform_destroy(self, instance: Membership):
        before = _membership_snapshot(instance)
        tenant_id = instance.tenant_id
        membership_id = instance.id
        instance.delete()
        write_audit_log(
            tenant_id=tenant_id,
            actor=self.request.user,
            action=USER_MANAGE,
            entity_type="membership",
            entity_id=membership_id,
            before=before,
            after=None,
            ip=client_ip(self.request),
        )
