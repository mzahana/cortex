"""Prod settings (T0.3): SECURE_* headers, HSTS, secure cookies — all from env
so nothing here assumes a specific host (docs/deployment.md §6, §7).

NOTE: `/healthz` is exempt from ALLOWED_HOSTS / SSL-redirect handling by
`config.middleware.HealthCheckMiddleware`, which runs before SecurityMiddleware
— see that module's docstring. Do not "fix" a healthcheck failure here by
adding localhost/127.0.0.1 to ALLOWED_HOSTS; that would weaken the Host
allow-list for real traffic instead of fixing the actual problem.
"""

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS are required in prod — fail fast rather
# than silently serving with an empty allow-list.
if not ALLOWED_HOSTS:  # noqa: F405
    raise RuntimeError("ALLOWED_HOSTS must be set via env in production.")

# --- Transport security --------------------------------------------------------
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=True)

# Real email (Brevo) — see EmailProvider interface (wired in a later milestone);
# console backend is only ever used in dev/tests.
EMAIL_BACKEND = env.str("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
