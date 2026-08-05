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
from django.db import models, transaction
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import client_ip, write_audit_log
from apps.common.errors import problem_response
from apps.common.pagination import BoundedPageNumberPagination
from apps.rbac.permission_keys import (
    ROLE_ASSIGN,
    SYSTEM_ROLE_PERMISSIONS,
    TENANT_MANAGE,
    USER_MANAGE,
)

from .models import Membership, Permission, Role, UserPermissionOverride
from .permissions import MembershipPermission
from .serializers import (
    MembershipSerializer,
    PermissionSerializer,
    RoleSerializer,
    UserPermissionOverrideSerializer,
    UserPermissionsWriteSerializer,
    apply_role_permissions,
)
from .services import (
    get_effective_permissions,
    get_viewable_project_scope,
    invalidate_permission_override_cache,
    user_has_permission,
    user_has_permission_in_any_scope,
)


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


class RolePermissionClass(BasePermission):
    """Reads vs. writes on the role catalog are two different bars:

    - **Read** (`list`/`retrieve`): any `user.manage` grant, tenant-wide OR
      project-scoped — roles carry no sensitive data, and anyone who can
      grant a Membership needs this to discover which role ids exist.
    - **Write** (`create`/`update`/`partial_update`/`destroy`/`reset`):
      TENANT-WIDE `tenant.manage` only, i.e. an Admin. This is deliberately
      stricter than `user.manage`: editing a role's grants is editing the
      authorization rules themselves, so a ProjectLead (who holds
      `user.manage` scoped to their project, footnote 3) must never be able
      to hand themselves `category.manage` by editing the role they hold.
    """

    READ_ACTIONS = frozenset({"list", "retrieve"})

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return False
        action = getattr(view, "action", "") or ""
        if action in self.READ_ACTIONS:
            return user_has_permission_in_any_scope(user, USER_MANAGE)
        return user_has_permission(user, TENANT_MANAGE, project=None)


def _role_snapshot(role: Role) -> dict:
    return {
        "key": role.key,
        "name": role.name,
        "is_system": role.is_system,
        "is_customized": role.is_customized,
        "permission_keys": sorted(rp.permission.key for rp in role.role_permissions.all()),
    }


def assert_tenant_keeps_an_admin(tenant_id: int) -> None:
    """Guardrail against the one unrecoverable mistake this whole feature
    makes possible: editing roles/overrides until NOBODY in the tenant can
    administer it any more. Self-hosted Cortex has no break-glass path back
    — recovering would mean shell access to the NAS — so every write that
    can reduce privilege re-checks, INSIDE its transaction, that at least
    one active user still holds tenant-wide `tenant.manage` + `user.manage`,
    and rolls back with a 400 otherwise.

    Deliberately re-derived from scratch (not a cached/annotated count):
    it must reflect the state AFTER the write, including role edits, custom
    roles, and per-user DENY overrides all interacting.
    """
    for membership in (
        Membership.objects.filter(tenant_id=tenant_id, project__isnull=True)
        .select_related("user")
        .only("user", "project")
    ):
        user = membership.user
        if not user.is_active:
            continue
        perms = get_effective_permissions(user, project=None)
        if TENANT_MANAGE in perms and USER_MANAGE in perms:
            return
    raise serializers.ValidationError(
        "This change would leave no active user able to administer the tenant "
        "(tenant-wide 'tenant.manage' + 'user.manage'). Grant those to someone "
        "else first."
    )


