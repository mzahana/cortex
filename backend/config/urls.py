"""Root URLconf (T0.3).

`/api/v1` mounts the DRF router; apps register their viewsets on `router` as
they land (T0.4+). `/healthz` stays a stable, unauthenticated liveness
endpoint — note it is actually served by `config.middleware.HealthCheckMiddleware`
*before* URL resolution even runs (see that module for why); the route below
exists mainly for `reverse()`/documentation/tests that go through the normal
test client with an allowed Host header.
"""

from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.accounts.api import (
    ChangePasswordView,
    CsrfView,
    LoginView,
    LogoutView,
    MeView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    UserViewSet,
)
from apps.assets.api import AssetResolveView, AssetViewSet
from apps.audit.api import AuditLogViewSet
from apps.catalog.api import CategoryViewSet, LocationViewSet, TagViewSet
from apps.dashboard.api import DashboardSummaryView
from apps.imports.api import ImportCommitView, ImportDetailView, ImportUploadView
from apps.imports.exports import AssetExportView
from apps.jobs.api import JobRetrieveView
from apps.labels.api import LabelGenerateView
from apps.notifications.api import (
    EmailSettingsTestView,
    EmailSettingsView,
    NotificationPrefViewSet,
)
from apps.projects.api import (
    ExpenseAttachmentViewSet,
    ExpenseCategoryViewSet,
    ExpenseViewSet,
    ProjectDocumentViewSet,
    ProjectViewSet,
)
from apps.rbac.api import MembershipViewSet, RoleViewSet
from apps.reservations.api import ReservationViewSet
from apps.reservations.checkout import CheckoutViewSet
from apps.stock.api import ReorderRequestViewSet, StockItemViewSet

router = DefaultRouter()
router.register("categories", CategoryViewSet, basename="category")
router.register("locations", LocationViewSet, basename="location")
# M7 (docs/tasks/M7-project-grants.md): `apps.projects.api.ProjectViewSet` is
# the SOLE owner of the `/api/v1/projects` route — it supersedes (and is
# contract-compatible with, for list/create) `apps.catalog.api.
# ProjectViewSet`, which stays defined but UNREGISTERED (see
# `apps.projects.api` module docstring "Route ownership"). `expenses`/
# `documents` are separate top-level resources for their own
# retrieve/update/destroy routes; list/create for both live nested under
# `/projects/{id}/expenses` and `/projects/{id}/documents` respectively
# (custom actions on `ProjectViewSet`), never registered as their own
# router prefix, so a client can never list/create either without a project
# context.
router.register("projects", ProjectViewSet, basename="project")
router.register("expenses", ExpenseViewSet, basename="expense")
router.register("documents", ProjectDocumentViewSet, basename="project-document")
router.register("expense-attachments", ExpenseAttachmentViewSet, basename="expense-attachment")
# Frontend follow-up (`docs/tasks/M7-project-grants.md`): read-only reference
# data for the expense form's category picker (name-to-id resolution) — no
# create/update/delete, see `apps.projects.api.ExpenseCategoryViewSet`.
router.register("expense-categories", ExpenseCategoryViewSet, basename="expense-category")
router.register("tags", TagViewSet, basename="tag")
router.register("assets", AssetViewSet, basename="asset")
router.register("stock", StockItemViewSet, basename="stock-item")
router.register("reorder-requests", ReorderRequestViewSet, basename="reorder-request")
router.register("reservations", ReservationViewSet, basename="reservation")
router.register("notification-prefs", NotificationPrefViewSet, basename="notification-pref")
router.register("memberships", MembershipViewSet, basename="membership")
router.register("roles", RoleViewSet, basename="role")
# Gap-fill: "create/discover a user" (see apps.accounts.services module
# docstring) -- `POST /api/v1/users` admin-only create, `GET /api/v1/users`
# any `user.manage` scope for discovery ahead of `POST /api/v1/memberships`.
router.register("users", UserViewSet, basename="user")
# T5.3: read-only, no create/update/destroy route exists (see
# apps.audit.api module docstring) -- registered on the same shared router
# regardless since it's a plain additive registration, not a parallel-task
# merge-conflict risk like `checkout_router` was for T3.2/T3.3.
router.register("audit", AuditLogViewSet, basename="audit-log")

