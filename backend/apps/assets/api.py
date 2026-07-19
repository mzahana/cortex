"""Asset endpoints (T1.2): CRUD (no hard delete — `docs/api-and-ui.md` lists
only `GET/PATCH /api/v1/assets/{id}`, no `PUT`/`DELETE`; the lifecycle exit is
the dedicated `retire` action, which RETAINS the row per `docs/data-model.md`
§4), `POST /{id}/retire`, `POST /{id}/attachments`.

Tenant scoping (golden-path step 2): `get_queryset()` builds `Asset.objects...`
(the tenant-scoped, fail-closed manager) fresh per request, never a
class-level `queryset = ...` — same reasoning as `apps.catalog.api`.

RBAC (golden-path step 3): `apps.assets.permissions.AssetPermission` — see
its module docstring for the exact scoped rule per action.

Pagination & search & filters (golden-path step 4, T1.4): `list` supports
bounded `?page`/`?page_size` (default) or `?cursor=` (opt-in, large-set seek
pagination) — see `paginator` below and `apps.common.pagination`. `?search=`
combines FTS (`websearch_to_tsquery('english', ...)` over `search_vector`,
hits `assets_asset_search_vector_gin`) with `pg_trgm` fuzzy matching on
`name`/`serial_number` (`assets_asset_name_trgm`/`_serial_number_trgm`) — see
`AssetSearchFilter`. `?category=`/`?status=`/`?location=`/`?project=`/
`?tag=`/`?is_consumable=` are whitelisted id-equality filters — see
`AssetFilterSet`. `?ordering=` is whitelisted via `ordering_fields` (no
arbitrary column injection). List rows are additionally scope-filtered
per `docs/rbac.md` §1 in `get_queryset()` — see the T1.4 comment there.

Audit (golden-path step 5): `retire` is in `docs/rbac.md` §5's mandatory
audit list — writes an `AuditLog` entry (before/after status, actor, ip) in
the same request/transaction. Attachment upload is NOT in that list (only
`asset.retire` is, among asset actions), so it is not separately audited.
"""

from __future__ import annotations

import django_filters as filters
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F, Prefetch, Q
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import BaseFilterBackend, OrderingFilter
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.audit.services import client_ip, write_audit_log
from apps.common.errors import problem_response
from apps.common.pagination import AssetCursorPagination, BoundedPageNumberPagination
from apps.rbac.permission_keys import ASSET_RETIRE, ASSET_VIEW
from apps.rbac.services import get_viewable_project_scope

from .models import Asset, AssetFieldValue, Attachment
from .permissions import AssetPermission
from .serializers import AssetSerializer, AttachmentSerializer
from .services import save_attachment_file, validate_attachment_upload


class AssetFilterSet(filters.FilterSet):
    """`?category=`/`?status=`/`?location=`/`?project=`/`?tag=`/
    `?is_consumable=` (docs/api-and-ui.md's documented Asset list facets,
    T1.4).

    Every FK facet is a plain **id-equality** filter (`NumberFilter`/a custom
    `method=`), deliberately NOT `django_filters`' auto-generated
    `ModelChoiceFilter` from `Meta.fields` dict-notation. That auto-generated
    filter builds its `queryset` kwarg from `Category`/`Location`/`Project`'s
    DEFAULT manager (`TenantScopedManager`) at FILTERSET-CLASS-DEFINITION
    TIME — Django app-loading, long before any request/tenant context exists
    — which would raise `TenantContextError` immediately on startup, exactly
    the crash `apps.catalog.serializers.get_fields()` and
    `apps.catalog.api.get_queryset()` both exist to avoid by deferring
    tenant-scoped queryset construction to request time. A bare id-equality
    filter needs no queryset at all: `AssetViewSet.get_queryset()` already
    returns a tenant-scoped `Asset` base queryset, and every `Asset` row's
    own `category_id`/`location_id`/`project_id`/tag links were themselves
    re-scoped to the caller's tenant at write time (`apps.assets.serializers`)
    — so a category/location/project/tag id belonging to another tenant can
    never match any row here regardless (R4-safe with zero extra validation:
    "doesn't leak" reduces to "matches nothing", the same as any other
    non-existent id).
    """

    category = filters.NumberFilter(field_name="category_id")
    location = filters.NumberFilter(field_name="location_id")
    project = filters.NumberFilter(field_name="project_id")
    tag = filters.NumberFilter(method="filter_tag")
    status = filters.ChoiceFilter(choices=Asset.Status.choices)
    is_consumable = filters.BooleanFilter()

    class Meta:
        model = Asset
        fields = ["category", "location", "project", "tag", "status", "is_consumable"]

    def filter_tag(self, queryset, name, value):
        # `TagLink` has `uniq_tag_link_asset_tag` (asset, tag) — at most one
        # matching row per asset, so no `.distinct()` needed to avoid
        # duplicate rows from the join.
        return queryset.filter(tag_links__tag_id=value)


