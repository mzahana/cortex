"""Dev settings (T0.3). `DEBUG` opts IN here only — base.py defaults it off."""

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = env.bool("DEBUG", default=True)

# Convenient for local docker-compose/manual runs; still explicit opt-in via
# ALLOWED_HOSTS if the caller wants to pin it (e.g. CI).
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])

# Local HTTP (no TLS) — relax cookie/SSL flags that prod turns on.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False

# Print emails to the console/worker logs instead of calling Brevo.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