# T3.3: registered on its OWN router (not the shared `router` above) so this
# edit stays additive and doesn't touch the same lines the parallel T3.2 task
# (Reservation create/approve/reject/cancel endpoints) is registering on —
# reduces merge-conflict risk between the two tasks (task instructions).
checkout_router = DefaultRouter()
checkout_router.register("checkouts", CheckoutViewSet, basename="checkout")


def healthz(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("healthz", healthz, name="healthz"),
    # Mounted at `django-admin/` (not `admin/`) so the SPA owns the `/admin/*`
    # namespace for product-admin screens (categories, locations); see
    # docker/nginx/default.conf. Kept mounted for a future admin strategy even
    # though no ModelAdmins are registered and the app DB role (`cortex_app`)
    # is a non-superuser subject to RLS, so Django admin login is currently
    # non-functional at runtime.
    path("django-admin/", admin.site.urls),
    # T0.6 auth endpoints (docs/api-and-ui.md "Auth & identity").
    path("api/v1/auth/csrf", CsrfView.as_view(), name="auth-csrf"),
    path("api/v1/auth/login", LoginView.as_view(), name="auth-login"),
    path("api/v1/auth/logout", LogoutView.as_view(), name="auth-logout"),
    # Forgot-password (unauthenticated): request a reset link, then confirm it.
    path(
        "api/v1/auth/password-reset/request",
        PasswordResetRequestView.as_view(),
        name="auth-password-reset-request",
    ),
    path(
        "api/v1/auth/password-reset/confirm",
        PasswordResetConfirmView.as_view(),
        name="auth-password-reset-confirm",
    ),
    path("api/v1/me", MeView.as_view(), name="me"),
    # Self-service password change (authenticated).
    path("api/v1/me/password", ChangePasswordView.as_view(), name="me-password"),
    # T5.5: a single non-CRUD aggregate endpoint, a plain `path()` rather
    # than a router registration (see `apps.dashboard.api` module docstring).
    path("api/v1/dashboard/summary", DashboardSummaryView.as_view(), name="dashboard-summary"),
    # T4.1: scan/label resolver, a plain `path()` (not router-registered —
    # it's a single non-CRUD lookup keyed by `qr_token`, not `assets/{id}`,
    # same reasoning as `dashboard/summary` above).
    path("api/v1/resolve/<str:qr_token>", AssetResolveView.as_view(), name="asset-resolve"),
    # T4.5: label PDF generation + the generic job-polling endpoint it's the
    # first consumer of. Plain `path()`s (not router-registered) — neither is
    # a CRUD resource collection: `labels/generate` is a single write action,
    # `jobs/{id}` is a single-id read keyed by a UUID that isn't a `Job`
    # ModelViewSet's `list`/`create` route, same reasoning as `resolve/` and
    # `dashboard/summary` above.
    # Per-tenant email delivery config (provider/sender/Brevo API key,
    # `apps.notifications.models.EmailSettings`): a singleton resource, plain
    # `path()` (not router-registered — same reasoning as `dashboard/summary`
    # / `resolve/` above: not a `list`/`create`-shaped CRUD collection).
    path(
        "api/v1/notifications/email-settings",
        EmailSettingsView.as_view(),
        name="email-settings",
    ),
    path(
        "api/v1/notifications/email-settings/test",
        EmailSettingsTestView.as_view(),
        name="email-settings-test",
    ),
    path("api/v1/labels/generate", LabelGenerateView.as_view(), name="label-generate"),
    path("api/v1/jobs/<uuid:job_id>", JobRetrieveView.as_view(), name="job-detail"),
    # T6.1: bulk importer (dry-run upload + commit, plain `path()`s — not
    # router-registered, same reasoning as `labels/generate`/`jobs/{id}`
    # above: none of these three is a `list`/`create`-shaped CRUD collection)
    # + the filtered CSV export.
    path("api/v1/imports", ImportUploadView.as_view(), name="import-upload"),
    path("api/v1/imports/<int:import_id>", ImportDetailView.as_view(), name="import-detail"),
    path(
        "api/v1/imports/<int:import_id>/commit",
        ImportCommitView.as_view(),
        name="import-commit",
    ),
    path("api/v1/exports/assets.csv", AssetExportView.as_view(), name="export-assets-csv"),
    path("api/v1/", include(router.urls)),
    path("api/v1/", include(checkout_router.urls)),
]
