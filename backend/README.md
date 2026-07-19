# backend/

Django 5 + DRF project (scaffolded in T0.3+). Expected layout once filled in:

```
backend/
  manage.py
  config/            # settings/, celery.py, urls.py, wsgi.py, asgi.py
    settings/
      base.py
      dev.py
      prod.py
  apps/
    tenancy/          # Tenant model, tenant-scoped base manager (T0.4)
    accounts/         # User, auth endpoints (T0.4, T0.6)
    rbac/             # Role, Permission, RolePermission, Membership (T0.4)
    assets/           # Milestone 1+
    ...
  requirements/
    base.txt
    dev.txt
  pytest.ini / conftest.py
```

Stack: Django 5, DRF, PostgreSQL 16, Redis (cache/sessions/broker), Celery + beat,
Argon2 password hashing, django-environ for 12-factor settings. See
`../docs/architecture.md` and `../docs/deployment.md`.
