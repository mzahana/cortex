"""WSGI entrypoint. `DJANGO_SETTINGS_MODULE` is set explicitly in the
container environment (config.settings.dev/prod); the default below only
matters for ad-hoc host runs."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

application = get_wsgi_application()
