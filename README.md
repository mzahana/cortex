# Cortex — Lab Asset & Inventory Management

Cortex helps a lab keep track of all its physical things — computers and GPUs,
edge devices, drones, components, tools, and instruments. For each item it
answers: **what do we own, where is it, who has it, when is it due back, and
when are we running low?**

It works great on a phone: you can install it like an app, scan a QR code with
the camera to pull up an item, and print QR labels to stick on your equipment.
Each lab or team gets its own private, separate space.

**Status:** ready to use (the full first version is complete). See
[`CHANGELOG.md`](CHANGELOG.md) for what's included.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

---

## Screenshots

<p>
  <img src="docs/images/screenshot-dashboard.png" alt="Cortex dashboard" width="70%">
</p>
<p>
  <img src="docs/images/screenshot-login.png" alt="Cortex login screen" width="30%">
</p>

---

## What you can do with it

| Area | What it gives you |
|---|---|
| **Asset registry** | A searchable list of everything you own, with custom fields per category, tags, and projects |
| **Consumables** | Track stock levels, get low-stock alerts, and reorder |
| **Reservations & checkout** | Book items on a calendar, approve requests, check items in/out, and see what's overdue |
| **Mobile scan & labels** | Install as a phone app, scan a QR code to open an item, take photos, and print QR labels |
| **Notifications** | Email reminders for reservations, approvals, overdue items, and low stock |
| **Projects & grants** | Per-project budgets vs. spending, expense/invoice tracking, and PDF reports |
| **Audit & dashboard** | A tamper-proof history of changes and a live dashboard |
| **Import/export** | Bulk-import from a spreadsheet (with a safe preview first) and export to CSV |

Want the full detail? See [`docs/features.md`](docs/features.md).

---

## Before you start

You need one thing installed: **[Docker](https://docs.docker.com/get-docker/)**
(with Docker Compose, which comes with it). Docker is a free tool that runs
Cortex and everything it needs — you don't have to install databases or other
software yourself.

You'll also need **git** to download the code, or you can download it as a ZIP
from the project page.

That's it. You don't need to be a programmer to get Cortex running.

---

## Run it on your own computer (to try it out)

This gets Cortex running on your own machine so you can explore it. Do the
steps in order.

**1. Download the code and create your settings file.**

```bash
git clone <this-repo-url> cortex
cd cortex
cp .env.example .env
```

**2. Fill in a few settings.** Open the new `.env` file in any text editor and
replace the placeholder values that start with `changeme-`. At minimum, set:

- **`SECRET_KEY`** — a long random string. You can create one by running:
  `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`
- **The database passwords** (`POSTGRES_PASSWORD` / `DATABASE_URL` and
  `APP_DB_PASSWORD` / `APP_DATABASE_URL`) — pick your own passwords. Each
  password appears in two places; make sure they match.

You can leave everything else as-is for a first try. The email and internet-
tunnel settings can stay as placeholders — those are only needed when you
deploy it for a real team.

**3. Start Cortex.**

```bash
docker compose build
docker compose up -d
```

The first `build` takes a few minutes. After it finishes, everything starts up
automatically — including the database and the first-time setup.

**4. Open it in your browser.** By default Cortex is set up to sit behind a
secure internet tunnel, so it doesn't open a browser port on its own. To view
it locally, create a file named `docker-compose.override.yml` in the project
folder with this content:

```yaml
services:
  nginx:
    ports:
      - "8080:80"
```

Then run `docker compose up -d` again and open **http://localhost:8080** in
your browser.

**5. Create your first login.** There's no "sign up" button — the first tenant
(your lab) and its admin account are created with a short setup command:

```bash
docker compose exec web python manage.py shell
```

Then paste this block, replacing the slug, name, email, and password with
your own:

```python
from apps.tenancy.models import Tenant
from apps.accounts.models import User
from apps.rbac.models import Membership, Role
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.tenancy.context import tenant_context

tenant = Tenant.objects.create(slug="your-lab-slug", name="Your Lab Name")

with tenant_context(tenant.id):
    admin_user = User.all_objects.create_superuser(
        tenant=tenant,
        email="admin@your-real-domain.com",
        password="<a real, strong password — change immediately after first login>",
    )
    membership = Membership.all_objects.get(user=admin_user, project__isnull=True)
    admin_role = Role.all_objects.get(tenant=tenant, key=ROLE_ADMIN)
    membership.role = admin_role
    membership.save(update_fields=["role"])

print(f"Created tenant {tenant.slug!r} with admin {admin_user.email!r}")
```

Exit the shell (`exit()`), then log in with your **lab slug, email, and
password**. (Full details, including a demo-only seed command for local
testing, are in the [deployment runbook, §3c](docs/deployment-runbook.md#3c-first-time-bring-up).)

---

## Stopping and starting

```bash
docker compose stop    # pause Cortex (your data is kept)
docker compose up -d   # start it again
```

Your data is always safe when you stop — nothing is deleted.

---

## Deploy it for your team (over the internet)

To run Cortex for real — on a small home server or NAS (like a Synology), so
your whole team can reach it securely over the internet — follow the
step-by-step guide:

**➡️ [Deployment runbook](docs/deployment-runbook.md)** — the exact,
copy-paste steps: setting up the secure Cloudflare internet tunnel, email,
folders on the NAS, starting everything, creating your admin, and backups.

For the bigger picture of how it all fits together (and how much memory it
needs), read [`docs/deployment.md`](docs/deployment.md) first.

**Your server never builds anything.** Cortex's two images are built by CI and
published publicly to Docker Hub — [`mzahana/cortex`](https://hub.docker.com/r/mzahana/cortex)
(the app: web, worker and scheduler) and
[`mzahana/cortex-nginx`](https://hub.docker.com/r/mzahana/cortex-nginx) (the web
server plus the built app screens). So a real deploy only needs the two compose
files, your `.env`, and the nginx/redis config files — no source code, and no
slow build on a small NAS:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

That gets you `latest`, which always points at the newest **released** version
(never an untested in-progress commit). To pin a specific version instead, set
`CORTEX_IMAGE_TAG=0.12.0`. Use `docker compose ... up -d` — not `restart` — when
upgrading, or Docker keeps running the old image.

You don't have to open any ports on your router — Cortex reaches the internet
through a secure outbound tunnel, and your connection is encrypted (HTTPS)
automatically.

---

## Getting more out of it / more help

- **Full feature list:** [`docs/features.md`](docs/features.md)
- **Deploy for a team:** [`docs/deployment-runbook.md`](docs/deployment-runbook.md)
- **What changed in each version:** [`CHANGELOG.md`](CHANGELOG.md)
- **Roles & permissions (who can do what):** [`docs/rbac.md`](docs/rbac.md)

## For developers

Building on or contributing to Cortex? Everything technical — local
development, running tests, rebuilding after code changes, the repository
layout, and the design docs — lives in the
**[developer guide](docs/development.md)**. The project's ground rules for code
changes are in [`CLAUDE.md`](CLAUDE.md).

## License

Copyright © 2026 Mohamed Abdelkader.

Cortex is open-source software licensed under the
[Apache License, Version 2.0](LICENSE).
