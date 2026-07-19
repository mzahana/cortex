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

router = DefaultRouter()
# apps/* register viewsets here, e.g.:
#   router.register("assets", AssetViewSet, basename="asset")


def healthz(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    # T0.6 auth endpoints (docs/api-and-ui.md "Auth & identity").
    path("api/v1/auth/csrf", CsrfView.as_view(), name="auth-csrf"),
    path("api/v1/auth/login", LoginView.as_view(), name="auth-login"),
    path("api/v1/auth/logout", LogoutView.as_view(), name="auth-logout"),
    path("api/v1/me", MeView.as_view(), name="me"),
    path("api/v1/", include(router.urls)),
]
