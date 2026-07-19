"""Catalog viewsets (T1.1): categories (+ `/fields` sub-resource), locations,
projects, tags. Contract: `docs/api-and-ui.md` "Structure" table.

**Tenant scoping (golden-path step 2):** every `get_queryset()` below returns
`Model.objects...` — the tenant-scoped, fail-closed manager (T0.4) — built
INSIDE the method, never assigned to a class-level `queryset = ...`
attribute. A class-level assignment would evaluate
`TenantScopedManager.get_queryset()` at import time (Django app-loading,
long before any request/tenant context exists) and raise
`TenantContextError` immediately on startup; building it per-call defers
evaluation until a real request is being served, exactly like the serializer
`get_fields()` overrides in `apps.catalog.serializers`. RLS (T1.3) is the
DB-layer backstop on top of this once it lands on these tables.

**RBAC (golden-path step 3):** `Category`/`Location`/`Project` are tenant-wide
admin configuration (no `project_id` of their own) — see
`apps.catalog.permissions.TenantWideReadOrManage` for the exact
read-vs-write permission keys and the reasoning/assumptions for each
resource. `Tag` is read-only.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.response import Response

from apps.projects.models import Project
from apps.rbac.permission_keys import CATEGORY_MANAGE, LOCATION_MANAGE, TENANT_MANAGE

from .models import Category, CustomFieldDef, Location, Tag
from .permissions import TenantWideReadOrManage, TenantWideView
from .serializers import (
    CategorySerializer,
    CustomFieldDefSerializer,
    LocationSerializer,
    ProjectSerializer,
    TagSerializer,
)


class CategoryViewSet(viewsets.ModelViewSet):
    serializer_class = CategorySerializer
    permission_classes = [TenantWideReadOrManage(CATEGORY_MANAGE)]  # type: ignore[list-item]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = [
        "parent",
        "default_is_consumable",
        "requires_approval",
        "requires_calibration",
    ]
    search_fields = ["name"]
    ordering_fields = ["name", "created_at"]
    ordering = ["name"]

    def get_queryset(self):
        # Tenant-scoped manager, resolved per-request (see module docstring).
        return Category.objects.select_related("parent").prefetch_related("field_defs")

    def perform_create(self, serializer):
        # Tenant is derived from the authenticated session, never the client
        # (R4) — `request.user.tenant` is what `CurrentTenantMiddleware`
        # already scoped this whole request to.
        serializer.save(tenant=self.request.user.tenant)  # type: ignore[union-attr]

    @action(detail=True, methods=["get", "post"], url_path="fields")
    def fields(self, request, pk=None):
        """`GET/POST /api/v1/categories/{id}/fields` — custom field defs for
        this category (docs/api-and-ui.md). Permission is re-derived by the
        SAME class-level `TenantWideReadOrManage` (GET->asset.view,
        POST->category.manage) via the normal `check_permissions()` cycle;
        `get_object()` below also re-checks object-level permission and,
        crucially, re-scopes `pk` through the tenant-scoped queryset (a
        cross-tenant/guessed category id 404s here, never leaks a fields
        list).
        """
        category = self.get_object()
        if request.method.upper() == "POST":
            # `category` is passed via `context`, not `data` (it's a
            # read-only field, see `CustomFieldDefSerializer.Meta`) — the
            # serializer's `validate()` uses it for the tenant-aware
            # `(category, key)` uniqueness check (code-review finding: this
            # used to only surface as a DB `IntegrityError` -> 500).
            serializer = CustomFieldDefSerializer(
                data=request.data, context={"request": request, "category": category}
            )
            serializer.is_valid(raise_exception=True)
            serializer.save(tenant=request.user.tenant, category=category)
            return Response(serializer.data, status=201)

        field_defs = category.field_defs.order_by("order", "id")
        serializer = CustomFieldDefSerializer(field_defs, many=True)
        return Response(serializer.data)

    def _get_field_def_or_404(self, category: Category, field_id) -> CustomFieldDef:
        """Resolve `field_id` tenant-scoped AND under `category` (R4). Built
        from `CustomFieldDef.objects` (the tenant-scoped manager, never
        `.all_objects`) and additionally filtered by `category=category` (the
        URL's `{cat_id}`, itself already re-scoped by `get_object()` above) —
        a field id that belongs to another tenant OR to a DIFFERENT category
        than the URL's `{cat_id}` 404s here, never leaks/edits cross-tenant or
        cross-category. Never trusts the client-supplied `field_id` beyond
        using it as a lookup key.
        """
        return get_object_or_404(CustomFieldDef.objects.filter(category=category), pk=field_id)

    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=r"fields/(?P<field_id>\d+)",
    )
    def field_detail(self, request, pk=None, field_id=None):
        """`PATCH/DELETE /api/v1/categories/{id}/fields/{field_id}`
        (docs/api-and-ui.md). Permission: same `TenantWideReadOrManage`
        class-level check as `fields` above -- both methods here are writes,
        so both require `category.manage`. `category` is re-scoped via
        `get_object()` (tenant + object-level RBAC already enforced there);
        `field_id` is then re-scoped a second time, under that SAME category,
        by `_get_field_def_or_404` (R4).
        """
        category = self.get_object()
        field_def = self._get_field_def_or_404(category, field_id)

        if request.method.upper() == "DELETE":
            # Delete policy (documented in docs/api-and-ui.md): cascades to
            # `AssetFieldValue` rows for this field def -- already enforced at
            # the DB layer (`AssetFieldValue.field_def` is `on_delete=CASCADE`,
            # apps.assets.models), so no extra application logic is needed
            # here; this simply triggers that FK cascade.
            field_def.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = CustomFieldDefSerializer(
            field_def,
            data=request.data,
            partial=True,
            context={"request": request, "category": category},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="fields/reorder")
    def fields_reorder(self, request, pk=None):
        """`POST /api/v1/categories/{id}/fields/reorder` (docs/api-and-ui.md):
        body `{"order": [<field_id>, ...]}`, an ordered list of THIS
        category's field def ids (order in the list = new `order` value,
        0-indexed). Chosen over per-field `PATCH ... {"order": n}` calls
        because a full reorder is a single atomic operation -- doing it as N
        separate PATCHes risks a half-applied ordering if one fails partway,
        and needs a full list read-back anyway to compute each new value.
        """
        category = self.get_object()
        ordered_ids = request.data.get("order")
        if not isinstance(ordered_ids, list) or not ordered_ids:
            raise serializers.ValidationError({"order": "order must be a non-empty list of ids."})

        existing_ids = set(category.field_defs.values_list("id", flat=True))
        try:
            requested_ids = [int(i) for i in ordered_ids]
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError(
                {"order": "order must be a list of integer ids."}
            ) from exc

        # The submitted id set must be EXACTLY this category's field defs --
        # no more, no fewer, no duplicates, and (R4) no id smuggled in from
        # another category/tenant (an id from elsewhere simply isn't a
        # member of `existing_ids`, since that set is built from the
        # tenant-scoped, already-re-scoped `category`).
        if len(requested_ids) != len(set(requested_ids)) or set(requested_ids) != existing_ids:
            raise serializers.ValidationError(
                {"order": "order must contain exactly this category's field def ids, once each."}
            )

        field_defs_by_id = {fd.id: fd for fd in category.field_defs.all()}
        for index, field_id in enumerate(requested_ids):
            field_defs_by_id[field_id].order = index
        CustomFieldDef.objects.bulk_update(field_defs_by_id.values(), ["order"])

        field_defs = category.field_defs.order_by("order", "id")
        serializer = CustomFieldDefSerializer(field_defs, many=True)
        return Response(serializer.data)


class LocationViewSet(viewsets.ModelViewSet):
    serializer_class = LocationSerializer
    permission_classes = [TenantWideReadOrManage(LOCATION_MANAGE)]  # type: ignore[list-item]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["parent", "kind"]
    search_fields = ["name"]
    ordering_fields = ["name", "created_at"]
    ordering = ["name"]

    def get_queryset(self):
        return Location.objects.select_related("parent")

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant)  # type: ignore[union-attr]


class ProjectViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectSerializer
    # ASSUMPTION (flagged, see apps.catalog.permissions module docstring):
    # rbac.md has no dedicated `project.manage` key; Project writes are
    # gated by the Admin-only `tenant.manage` key as the closest documented
    # analog for tenant-structural configuration.
    permission_classes = [TenantWideReadOrManage(TENANT_MANAGE)]  # type: ignore[list-item]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    # Code-review finding #3 (tracked, fixed here since it's trivial):
    # `django_filters` would otherwise build its `lead_user` filter's
    # `ModelChoiceFilter` queryset from `User._default_manager`
    # (`all_objects` — deliberately unscoped, see `apps.accounts.models.User`
    # `Meta.default_manager_name`), which is a harmless-but-needless
    # cross-tenant user-id existence oracle (no data leak: `Project`'s own
    # queryset stays tenant-scoped either way). Not part of the documented
    # filter contract (`docs/api-and-ui.md` only lists `CRUD
    # /api/v1/projects`, no filters), so dropping it is a clean fix with no
    # functional loss.
    filterset_fields = ["is_active"]
    search_fields = ["name"]
    ordering_fields = ["name", "created_at"]
    ordering = ["name"]

    def get_queryset(self):
        return Project.objects.select_related("lead_user")

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant)  # type: ignore[union-attr]


class TagViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Read-only per `docs/api-and-ui.md` ("GET /api/v1/tags"): tags are
    created inline when tagging an asset (T1.2's `TagLink`), not through a
    dedicated admin CRUD surface here.
    """

    serializer_class = TagSerializer
    permission_classes = [TenantWideView()]  # type: ignore[list-item]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    search_fields = ["name"]
    ordering_fields = ["name"]
    ordering = ["name"]

    def get_queryset(self):
        return Tag.objects.all()
