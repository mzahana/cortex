"""Base settings shared by every environment (T0.3).

12-factor: every value that varies between environments comes from `env.*`
below. `dev.py`/`prod.py` only override the handful of things that legitimately
differ (DEBUG, cookie security, HSTS, etc.) — see docs/deployment.md §6 and
docs/architecture.md §7.
"""

from pathlib import Path

import environ

# backend/config/settings/base.py -> backend/
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

# Local convenience only: when `manage.py` is run directly on a host (outside
# Docker), read the repo-root `.env` if present. Inside containers, compose's
# `env_file:` already injects these into the process environment, so this is a
# no-op there (os.environ wins; read_env does not override existing vars).
_repo_root_env = BASE_DIR.parent / ".env"
if _repo_root_env.exists():
    environ.Env.read_env(str(_repo_root_env))

# --- Core --------------------------------------------------------------------
SECRET_KEY = env.str("SECRET_KEY")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # T1.4: registers the postgres-specific lookups (`__trigram_similar`,
    # used by `apps.assets.api.AssetSearchFilter` for the `pg_trgm` fuzzy
    # match) via `PostgresConfig.ready()`. `SearchVectorField`/`SearchQuery`/
    # `SearchRank` (also used there) work without this app registered, but
    # the trigram LOOKUP class only self-registers onto `CharField`/
    # `TextField` when this app's `ready()` runs — without it, `django.
    # contrib.postgres.search` imports fine but `name__trigram_similar=...`
    # raises `FieldError: Unsupported lookup 'trigram_similar'` at query time.
    "django.contrib.postgres",
    "rest_framework",
    "django_filters",
    # Tenant/RBAC/asset apps (T0.4). Order matters only for migration
    # dependency resolution, which Django handles via explicit
    # `dependencies` in each app's migrations, not app order.
    "apps.tenancy",
    "apps.accounts",
    "apps.projects",
    "apps.rbac",
    "apps.catalog",
    "apps.audit",
    "apps.assets",
    "apps.stock",
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    # Must stay first — see config/middleware.py docstring for why (resolves
    # the T0.2 healthcheck-vs-ALLOWED_HOSTS review finding).
    "config.middleware.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    # Must come AFTER SessionMiddleware, BEFORE AuthenticationMiddleware: a
    # T0.6 finding (RLS otherwise blocks AuthenticationMiddleware's own user
    # lookup on every request past login) — see
    # apps/tenancy/middleware.py::SessionTenantPreloadMiddleware docstring.
    "apps.tenancy.middleware.SessionTenantPreloadMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Must come AFTER AuthenticationMiddleware: derives the current tenant
    # from `request.user` (session-authenticated), never from client input
    # (R4 — see apps/tenancy/middleware.py docstring).
    "apps.tenancy.middleware.CurrentTenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --- Database (Postgres 16) ---------------------------------------------------
DATABASES = {
    "default": env.db("DATABASE_URL"),
}
# Persistent connections instead of PgBouncer at tier-1 (docs/architecture.md §4).
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DATABASES["default"]["ATOMIC_REQUESTS"] = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Redis: cache + sessions (broker/result config is on the Celery app) -----
# REDIS_URL (db 0) per docs/architecture.md "Redis does triple duty"; Celery
# uses its own DB indexes (1, 2) — see CELERY_* below and config/celery.py.
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": env.str("REDIS_URL", default="redis://redis:6379/0"),
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}

# Sessions live in Redis, not in-process and not sticky (stateless `web`
# per docs/architecture.md §4).
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"
SESSION_COOKIE_NAME = "cortex_sessionid"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=60 * 60 * 24 * 7)  # 7 days

# CSRF cookie must be JS-readable so the SPA can echo it back as a header on
# writes (docs/api-and-ui.md); it is not the auth token, session cookie is.
CSRF_COOKIE_NAME = "cortex_csrftoken"
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_HEADER_NAME = "HTTP_X_CSRFTOKEN"
# Keep CSRF rejections in the same RFC-7807 envelope as every other API error
# (T0.6) instead of Django's default HTML 403 page — see
# `apps.common.errors.csrf_failure_view`.
CSRF_FAILURE_VIEW = "apps.common.errors.csrf_failure_view"

