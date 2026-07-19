"""Shared list-endpoint pagination (T1.4, `docs/api-and-ui.md` §1: "every list
is paginated (`?page`, `?page_size`, cursor option for large sets)").

Two classes, one per documented mode:

- `BoundedPageNumberPagination` — the default `?page`/`?page_size` mode
  (`config/settings/base.py` already sets `DEFAULT_PAGINATION_CLASS`/
  `PAGE_SIZE=25` tenant-wide; this per-viewset subclass adds the bounded
  `max_page_size` the base DRF setting doesn't provide, so a client can never
  force an unbounded "load-all" page via a huge `?page_size=` value — see
  CLAUDE.md "Lists are server-side paginated... frontend never loads 'all
  assets'").
- `AssetCursorPagination` — the `?cursor=` opt-in for large result sets
  (stable, index-friendly seek pagination instead of `OFFSET`, which degrades
  on large tables). A client asks for cursor mode explicitly; see
  `apps.assets.api.AssetViewSet.pagination_class`, which switches between the
  two per-request based on whether `?cursor=` is present.
"""

from __future__ import annotations

from rest_framework.pagination import CursorPagination, PageNumberPagination


class BoundedPageNumberPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class AssetCursorPagination(CursorPagination):
    # Ordered by `-created_at` to match the default list ordering
    # (`Asset.Meta.ordering`) — cursor pagination requires a single,
    # consistent, indexed ordering to produce a stable cursor; ties are
    # broken by `pk` automatically (`CursorPagination` appends the model's
    # `pk` as a secondary ordering key when needed).
    page_size = 25
    max_page_size = 100
    page_size_query_param = "page_size"
    ordering = "-created_at"
