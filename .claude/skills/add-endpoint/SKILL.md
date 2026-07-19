---
name: add-endpoint
description: >-
  The golden-path checklist for adding or changing a DRF endpoint in the LMS
  backend so it is correctly tenant-scoped, RBAC-enforced, audited, paginated, and
  tested. Use whenever implementing any `/api/v1/...` endpoint or viewset — this is
  the R4 (tenant-leak) hotspot repeated across every milestone. Trigger cues: "add
  an endpoint", "new DRF view/viewset", "expose an API", "new /api/v1 route".
---

# Adding a DRF endpoint (LMS golden path)

Follow this every time. A single skipped step here is how tenant data leaks (R4) or
an unaudited mutation slips through. Read `docs/api-and-ui.md` (contract),
`docs/rbac.md` (permission keys + scope), and `docs/data-model.md` first.

## Checklist

1. **Locate the contract.** Find the endpoint in `docs/api-and-ui.md` — path, method,
   filters, purpose. Match it exactly (path under `/api/v1`, plural nouns, documented
   query params). If it isn't in the contract, stop and confirm the shape before inventing one.

2. **Tenant scoping (never optional).** The queryset MUST come from the central
   tenant-scoped base manager that injects the tenant from the authenticated session.
   - Never `Model.objects.all()` on a tenant-owned model in a view.
   - Never accept a `tenant_id` (or any tenant selector) from the client.
   - A foreign key or id in the request body is only valid if it also resolves inside
     the current tenant — re-scope it, don't trust it.

3. **RBAC after tenant isolation.** Attach the DRF permission that checks the exact
   permission key from the `docs/rbac.md` matrix, using the union-of-memberships scope
   rule (tenant-wide vs project-scoped; general-pool assets need a tenant-wide grant).
   Enforce server-side even if the UI hides the action. Unauthorized → 403, not 404-by-omission.

4. **Pagination & filtering (list endpoints).** Paginated (`?page`/`?page_size`, cursor
   option for large sets), plus `?search=`, `?ordering=`, and every field filter the
   contract lists. Use `select_related`/`prefetch_related` to avoid N+1s.

5. **Audit (mutating endpoints).** If the action is in `docs/rbac.md` §5 (any `*.approve`,
   `*.override`, `user.manage`, `role.assign`, `stock.adjust`, `asset.retire`, checkout,
   reservation change), write an immutable `AuditLog` entry with actor, before/after
   JSONB, and ip — in the same transaction as the mutation.

6. **Async for slow work.** Anything slow (email, PDF, import, export) is enqueued to
   Celery and the endpoint returns immediately (often a job id polled via `/jobs/{id}`).
   Email only via the `EmailProvider` interface — never import Brevo in a view.

7. **Errors & idempotency.** RFC-7807 problem+json errors. Make writes idempotent where
   sensible (e.g. check-in). Throttle auth-sensitive routes.

8. **Test before done (or hand to qa-test-engineer).** At minimum:
   - a **cross-tenant** test: tenant-B object returns 404/403, including with a guessed id;
   - an **RBAC** test: a role lacking the permission gets a server-side 403;
   - for mutations, an **audit** test: the entry is written and is immutable;
   - for lists, a **query-count/perf** assertion to catch N+1s.

## Definition of done
The endpoint matches the contract, uses the tenant-scoped manager, enforces the right
permission key server-side, audits mutations, paginates/filters lists, and has the
cross-tenant + RBAC tests passing. Then it's ready for **code-reviewer**.
