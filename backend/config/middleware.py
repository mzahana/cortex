"""Process-wide middleware that isn't tied to a specific app.

Kept in `config/` (not `apps/`) because it is infrastructure, not domain logic.
"""

from django.http import JsonResponse

HEALTHZ_PATH = "/healthz"


class HealthCheckMiddleware:
    """Serve `/healthz` before any other middleware runs.

    Resolves the carried-forward T0.2 review finding: in prod, `DEBUG=False`
    with `ALLOWED_HOSTS` pinned to the real domain makes Django raise
    `DisallowedHost` (-> 400) for the Docker healthcheck, which hits the
    container over `localhost`/`127.0.0.1` — a Host header that is correctly
    *not* in `ALLOWED_HOSTS` for internet traffic. Similarly, `SecurityMiddleware`
    would 301-redirect a plain-HTTP healthcheck request when
    `SECURE_SSL_REDIRECT=True` in prod.

    Both failure modes are triggered by code that runs *after* this point in
    the middleware chain (`request.get_host()` is only evaluated when
    something calls it — `CommonMiddleware`/`SecurityMiddleware`/CSRF do so).
    By registering this middleware **first** in `MIDDLEWARE` and returning a
    response immediately for the health path, no downstream middleware ever
    touches `request.get_host()` or the HTTPS redirect for that path, so the
    healthcheck works regardless of `ALLOWED_HOSTS`/`SECURE_SSL_REDIRECT`
    configuration. This is intentionally NOT solved by adding
    localhost/127.0.0.1 to `ALLOWED_HOSTS` in prod, which would only mask the
    symptom and weaken the Host-header allow-list for real traffic.

    The endpoint is deliberately unauthenticated (liveness probe only) and
    does not touch the database/cache, so it can't itself become the
    bottleneck or leak tenant data.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == HEALTHZ_PATH:
            return JsonResponse({"status": "ok"})
        return self.get_response(request)