class AssetSearchFilter(BaseFilterBackend):
    """`?search=` (docs/api-and-ui.md: "FTS+fuzzy").

    Full-text over `search_vector` via **`websearch_to_tsquery('english',
    :q)`** — the exact config/function T1.3's `assets_asset_search_vector_gin`
    GIN index was built for (a different config makes the index ineligible,
    per that migration's own module docstring) — UNIONed (single query, `OR`)
    with `pg_trgm` fuzzy matching on `name`/`serial_number` via the `%`
    similarity operator (Django's `__trigram_similar` lookup), which is what
    `assets_asset_name_trgm`/`assets_asset_serial_number_trgm` are built to
    serve. One extra `WHERE`/annotation on the existing query — not an extra
    round-trip — so this stays inside the bounded query-count budget T1.4/
    T1.8 assert.

    Relevance: matches are annotated with `SearchRank` (respects the T1.3
    trigger's A/B/B/C name/serial+tags/custom-value weights) and, when the
    caller hasn't asked for a specific `?ordering=`, results are ordered by
    that rank (trigram-only matches rank 0 and fall back to `-created_at`).
    An explicit `?ordering=` always wins — `OrderingFilter` runs its own
    `order_by()` after this backend in `AssetViewSet.filter_backends`.
    """

    search_param = "search"

    def filter_queryset(self, request, queryset, view):
        search = request.query_params.get(self.search_param, "").strip()
        if not search:
            return queryset

        ts_query = SearchQuery(search, config="english", search_type="websearch")
        matched = queryset.filter(
            Q(search_vector=ts_query)
            | Q(name__trigram_similar=search)
            | Q(serial_number__trigram_similar=search)
        ).annotate(rank=SearchRank(F("search_vector"), ts_query))

        if request.query_params.get("ordering"):
            return matched
        return matched.order_by("-rank", "-created_at")


class AssetViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = AssetSerializer
    permission_classes = [AssetPermission]
    # No PUT (full replace) or DELETE — contract is GET/PATCH + `retire`
    # (docs/api-and-ui.md); rows are never hard-deleted (docs/data-model.md §4).
    http_method_names = ["get", "post", "patch", "head", "options"]
    filter_backends = [DjangoFilterBackend, AssetSearchFilter, OrderingFilter]
    filterset_class = AssetFilterSet
    ordering_fields = ["name", "created_at", "status", "purchase_date"]
    # Deliberately NO class-level `ordering = [...]` (code-review finding,
    # T1.4 follow-up: relevance ranking was dead code). DRF's `OrderingFilter`
    # runs AFTER `AssetSearchFilter` in `filter_backends`, and — regardless of
    # whether the request has a `?search=` term — `get_default_ordering()`
    # falls back to this class attribute and UNCONDITIONALLY calls
    # `.order_by(*ordering)` whenever it's non-empty, even with no `?ordering=`
    # param on the request. That silently clobbered (replaced, not appended
    # to) `AssetSearchFilter`'s own `.order_by("-rank", "-created_at")` on
    # every plain `?search=` request, so relevance ranking NEVER actually took
    # effect. With this attribute absent, `OrderingFilter.get_default_ordering`
    # returns `None` and `filter_queryset` is a no-op when the client passes
    # no `?ordering=` — letting `AssetSearchFilter`'s rank ordering (search
    # present) or `get_queryset()`'s own explicit `-created_at` (no search)
    # stand. An explicit `?ordering=...` from the client still always wins
    # (whitelisted via `ordering_fields`, applied by `OrderingFilter` last).
    # `docs/data-model.md`'s stable non-search default is instead guaranteed
    # explicitly in `get_queryset()` below, not implicitly via this attribute.
    # `?page`/`?page_size` (bounded, see `apps.common.pagination`) is the
    # default; `?cursor=...` opts into the cursor-pagination mode
    # (docs/api-and-ui.md's "cursor option for large sets") — see `paginator`
    # below, which switches per-request.
    pagination_class = BoundedPageNumberPagination

    @property
    def paginator(self) -> AssetCursorPagination | BoundedPageNumberPagination:
        # Overrides `GenericAPIView.paginator`'s cached-property lookup of
        # the class-level `pagination_class` so the SAME endpoint can serve
        # either pagination style per-request: a client that already knows it
        # wants seek/cursor pagination (large sets, stable ordering, no
        # `OFFSET` cost) opts in with `?cursor=`; everyone else gets the
        # default bounded `?page`/`?page_size`.
        if not hasattr(self, "_paginator"):
            query_params = getattr(self.request, "query_params", {})
            paginator: AssetCursorPagination | BoundedPageNumberPagination
            if "cursor" in query_params:
                paginator = AssetCursorPagination()
            else:
                paginator = BoundedPageNumberPagination()
            self._paginator = paginator
        return self._paginator

    def get_queryset(self):
        # Tenant-scoped manager, resolved per-request (see module docstring).
        # `.order_by("-created_at")` here (not a class-level `ordering`
        # attribute — see that attribute's removal note above) is the stable
        # default for a non-search list with no explicit `?ordering=`:
        # `AssetSearchFilter` only overrides it (`-rank, -created_at`) when a
        # `?search=` term is present and no `?ordering=` was given;
        # `OrderingFilter` overrides it whenever `?ordering=` IS given; absent
        # both, this order stands untouched all the way to the response.
        qs = (
            Asset.objects.select_related("category", "project", "location", "current_workload_user")
            .prefetch_related(
                Prefetch(
                    "field_values", queryset=AssetFieldValue.objects.select_related("field_def")
                ),
                "attachments",
                "tag_links__tag",
            )
            .order_by("-created_at")
        )
        if self.action == "list":
            # Retired assets are hidden from the default list (docs/data-model.md
            # §4: "retire hides from default lists but retains the row") — but
            # a caller that explicitly asks for them (`?include_retired=true`
            # or `?status=retired`) still gets them; `retrieve`/`retire`/
            # `attachments` (detail routes) are NEVER filtered here, so a
            # retired asset stays fetchable by id regardless.
            include_retired = self.request.query_params.get("include_retired", "").lower() == "true"
            status_param = self.request.query_params.get("status")
            if not include_retired and status_param != Asset.Status.RETIRED:
                qs = qs.exclude(status=Asset.Status.RETIRED)

            # Scope-aware listing (docs/rbac.md §1, T1.4): a tenant-wide
            # `asset.view` grant sees every asset in the tenant; a user whose
            # ONLY `asset.view` grant(s) are project-scoped sees ONLY those
            # project(s)' assets — never another project's, never the
            # general pool (general-pool assets are governed by tenant-wide
            # grants only). `AssetPermission.has_permission` already let a
            # "holds it somewhere" caller reach `list` (the pure-ProjectLead
            # over-deny fix); this is what makes the ROWS returned match that
            # same scope precisely, instead of a blanket tenant-wide list.
            tenant_wide, project_ids = get_viewable_project_scope(self.request.user, ASSET_VIEW)
            if not tenant_wide:
                qs = qs.filter(project_id__in=project_ids) if project_ids else qs.none()
        return qs

    @action(detail=True, methods=["post"])
    def retire(self, request, pk=None):
        asset = self.get_object()  # tenant + object-level RBAC (ASSET_RETIRE) already enforced

        if asset.status == Asset.Status.RETIRED:
            # Idempotent (docs/api-and-ui.md §1: "writes are idempotent where
            # sensible") — re-retiring an already-retired asset is a no-op,
            # not an error, and does not write a duplicate audit entry.
            return Response(self.get_serializer(asset).data, status=status.HTTP_200_OK)

        before = {"status": asset.status, "retired_at": None}
        asset.status = Asset.Status.RETIRED
        asset.retired_at = timezone.now()
        asset.save(update_fields=["status", "retired_at", "updated_at"])
        after = {"status": asset.status, "retired_at": asset.retired_at.isoformat()}

        write_audit_log(
            tenant_id=asset.tenant_id,
            actor=request.user,
            action=ASSET_RETIRE,
            entity_type="asset",
            entity_id=asset.id,
            before=before,
            after=after,
            ip=client_ip(request),
        )

        return Response(self.get_serializer(asset).data, status=status.HTTP_200_OK)

    @action(
        detail=True,
        methods=["post"],
        url_path="attachments",
        parser_classes=[MultiPartParser, FormParser],
    )
    def attachments(self, request, pk=None):
        asset = self.get_object()  # tenant + object-level RBAC (ASSET_ATTACH) already enforced

        uploaded_file = request.FILES.get("file")
        if uploaded_file is None:
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Missing file",
                detail="A multipart 'file' field is required.",
            )

        kind = request.data.get("kind", "photo")
        if kind not in ("photo", "doc"):
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Invalid kind",
                detail="'kind' must be 'photo' or 'doc'.",
            )

        # Size cap + content-type/extension allowlist (code-review hardening
        # finding) — validated BEFORE any bytes are written to the storage
        # backend. `validate_attachment_upload` raises `serializers.
        # ValidationError` on rejection, which DRF's own exception handling
        # (`apps.common.errors.rfc7807_exception_handler`) turns into a
        # clean 400 problem+json response, same as every other validation
        # error in this app.
        validate_attachment_upload(kind=kind, uploaded_file=uploaded_file)

        # Bytes go to the storage backend (volume via django-storages); only
        # the returned key + metadata is ever persisted below.
        storage_key, content_type, size = save_attachment_file(
            tenant_id=asset.tenant_id, asset_id=asset.id, uploaded_file=uploaded_file
        )
        attachment = Attachment.objects.create(
            tenant=asset.tenant,
            asset=asset,
            kind=kind,
            storage_key=storage_key,
            filename=uploaded_file.name,
            content_type=content_type,
            size=size,
            uploaded_by=request.user,
        )
        return Response(AttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED)