class RoleViewSet(viewsets.ModelViewSet):
    """`GET/POST /api/v1/roles`, `GET/PATCH/DELETE /api/v1/roles/{id}`,
    `POST /api/v1/roles/{id}/reset` (docs/rbac.md §6).

    The 4 system roles are still seeded per tenant (T0.4) and still can't be
    deleted or re-keyed, but their permission SETS are now editable by an
    Admin — `docs/rbac.md` §3's matrix is the default, not a hard-coded law
    (the motivating case: a Project Lead who also needs `category.manage`).
    Tenants can additionally author their own roles.

    Every write is audited under `role.assign` with the before/after
    permission-key sets (docs/rbac.md §5), and re-checks
    `assert_tenant_keeps_an_admin` inside the transaction.
    """

    serializer_class = RoleSerializer
    permission_classes = [RolePermissionClass]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    pagination_class = BoundedPageNumberPagination

    def get_queryset(self):
        # Tenant-scoped manager, resolved per-request.
        return (
            Role.objects.all()
            .prefetch_related("role_permissions__permission")
            .annotate(membership_count=models.Count("memberships", distinct=True))
            .order_by("name")
        )

    def _audit(self, role: Role, before: dict | None, after: dict | None) -> None:
        write_audit_log(
            tenant_id=role.tenant_id,
            actor=self.request.user,
            action=ROLE_ASSIGN,
            entity_type="role",
            entity_id=role.id,
            before=before,
            after=after,
            ip=client_ip(self.request),
        )

    def perform_create(self, serializer):
        with transaction.atomic():
            role: Role = serializer.save(tenant=self.request.user.tenant)
            self._audit(role, None, _role_snapshot(role))

    def perform_update(self, serializer):
        instance: Role = serializer.instance
        before = _role_snapshot(instance)
        with transaction.atomic():
            role: Role = serializer.save()
            # Re-read grants after the diff so the audit "after" is the real
            # persisted set, not the prefetched pre-write one.
            role.refresh_from_db()
            role = (
                Role.objects.prefetch_related("role_permissions__permission")
                .filter(pk=role.pk)
                .first()
                or role
            )
            assert_tenant_keeps_an_admin(role.tenant_id)
            self._audit(role, before, _role_snapshot(role))

    def perform_destroy(self, instance: Role):
        if instance.is_system:
            raise serializers.ValidationError(
                "A system role cannot be deleted. Edit its permissions, or create a "
                "custom role instead."
            )
        if instance.memberships.exists():
            raise serializers.ValidationError(
                "This role is still assigned to at least one user. Reassign those "
                "memberships first."
            )
        before = _role_snapshot(instance)
        tenant_id = instance.tenant_id
        role_id = instance.id
        with transaction.atomic():
            instance.delete()
            write_audit_log(
                tenant_id=tenant_id,
                actor=self.request.user,
                action=ROLE_ASSIGN,
                entity_type="role",
                entity_id=role_id,
                before=before,
                after=None,
                ip=client_ip(self.request),
            )

    @action(detail=True, methods=["post"], url_path="reset")
    def reset(self, request, pk=None):
        """Restore a SYSTEM role's `docs/rbac.md` §3 default grants and clear
        `is_customized` (which re-enables `apps.rbac.seed`'s idempotent
        re-seeding for it). A custom role has no defaults to restore."""
        role: Role = self.get_object()
        if not role.is_system:
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Not a system role",
                detail="Only the seeded system roles have defaults to reset to.",
            )
        before = _role_snapshot(role)
        with transaction.atomic():
            apply_role_permissions(role, sorted(SYSTEM_ROLE_PERMISSIONS[role.key]))
            role.is_customized = False
            role.save(update_fields=["is_customized"])
            assert_tenant_keeps_an_admin(role.tenant_id)
            role = (
                Role.objects.prefetch_related("role_permissions__permission")
                .filter(pk=role.pk)
                .first()
                or role
            )
            self._audit(role, before, _role_snapshot(role))
        return Response(self.get_serializer(role).data)


