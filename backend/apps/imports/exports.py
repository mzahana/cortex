"""`GET /api/v1/exports/assets.csv` (T6.1): filtered CSV export, honoring the
SAME query-param filters as `GET /api/v1/assets` and RBAC-scoped the same
way (`asset.export`, `docs/rbac.md` §3: Admin ✅ tenant-wide, Project Lead 🟡
scoped to their own project's assets, Member ✅ tenant-wide, Viewer ➖).

**Reuses, never duplicates, `apps.assets.api`'s filter/queryset logic**
(task instructions): the base "which assets are visible" queryset is
`apps.assets.api.visible_assets_queryset` (the SAME helper `AssetViewSet.
get_queryset` uses for `list`, just keyed by `ASSET_EXPORT` instead of
`ASSET_VIEW`), and `?category=`/`?location=`/`?project=`/`?tag=`/`?status=`/
`?is_consumable=`/`?search=` are applied by the SAME `AssetFilterSet`/
`AssetSearchFilter` classes `AssetViewSet` uses, via `GenericAPIView.
filter_queryset()` — no separate filter implementation exists here at all.

**Synchronous, streamed response — not a Celery job (deliberate MVP
choice).** M1 (`docs/tasks/M1-asset-registry.md`'s perf work) already proved
the underlying list query is fast/well-indexed at 10k+ assets; a plain CSV
row format (unlike the label PDF's CPU-heavy WeasyPrint render) is cheap
per-row to write, and `StreamingHttpResponse` means the response body is
generated and flushed to the client incrementally rather than buffered
whole in memory — so this comfortably fits inside a normal request/response
cycle without blocking a worker the way a slow synchronous view would.
Revisit as a background job only if a future tenant's asset count grows
far past the M1-proven range.

**Round-trip with the importer (T6.1 exit criterion).** Columns are exactly
`apps.imports.services`'s expected import schema: `name, category, location,
status, condition, project, tags`, plus one column per DISTINCT
custom-field KEY used by ANY exported asset's category (a category whose
fields a given row doesn't have simply gets a blank cell for that column) —
re-importing an unmodified export via `POST /imports` (auto-mapping,
`apps.imports.services.default_column_mapping`, matches every one of these
headers automatically) reproduces equivalent assets.
"""

from __future__ import annotations

import csv

from django.http import StreamingHttpResponse
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import generics, permissions
from rest_framework.filters import OrderingFilter

from apps.assets.api import AssetFilterSet, AssetSearchFilter, visible_assets_queryset
from apps.catalog.models import CustomFieldDef
from apps.rbac.permission_keys import ASSET_EXPORT
from apps.tenancy.context import tenant_context

from .permissions import AssetExportPermission

CORE_EXPORT_COLUMNS = [
    "name",
    "category",
    "location",
    "status",
    "condition",
    "project",
    "tags",
    "url",
]


class _EchoWriter:
    """A file-like object whose `write()` just returns what it was given —
    `csv.writer` normally writes to a real file; pairing it with this lets
    each formatted CSV line be yielded straight into a `StreamingHttpResponse`
    generator instead of buffering the whole file (same technique Django's
    own streaming-CSV-export docs recommend).
    """

    def write(self, value):
        return value


class AssetExportView(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, AssetExportPermission]
    filter_backends = [DjangoFilterBackend, AssetSearchFilter, OrderingFilter]
    filterset_class = AssetFilterSet
    ordering_fields = ["name", "created_at", "status", "purchase_date"]

    def get_queryset(self):
        return visible_assets_queryset(self.request, ASSET_EXPORT)

    def get(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        # One extra, small query: which custom-field columns does THIS
        # filtered result set actually need? (Distinct field keys across the
        # distinct categories present — not one query per asset.)
        category_ids = queryset.values_list("category_id", flat=True).distinct()
        field_defs = list(
            CustomFieldDef.objects.filter(category_id__in=list(category_ids)).order_by(
                "category_id", "order", "id"
            )
        )
        seen_keys: set[str] = set()
        custom_columns: list[str] = []
        for field_def in field_defs:
            if field_def.key not in seen_keys:
                seen_keys.add(field_def.key)
                custom_columns.append(field_def.key)

        header = CORE_EXPORT_COLUMNS + custom_columns

        # **Bug found/fixed while writing this view's tests.** A
        # `StreamingHttpResponse`'s generator body runs LAZILY, as the WSGI
        # server pulls chunks from it — i.e. AFTER this view has returned
        # and `CurrentTenantMiddleware` has already unwound/cleared the
        # request's tenant context in its own `finally`. `queryset` itself
        # was already scoped (its `WHERE tenant_id = ...` is baked into the
        # query at `filter_queryset()` time, above, while still inside the
        # request's tenant context), but `.iterator()`'s per-chunk
        # `prefetch_related` (`field_values`, `tag_links`) re-resolves its
        # OWN tenant-scoped queryset lazily too, at iteration time — which
        # would otherwise hit `TenantContextError` (fail-closed) mid-stream.
        # Explicitly re-entering `tenant_context(tenant_id)` for the whole
        # generator body (captured now, still inside the real request
        # context) fixes this without losing genuine streaming.
        tenant_id = request.user.tenant_id

        def rows():
            with tenant_context(tenant_id):
                writer = csv.writer(_EchoWriter())
                yield writer.writerow(header)
                for asset in queryset.iterator(chunk_size=200):
                    values_by_key = {fv.field_def.key: fv.value for fv in asset.field_values.all()}
                    row = [
                        asset.name,
                        asset.category.name if asset.category else "",
                        asset.location.name if asset.location else "",
                        asset.status,
                        asset.condition,
                        asset.project.name if asset.project else "",
                        ", ".join(sorted(link.tag.name for link in asset.tag_links.all())),
                        asset.url,
                    ]
                    for key in custom_columns:
                        value = values_by_key.get(key, "")
                        row.append("" if value is None else value)
                    yield writer.writerow(row)

        response = StreamingHttpResponse(rows(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="assets.csv"'
        return response
