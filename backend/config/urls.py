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

from apps.accounts.api import CsrfView, LoginView, LogoutView, MeView
from apps.assets.api import AssetViewSet
from apps.catalog.api import CategoryViewSet, LocationViewSet, ProjectViewSet, TagViewSet
from apps.reservations.api import ReservationViewSet
from apps.reservations.checkout import CheckoutViewSet
from apps.stock.api import ReorderRequestViewSet, StockItemViewSet

router = DefaultRouter()
router.register("categories", CategoryViewSet, basename="category")
router.register("locations", LocationViewSet, basename="location")
router.register("projects", ProjectViewSet, basename="project")
router.register("tags", TagViewSet, basename="tag")
router.register("assets", AssetViewSet, basename="asset")
router.register("stock", StockItemViewSet, basename="stock-item")
router.register("reorder-requests", ReorderRequestViewSet, basename="reorder-request")
router.register("reservations", ReservationViewSet, basename="reservation")

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
    path("api/v1/me", MeView.as_view(), name="me"),
    path("api/v1/", include(router.urls)),
    path("api/v1/", include(checkout_router.urls)),
]