class PermissionCatalogView(APIView):
    """`GET /api/v1/permissions` — the fixed, system-wide permission
    vocabulary (NOT tenant-owned, see `apps.rbac.models.Permission`), so the
    admin UI can render its checkbox matrix from the server's own list
    instead of a hard-coded frontend copy that would drift from
    `permission_keys.py`.

    Unpaginated by design: the vocabulary is a couple of dozen fixed rows
    and the UI needs all of them at once to render a complete matrix.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not user_has_permission_in_any_scope(request.user, USER_MANAGE):
            return problem_response(
                status_code=status.HTTP_403_FORBIDDEN,
                title="Forbidden",
                detail="You do not have permission to view the permission catalog.",
            )
        permissions = Permission.objects.all().order_by("key")
        return Response({"results": PermissionSerializer(permissions, many=True).data})


class UserPermissionsView(APIView):
    """`GET/PUT /api/v1/users/{user_id}/permissions` (docs/rbac.md §6) — the
    per-user override layer an Admin edits from Users & Roles.

    `GET` returns the full picture for one user so the UI can render a
    tri-state matrix without re-deriving RBAC client-side:
    `role_permission_keys` (what their memberships grant, tenant-wide),
    `overrides` (`{key: "grant"|"deny"}`), and `effective_permission_keys`
    (what the server will actually enforce, tenant-wide).

    `PUT` REPLACES the whole override set (see
    `UserPermissionsWriteSerializer`), audited under `role.assign` with the
    before/after override maps, and re-checked by
    `assert_tenant_keeps_an_admin` inside the transaction so an admin can't
    DENY the tenant's last administrator out of existence.

    Gate: TENANT-WIDE `role.assign` + `user.manage` (an Admin). Never a
    ProjectLead — these overrides are tenant-wide by construction
    (`UserPermissionOverride` docstring), so a project-scoped grant is not a
    sufficient basis for writing one.
    """

    permission_classes = [IsAuthenticated]

    def _get_target_user(self, request, user_id: int):
        from apps.accounts.models import User

        # Tenant-scoped manager (R4): a user id from another tenant simply
        # does not resolve here.
        return User.objects.filter(pk=user_id).first()

    def _forbidden(self):
        return problem_response(
            status_code=status.HTTP_403_FORBIDDEN,
            title="Forbidden",
            detail="Only a tenant administrator can view or change per-user permissions.",
        )

    def _not_found(self):
        return problem_response(
            status_code=status.HTTP_404_NOT_FOUND,
            title="Not found",
            detail="No such user in this tenant.",
        )

    def _is_admin(self, user) -> bool:
        return user_has_permission(user, ROLE_ASSIGN, project=None) and user_has_permission(
            user, USER_MANAGE, project=None
        )

    def _payload(self, target) -> dict:
        role_permission_keys: set[str] = set()
        for membership in (
            Membership.objects.filter(user=target, project__isnull=True)
            .select_related("role")
            .prefetch_related("role__role_permissions__permission")
        ):
            role_permission_keys.update(
                rp.permission.key for rp in membership.role.role_permissions.all()
            )
        overrides = UserPermissionOverride.objects.filter(user=target).select_related("permission")
        return {
            "user": target.id,
            "user_email": target.email,
            "role_permission_keys": sorted(role_permission_keys),
            "overrides": {o.permission.key: o.effect for o in overrides},
            "override_rows": UserPermissionOverrideSerializer(overrides, many=True).data,
            "effective_permission_keys": sorted(get_effective_permissions(target, project=None)),
        }

    def get(self, request, user_id: int):
        if not self._is_admin(request.user):
            return self._forbidden()
        target = self._get_target_user(request, user_id)
        if target is None:
            return self._not_found()
        return Response(self._payload(target))

    def put(self, request, user_id: int):
        if not self._is_admin(request.user):
            return self._forbidden()
        target = self._get_target_user(request, user_id)
        if target is None:
            return self._not_found()

        serializer = UserPermissionsWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        wanted: dict[str, str] = serializer.validated_data["overrides"]

        existing_rows = list(
            UserPermissionOverride.objects.filter(user=target).select_related("permission")
        )
        before = {row.permission.key: row.effect for row in existing_rows}

        with transaction.atomic():
            permissions_by_key = {p.key: p for p in Permission.objects.filter(key__in=list(wanted))}
            existing_by_key = {row.permission.key: row for row in existing_rows}

            for key, row in existing_by_key.items():
                if key not in wanted:
                    row.delete()
                elif row.effect != wanted[key]:
                    row.effect = wanted[key]
                    row.save(update_fields=["effect", "updated_at"])
            for key, effect in wanted.items():
                if key not in existing_by_key:
                    UserPermissionOverride.objects.create(
                        tenant_id=target.tenant_id,
                        user=target,
                        permission=permissions_by_key[key],
                        effect=effect,
                        created_by=request.user,
                    )

            # The resolvers memoize overrides per user INSTANCE; `target` is
            # the instance the post-write payload/guardrail below read from.
            invalidate_permission_override_cache(target)
            assert_tenant_keeps_an_admin(target.tenant_id)

            write_audit_log(
                tenant_id=target.tenant_id,
                actor=request.user,
                action=ROLE_ASSIGN,
                entity_type="user_permission_overrides",
                entity_id=target.id,
                before={"overrides": before},
                after={"overrides": wanted},
                ip=client_ip(request),
            )

        invalidate_permission_override_cache(target)
        return Response(self._payload(target))
