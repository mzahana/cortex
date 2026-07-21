# ------------------------------------------------------------------------------
# Shared image for the `web`, `worker`, and `beat` services (T0.2). One build,
# one image, three commands (see docker-compose.yml). Base image is pinned by
# digest per docs/deployment.md §7 hardening checklist.
#
# The backend/ source copied in below is currently the PLACEHOLDER scaffold
# from T0.2 (minimal Django project + trivial Celery task) so this image
# builds and the containers boot/health-check cleanly before the real Django
# app lands in T0.3. T0.3 replaces backend/ contents; this Dockerfile's build
# steps (deps from requirements/, copy backend/, non-root user, entrypoint)
# should NOT need to change.
# ------------------------------------------------------------------------------
FROM python:3.12-alpine3.20@sha256:25849f9599e06dfe4d11b552e06f5ac4cc2ad342054eb81f7877e611f6f87c66

# libpq: runtime lib for psycopg; tini: proper PID 1 signal handling for
# gunicorn/celery; bash: convenience for exec/debugging only.
# pango/cairo/gdk-pixbuf/fontconfig/shared-mime-info/ttf-freefont: T4.5's
# WeasyPrint (label PDF rendering, `apps.labels.rendering`) links against
# these at import time -- without them `import weasyprint` raises
# `OSError: cannot load library 'libpango-1.0.so.0'` (or similar) the first
# time the Celery worker picks up a `label_pdf` job. Needed on BOTH `web`
# (Django import-time app-loading pulls in `apps.labels`) and `worker`
# (where the task actually renders) since they share this one image.
# zbar + imagemagick: provide the `zbarimg` CLI the T4.5 label-PDF
# round-trip test shells out to (see `requirements/dev.txt`) to decode a
# rendered QR back to its token -- not used by the app itself at runtime,
# but tiny, and this image is also what `pip install -r requirements/
# dev.txt` + pytest run inside. `imagemagick` is required too: Alpine's
# `zbar` package links `zbarimg` against MagickWand for image loading, and
# without ImageMagick's own PNG delegate present `zbarimg` fails every
# input with "NoDecodeDelegateForThisImageFormat 'PNG'" even though the
# `zbar` package installs fine on its own.
RUN apk add --no-cache libpq tini pango cairo gdk-pixbuf fontconfig shared-mime-info ttf-freefont zbar imagemagick \
    && addgroup -S app && adduser -S app -G app

WORKDIR /app

COPY backend/requirements/ /app/requirements/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements/base.txt

COPY backend/ /app/

RUN mkdir -p /app/media /app/staticfiles \
    && chown -R app:app /app

USER app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=config.settings.dev

EXPOSE 8000

ENTRYPOINT ["/sbin/tini", "--"]
# Overridden per-service by `command:` in docker-compose.yml.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