# --- Celery (broker/result on Redis; see config/celery.py for the app) -------
CELERY_BROKER_URL = env.str("CELERY_BROKER_URL", default="redis://redis:6379/1")
CELERY_RESULT_BACKEND = env.str("CELERY_RESULT_BACKEND", default="redis://redis:6379/2")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = env.int("CELERY_TASK_TIME_LIMIT", default=300)
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200
CELERY_TIMEZONE = "UTC"

# --- Auth / passwords ----------------------------------------------------------
AUTH_PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- DRF -----------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    # RFC-7807 problem+json everywhere (docs/api-and-ui.md §1, T0.6).
    "EXCEPTION_HANDLER": "apps.common.errors.rfc7807_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/min",
        "user": "1000/min",
        # Tighter, dedicated rate for the login endpoint (T0.6) to back off
        # brute-force per docs/architecture.md §7.
        "login": "10/min",
    },
}

# --- I18N ------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static / media ------------------------------------------------------------
STATIC_URL = env.str("STATIC_URL", default="/static/")
STATIC_ROOT = env.str("STATIC_ROOT", default="/app/staticfiles")
MEDIA_URL = env.str("MEDIA_URL", default="/media/")
MEDIA_ROOT = env.str("MEDIA_ROOT", default="/app/media")

# `django-storages` (docs/architecture.md §2/§4, CLAUDE.md stack) backs
# Attachment uploads (T1.2): binary bytes are written to the mounted `media`
# volume by the storage backend, never persisted in the DB — only the
# resulting storage key/path is stored (`apps.assets.models.Attachment.
# storage_key`). Default backend is the plain filesystem (the NAS volume);
# swapping to S3-compatible object storage at a later deployment tier is a
# pure env-var change (`DJANGO_DEFAULT_FILE_STORAGE=storages.backends.s3.
# S3Storage` + the matching `AWS_*`/`STORAGES["default"]["OPTIONS"]` env vars),
# no code change, per docs/architecture.md §5's "config, not rewrite" path.
STORAGES = {
    "default": {
        "BACKEND": env.str(
            "DJANGO_DEFAULT_FILE_STORAGE",
            default="django.core.files.storage.FileSystemStorage",
        ),
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# Attachment upload hardening (code-review finding, T1.2 follow-up): a size
# cap enforced in `apps.assets.services.validate_attachment_upload` BEFORE
# any bytes are written to the volume. The content-type/extension allowlist
# lives alongside it (not env-configurable — a fixed, reviewed list, not
# something an operator should be able to loosen via `.env`). Note this is
# one of two defense-in-depth layers for inline-rendering/stored-XSS: the
# other is nginx serving `/media/` with `Content-Disposition: attachment`
# (queued for devops-engineer) — this allowlist does not by itself guarantee
# a browser never renders an accepted file inline.
MAX_ATTACHMENT_UPLOAD_BYTES = env.int("MAX_ATTACHMENT_UPLOAD_BYTES", default=25 * 1024 * 1024)

# --- Email (Brevo goes behind EmailProvider; wired in a later milestone) -----
DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default="Cortex <cortex@example.com>")
EMAIL_BACKEND = env.str("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")

# --- Logging -------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": env.str("DJANGO_LOG_LEVEL", default="INFO")},
}

# --- Reverse proxy (nginx -> gunicorn behind cloudflared) ---------------------
# Trust X-Forwarded-Proto from our own nginx so request.is_secure() (used by
# SECURE_SSL_REDIRECT/HSTS in prod) reflects the edge-terminated TLS, not the
# plain-HTTP hop between nginx and gunicorn.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

# --- Auth model checks (T0.4) --------------------------------------------------
# `auth.E003` requires `USERNAME_FIELD` (email) to be globally unique. Per
# docs/data-model.md, email is unique **per tenant**
# (`UniqueConstraint(tenant, email)` on apps.accounts.User), by design — a
# lab member could plausibly belong to more than one tenant with the same
# email. Login (T0.6) resolves `(tenant, email)` explicitly rather than
# relying on Django's default single-field lookup, so this check does not
# apply here.
SILENCED_SYSTEM_CHECKS = ["auth.E003"]
