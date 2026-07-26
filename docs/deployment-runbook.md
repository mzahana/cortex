# Cortex — Deployment Runbook (Synology DS220+ + Cloudflare Tunnel)

**Audience:** the human operator running the real deploy. This is the
step-by-step companion to the design-level `docs/deployment.md` (topology,
rationale, hardening checklist, memory budget) — read that first if you
haven't. This document assumes nothing beyond a fresh Synology DS220+ with
DSM and a domain you control in Cloudflare.

**Placeholders used throughout — find-and-replace before you start:**
- `cortex.example.com` — your real public hostname (the tunnel's public
  hostname route; matches `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS` in `.env`).
- `example.com` — your real sender/mail domain in Brevo (may or may not be
  the same domain as above — see `docs/risks.md` §3 Q3/Q6, both still open
  at the time this runbook was written; fill in your actual domain(s)).
- `<TUNNEL_TOKEN>`, `<SECRET_KEY>`, `<DB password>`, etc. — generate/copy
  real secrets per the instructions inline; never commit them.

This task (T6.4) does not cover backups/restore — see **§5 (T6.5)** for the
backup script, DSM Task Scheduler wiring, and the executed restore drill —
or the load test (T6.6, a separate artifact under `tests/load/*`).

---

## 0. Prerequisites checklist

- [ ] DS220+ with DSM, Container Manager package installed (`docs/deployment.md` §2).
- [ ] RAM confirmed (2 GB stock vs 6 GB upgraded — see `docs/deployment.md` §1/§8; this
      runbook assumes 6 GB, the recommended target).
- [ ] A domain you control, added to a Cloudflare account (free plan is enough).
- [ ] A Brevo account with API key, for transactional email.
- [ ] SSH access to the NAS (recommended for `docker compose exec` one-offs;
      Container Manager's own UI can also open a container terminal).

---

## 1. Cloudflare Zero Trust — create the Tunnel

Cortex uses the **token-based tunnel** pattern: no local `config.yml`, no
credentials-JSON file on the NAS. Everything routing-related is configured
once in the dashboard. See `docker/cloudflared/README.md` for why this
was chosen over a local `config.yml`.

1. Go to **Cloudflare dashboard → Zero Trust → Networks → Tunnels →
   Create a tunnel**.
2. Choose **Cloudflared** as the connector type. Name it e.g. `cortex-nas`.
3. On the **Install and run connector** step, select **Docker** — Cloudflare
   shows a `docker run ... cloudflare/cloudflared:latest tunnel run --token
   eyJ...` command. **You do not need to run that command as shown** — copy
   only the token value (the long string after `--token`) into `.env` as
   `TUNNEL_TOKEN` (see §4 below); the repo's `docker-compose.yml` already
   defines the `cloudflared` service (`command: ["tunnel", "run"]`,
   `TUNNEL_TOKEN: ${TUNNEL_TOKEN}`) — that's the full equivalent of the
   dashboard's docker-run command, already wired for you.
4. Click through past the "install connector" step without running the raw
   `docker run` — you'll bring the whole stack up together in §5. The tunnel
   will show **Status: Inactive** until the `cloudflared` container in the
   Cortex stack actually connects; that's expected at this point.
5. Go to the tunnel's **Public Hostname** tab → **Add a public hostname**:
   - **Subdomain:** `cortex` (or whatever you chose)
   - **Domain:** `example.com` (your real domain, picked from the dropdown
     of domains already onboarded to your Cloudflare account)
   - **Path:** leave blank (routes the whole hostname)
   - **Service → Type:** `HTTP`
   - **Service → URL:** `nginx:80`
     (this resolves via the compose network's internal DNS — `nginx` is the
     service name in `docker-compose.yml` — **not** a public address; the
     tunnel and the app share the same Docker network via the `cloudflared`
     container).
   - Save.
6. Cloudflare auto-creates the DNS `CNAME` record for `cortex.example.com`
   pointing at the tunnel — no manual DNS step needed for the hostname
   itself.

### 1a. TLS / edge hardening (dashboard settings)

All under the zone for `example.com` (**not** the Zero Trust section —
switch back to the regular dashboard for your zone):

1. **SSL/TLS → Overview** → set encryption mode to **Full (strict)**.
   (Cloudflare edge already terminates TLS for the public hostname; "strict"
   additionally validates the certificate cloudflared presents from the
   tunnel side — cloudflared handles this automatically, no cert to manage
   on the NAS.)
2. **SSL/TLS → Edge Certificates**:
   - Enable **Always Use HTTPS**.
   - Enable **HTTP Strict Transport Security (HSTS)**: min TLS 1.2,
     Max-Age **6 months** (matches the app's own `Strict-Transport-Security:
     max-age=63072000` set by both Django (`config/settings/prod.py`,
     `SECURE_HSTS_SECONDS`) and nginx (`docker/nginx/default.conf`) — leave
     "Include subdomains" and "Preload" on to match, but do **not** submit
     to the HSTS preload list until you're confident the deploy is stable
     long-term, since preload is very hard to reverse).
3. **Security → Bots** → enable **Bot Fight Mode** (free-plan tier; blocks
   known bad bots/scrapers at the edge before they reach the tunnel).
4. **Security → WAF → Rate limiting rules → Create rule** — the login
   throttle called out in `docs/deployment.md` §7 (this is **in addition
   to**, not instead of, the app's own DRF `login` scope throttle of
   10/min per `backend/config/settings/base.py`'s
   `DEFAULT_THROTTLE_RATES` — defense-in-depth at the edge before a
   request even reaches nginx/gunicorn):
   - **Rule name:** `login-rate-limit`
   - **If incoming requests match:** `URI Path` `equals`
     `/api/v1/auth/login` **and** `Hostname` `equals` `cortex.example.com`
   - **Rate:** `5` requests per `1 minute`, counted **per IP address** (the
     field is usually labelled "Characteristics" — choose "IP address" as
     the counting key)
   - **Then:** **Block** for `10 minutes` (Cloudflare's "Block" action;
     alternatively "Managed Challenge" if you want to allow retry via
     CAPTCHA instead of a hard block — Block is simpler and matches the
     app's own lockout-adjacent posture)
   - Save and deploy.
5. Optional (skip for MVP unless you want it now): **Zero Trust → Access →
   Applications** — see `docs/deployment.md` §4 for the edge-auth toggle;
   MVP intentionally ships with app-login-only.

---

## 2. Brevo sender domain — SPF / DKIM / DMARC (F9 deliverability)

Needed so transactional email (invites, due-date reminders, etc., via the
`EmailProvider`/Brevo integration) reliably lands in the inbox instead of
spam. Do this in **two places**: Brevo's dashboard (which tells you the
exact values) and your DNS zone (Cloudflare, since your domain is already
there) — DNS record **types/names below are templated**; the **values** you
must copy verbatim from your own Brevo account (they are per-account and
Brevo will reject/not-verify made-up values).

1. **Brevo dashboard → Senders, Domains & Dedicated IPs → Domains → Add a
   domain** → enter `example.com` (or a subdomain you're dedicating to
   mail, e.g. `mail.example.com` — either works; keep it consistent with
   `DEFAULT_FROM_EMAIL`/`BREVO_REPLY_TO` in `.env`, e.g.
   `Cortex <cortex@example.com>`).
2. Brevo shows **three record blocks** to add. Templated shape (replace
   `<...>` with what Brevo's UI actually shows for your account — these
   values differ per account/domain and are not guessable):

   **SPF (TXT record)** — often Brevo asks you to *append* to an existing
   SPF record if one already exists (a domain can only have one SPF TXT
   record):
   ```
   Type:  TXT
   Name:  @  (or "example.com", i.e. the domain root)
   Value: v=spf1 include:spf.brevo.com <other-existing-includes-if-any> ~all
   ```
   If you already send mail from this domain via another provider, merge
   the `include:` mechanisms into **one** SPF record rather than adding a
   second — multiple SPF TXT records on the same name is invalid per RFC
   7208 and will break validation.

   **DKIM (TXT record, Brevo-generated per account)**:
   ```
   Type:  TXT
   Name:  <brevo-provided-selector>._domainkey  (e.g. "mail._domainkey" — copy exactly from Brevo's UI)
   Value: <brevo-provided DKIM public key string, starts with "k=rsa; p=...">
   ```

   **DMARC (TXT record)** — Brevo's onboarding may not prompt for this one
   (it's a general best practice, not Brevo-specific), so add it yourself:
   ```
   Type:  TXT
   Name:  _dmarc
   Value: v=DMARC1; p=none; rua=mailto:dmarc-reports@example.com; fo=1
   ```
   **Policy recommendation:** start at `p=none` (monitor-only — collects
   aggregate reports at the `rua` address without affecting delivery) for
   at least 1-2 weeks while confirming SPF/DKIM pass consistently for real
   traffic, **then** tighten to `p=quarantine` and eventually `p=reject`
   once you've confirmed no legitimate mail (from Brevo or elsewhere) is
   failing alignment — jumping straight to `p=reject` risks silently
   dropping your own legitimate mail if SPF/DKIM aren't both correctly
   aligned yet.
3. In Cloudflare DNS (**DNS → Records** for the zone), add the three
   records above with **Proxy status: DNS only** (grey cloud, not orange —
   these are mail-authentication records, not web traffic; proxying them
   through Cloudflare's HTTP proxy would break DNS-based mail validation).
4. Back in Brevo, click **Verify** / **Authenticate domain** — Brevo checks
   the DNS records it can see and marks the domain verified once SPF+DKIM
   resolve (DNS propagation can take a few minutes to a few hours).
5. Update `.env`: `DEFAULT_FROM_EMAIL` and `BREVO_REPLY_TO` to use the
   now-verified domain (see `.env.example`'s placeholders for the exact
   variable names).

---

## 3. Synology Container Manager — shared folders & first bring-up

### 3a. Shared folder layout

Create one shared folder (**Control Panel → Shared Folder → Create**),
e.g. `docker`, and inside it a `cortex` project folder with this layout —
matches exactly what `docker-compose.yml`'s named volumes and bind mounts
need (cross-checked against the file: `pgdata`, `redis-data`, `media`,
`static` are named Docker volumes managed by the engine, not bind mounts,
so they don't need their own DSM subfolder unless you explicitly rebind
them; `.env`, `docker/nginx/nginx.conf`, `docker/nginx/default.conf` **are**
bind-mounted paths and do need to exist on disk exactly where the compose
file expects them — the PWA itself is no longer a bind mount: `docker/nginx/
Dockerfile`'s multi-stage build runs `npm ci && npm run build` against
`frontend/` and bakes the resulting `dist/` straight into the `cortex-nginx`
image at `docker compose build` time, so there is no separate `frontend/
dist` folder to create or keep in sync on the NAS):

```
/volume1/docker/cortex/                  <- project root; `cd` here to run docker compose
├── .env                                 <- secrets, mounted read-only (env_file: - .env)
├── docker-compose.yml                   <- copied from the repo (or the whole repo checked out)
├── docker-compose.prod.yml
├── docker/
│   ├── Dockerfile                       <- builds the shared web/worker image
│   ├── nginx/
│   │   ├── Dockerfile                   <- builds the frontend (Node stage) + nginx image
│   │   ├── nginx.conf                   <- bind-mounted read-only into nginx
│   │   └── default.conf                 <- bind-mounted read-only into nginx
│   └── redis.conf                       <- bind-mounted read-only into redis
├── frontend/                            <- source only; `docker compose build` runs
│                                            `npm ci && npm run build` inside the nginx
│                                            image, no local/NAS npm install needed
└── backend/                             <- only needed if building the image on the NAS;
                                             if you push cortex-app/cortex-nginx to a
                                             registry instead, you only need the compose
                                             files on the NAS itself
```

The simplest, lowest-maintenance option for a DS220+: **check out the whole
git repo** into `/volume1/docker/cortex` (via `git clone` over SSH, or
`git pull` to update) rather than hand-copying individual files — that way
`docker compose build` always has everything it needs (`docker/Dockerfile`,
`docker/nginx/Dockerfile`, `backend/`, `frontend/`) and stays trivially
updatable, including building the PWA bundle itself. Only `.env` needs to be
created by hand on the NAS (never checked into git).

Named volumes (`pgdata`, `redis-data`, `media`, `static`) are created and
managed by the Docker engine itself under
`/volume1/@docker/volumes/cortex_<name>/_data` — you don't need to
pre-create these directories; `docker compose up` does it. If you want them
on a specific physical volume/spindle for the NAS's own reasons, that's a
Docker Desktop/engine storage-location setting in DSM, not a compose change.

### 3b. `.env` — where it lives and how it's picked up

- Location: `/volume1/docker/cortex/.env` (i.e. the compose project root —
  `docker-compose.yml`'s `env_file: [.env]` on every app service resolves
  relative to wherever you run `docker compose` from, so always run
  commands from this directory).
- Copy `.env.example` → `.env` and fill in every placeholder (never commit
  `.env`; it's already gitignored — verified: `.gitignore` line 2 is
  `.env`). At minimum:
  - `SECRET_KEY` — generate with
    `python -c "import secrets; print(secrets.token_urlsafe(50))"` (any
    Python 3 on your workstation; doesn't need to run on the NAS).
  - `ALLOWED_HOSTS=cortex.example.com`,
    `CSRF_TRUSTED_ORIGINS=https://cortex.example.com` — must match §1's
    public hostname exactly (scheme included for `CSRF_TRUSTED_ORIGINS`).
  - `DATABASE_URL`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` —
    pick a real password; keep `POSTGRES_USER`/`DATABASE_URL` consistent.
  - `APP_DB_USER`, `APP_DB_PASSWORD`, `APP_DATABASE_URL` — a **different**
    password than the owner DB password above (this is the RLS-subject
    runtime role, see `.env.example`'s comment on why this must stay
    separate from the owner role).
  - `BREVO_API_KEY`, `DEFAULT_FROM_EMAIL`, `BREVO_REPLY_TO` — from §2.
  - `TUNNEL_TOKEN` — from §1 step 3.
  - `DJANGO_SETTINGS_MODULE=config.settings.prod` (already the
    `.env.example` default — leave as-is).
- **Container Manager UI path:** Container Manager → **Project → Create** →
  **Path**: `/docker/cortex` → it auto-detects `docker-compose.yml` in that
  folder. Container Manager does **not** need a separate "env file" field
  entered in the UI — because `docker-compose.yml` itself declares
  `env_file: [.env]` per service, Container Manager (which shells out to
  the same `docker compose` engine) picks it up automatically as long as
  `.env` sits next to the compose file, exactly as in §3a's layout. This
  matches `docs/deployment.md` §2's assumed workflow (point Container
  Manager at the compose file; it parses/manages the stack).
- If you prefer the command line instead of the Container Manager GUI (more
  reliable for the one-off `migrate`/seed commands below), SSH into the NAS
  and run `docker compose` directly from `/volume1/docker/cortex` — both
  approaches manage the exact same underlying stack; you can mix them
  (bring up via GUI, exec commands via SSH).

### 3c. First-time bring-up

Run from `/volume1/docker/cortex` (via SSH; Container Manager's own
"Action → Build"/"Action → Start" buttons do the equivalent of steps 1-2
below if you prefer the GUI):

```bash
# 1. Build the shared web/worker image (one image, two commands — see
#    docker-compose.yml's `x-app-image` anchor) and pull the rest.
docker compose -f docker-compose.yml -f docker-compose.prod.yml build

# 2. Bring the whole stack up. `migrate` runs as a one-off (owner DB role),
#    exits 0, THEN web/worker/beat start (depends_on:
#    service_completed_successfully) — this already runs `manage.py migrate
#    --noinput` for you; no separate manual migrate step needed.
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# 3. Confirm all 7 services are up and healthy (allow ~30-60s for
#    healthchecks to pass, especially postgres/web on first boot).
docker compose ps
```

**Create the first tenant + admin user.** There is a demo-only seed command
(`python manage.py seed_t0_6`) used in dev/CI that creates two throwaway
tenants with a known dev password (`DevPass123!`) — **do not run this in
production**, it's for local/CI verification only. Instead, create your
real first tenant and admin interactively:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec web \
  python manage.py shell
```

Then, at the shell prompt (paste as one block — replace the email/password):

```python
from apps.tenancy.models import Tenant
from apps.accounts.models import User
from apps.rbac.models import Membership, Role
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.tenancy.context import tenant_context

# Creating a Tenant auto-seeds its 4 system roles (post_save signal,
# apps/rbac/signals.py) — no separate role-seeding step needed.
tenant = Tenant.objects.create(slug="your-lab-slug", name="Your Lab Name")

with tenant_context(tenant.id):
    # Auto-grants a tenant-wide Member membership via post_save(User)
    # (apps/rbac/signals.py) — is_superuser/is_staff below only control
    # Django's own /django-admin/, NOT the application RBAC system
    # (see apps/accounts/models.py User.has_perm docstring) — the
    # Membership upgrade to Admin below is what matters for app access.
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

Exit the shell (`exit()`). This sequence was run and verified locally
against a throwaway prod-profile stack as part of authoring this runbook
(RLS correctly blocked the insert without `tenant_context(...)`, confirming
the tenant-scope requirement documented above — see the report for the
exact verification transcript).

Log in at `https://cortex.example.com` with that email/password once the
tunnel is live (§1) — tenant slug, email, and password are the three
login-form fields per `docs/rbac.md`'s login payload shape.

### 3d. Auto-restart across a NAS reboot

- Every service in `docker-compose.yml` already sets
  `restart: unless-stopped` — Docker itself will restart each container
  after the daemon restarts, **provided the daemon starts**.
- In DSM: **Container Manager → Project → (cortex project) → Settings** (or
  the project's action menu) → ensure **"Enable auto-startup"** / "Run this
  project when Container Manager starts" is turned on (exact label varies
  by DSM/Container Manager version) — this is what makes the *project*
  (and thus every `unless-stopped` service in it) come back after a full
  NAS power cycle, not just a container crash.
- Also confirm DSM itself is set to power back on after an outage
  (**Control Panel → Hardware & Power → General → "Power Recovery"**, if
  present on your model/DSM version) if you want survival through actual
  power loss, not just a manual reboot.
- **Verification:** reboot the NAS (DSM → Control Panel → Reboot, or
  physically power-cycle), wait ~2-3 minutes, then `docker compose ps` (or
  Container Manager's UI) should show all 7 services `Up`/`healthy` again
  with no manual `docker compose up` needed.

---

## 4. Verification checklist

Run through all of these after §1-3 are complete:

- [ ] **App reachable:** `https://cortex.example.com` loads the PWA over
      HTTPS with a valid Cloudflare-issued certificate (padlock, no browser
      warning).
- [ ] **Security headers present at the edge:**
      `curl -I https://cortex.example.com/` shows `strict-transport-security`,
      `x-content-type-options: nosniff`, `content-security-policy`, etc.
      (nginx sets these; Cloudflare's own HSTS setting reinforces at the
      edge — see §1a).
- [ ] **Login works:** log in with the admin user created in §3c
      (tenant slug + email + password) at `/api/v1/auth/login`; `/me`
      returns the expected permission set for the Admin role.
- [ ] **Login rate-limit fires:** issue 6+ rapid failed logins against
      `/api/v1/auth/login` from one IP within a minute and confirm
      Cloudflare's rule from §1a step 4 blocks further attempts (a `429`/
      block page from Cloudflare, distinct from the app's own throttle
      response) — confirms the edge rule is actually active, not just
      configured.
- [ ] **Camera scan works on a real phone:** open
      `https://cortex.example.com` on an actual phone browser (not
      localhost/dev), navigate to the QR-scan screen, and confirm the
      camera permission prompt appears and the scanner activates —
      `getUserMedia` requires a secure context, which the real HTTPS
      hostname now provides (this is the entire reason the mobile flow
      "just works" once the tunnel is live — see `docs/deployment.md` §3
      step 5).
- [ ] **Test email lands and passes auth:** trigger a real transactional
      email (e.g. an invite or password-reset flow) to a mailbox you
      control, then either (a) check the received message's headers for
      `spf=pass`, `dkim=pass`, `dmarc=pass` (most webmail providers expose
      "show original"/"view source"), or (b) send a one-off test message to
      a mail-tester-style service (e.g. mail-tester.com — send to the
      address it gives you, then check the score/report) and confirm all
      three checks pass. If any fail, re-check §2's DNS records
      (propagation delay, wrong selector name, or a duplicate/conflicting
      SPF record are the most common causes).
- [ ] **DSM admin is not reachable via the tunnel:** confirm
      `https://cortex.example.com` never exposes DSM itself (no public
      hostname route in Cloudflare points at DSM's port; DSM's own
      HTTPS/QuickConnect access remains whatever you already had, is
      unaffected by this deploy, and was never added as a tunnel route in
      §1).
- [ ] **Reboot survives** (§3d's verification step).

---

## 5. Backups & restore (T6.5)

Design-level rationale: `docs/deployment.md` §5. This section is the
step-by-step operator wiring plus a **proven restore drill** (R7) — every
command below was actually run against a throwaway local stack as part of
authoring this task; see §5d for the exact transcript and what it verified.

### 5a. The backup script

`docker/backup/backup.sh` is what DSM Task Scheduler invokes nightly. It:

1. Runs `pg_dump` **inside** the running `postgres` container (`docker
   compose exec postgres pg_dump ...`) — no client tools need to be
   installed on the NAS host itself, since the `postgres:16-alpine` image
   already ships `pg_dump`/`pg_restore`.
2. Uses **custom format** (`pg_dump -Fc`), not plain SQL. Custom format is
   compressed, and — the reason that matters here — `pg_restore` against a
   `-Fc` dump supports `--clean --if-exists` (drop-and-recreate existing
   objects before restoring, so a restore into an already-migrated fresh
   `postgres` — the normal case, since `docker-compose.yml`'s `migrate`
   one-off always runs before `web`/`worker`/`beat` start — doesn't collide
   with the schema `migrate` just created) and `-j` parallel restore.
   Plain SQL (`-Fp`, `psql < dump.sql`) would be simpler to eyeball/grep but
   loses both properties and is slower to restore once the DB has any real
   volume of data — not worth the tradeoff since `pg_restore` is already
   sitting in the same image at zero extra cost.
3. Copies the dump out of the container to a host-side `backups/daily/`
   directory (`docker compose cp`), so it survives independently of the
   `postgres` container/volume — this is the file Hyper Backup/offsite
   copies actually protect.
4. On Sundays, additionally copies that day's daily dump into
   `backups/weekly/`.
5. Rotates: keeps the newest **7 daily** + newest **4 weekly** dumps
   (`KEEP_DAILY`/`KEEP_WEEKLY`, overridable), deleting older ones by
   modification time — simple to audit, no date-arithmetic edge cases.
6. Logs every run to `backups/logs/backup-<timestamp>.log`; no interactive
   prompts; non-zero exit on any failure (missing `.env`, `postgres` not
   running, `pg_dump` failure, empty/missing dump after copy) so a cron
   failure is visible in Task Scheduler's own run history/exit-code column.

**DSM Task Scheduler wiring:**

1. **Control Panel → Task Scheduler → Create → Scheduled Task → User-defined
   script.**
2. **General:** Task name `cortex-nightly-backup`; User: `root` (needed to
   run `docker`/`docker compose` — Container Manager's Docker socket is
   root-owned by default on DSM).
3. **Schedule:** Daily, e.g. **02:30** (after normal usage hours, before any
   other nightly maintenance window you run).
4. **Task Settings → Run command:**
   ```bash
   cd /volume1/docker/cortex && \
     PROJECT_DIR=/volume1/docker/cortex \
     BACKUP_DIR=/volume1/docker/cortex/backups \
     COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml" \
     ENV_FILE=/volume1/docker/cortex/.env \
     bash docker/backup/backup.sh >> /volume1/docker/cortex/backups/logs/cron.log 2>&1
   ```
   (Working directory: the compose project root from §3a; matches
   `PROJECT_DIR`. Logs: `backup.sh` already writes its own per-run log file
   under `backups/logs/`; the `>>` above additionally captures anything
   printed directly to stdout/stderr by the wrapper invocation itself, e.g.
   a hard failure before the script's own logging starts.)
5. Save. **Run once manually** (task's own "Run" button) to confirm a
   `backups/daily/cortex-<timestamp>.dump` file appears and the task's exit
   code is `0` before trusting the schedule.

### 5b. Media — Hyper Backup

The `media` named volume (asset photos, generated label PDFs) is **not** a
bind-mounted host path by default (`docker-compose.yml`'s `volumes: media:`
is engine-managed, living under
`/volume1/@docker/volumes/cortex_media/_data` per §3a). Two options, in
order of operator-friendliness:

- **Recommended:** rebind `media` to an explicit host path so Hyper Backup
  can point at it directly, e.g. add `device:
  /volume1/docker/cortex/media-data` under a `driver_opts: type: none, o:
  bind` block for the `media` volume in a small local compose override (not
  `docker-compose.prod.yml` — a separate, NAS-only override kept outside git
  alongside `.env`), then point Hyper Backup at
  `/volume1/docker/cortex/media-data` as a normal shared-folder backup task
  target (Hyper Backup → **Backup Task → File Server/Shared Folder →**
  select the folder, destination = external USB and/or a cloud/B2 target
  per `docs/deployment.md` §5's tier-2 note).
- **Simpler, no compose change:** Hyper Backup can also target Docker's own
  volume path directly (`/volume1/@docker/volumes/cortex_media/_data`) if
  Container Manager's storage location is the default `@docker` system
  share — check **Control Panel → Shared Folder** for whether `@docker` is
  visible/selectable in Hyper Backup's folder picker (some DSM versions hide
  `@`-prefixed system shares from Hyper Backup's UI, in which case use the
  rebind option above instead).

Either way: **schedule Hyper Backup nightly**, after the `cortex-nightly-backup`
Task Scheduler job above (e.g. 03:00), so a given night's Hyper Backup run
also happens to pick up that night's fresh `pg_dump` file if the DB dump
directory (`/volume1/docker/cortex/backups`) is included in the same or a
second Hyper Backup task alongside media.

### 5c. `.env`/compose — encrypted offsite copy

`.env` contains every secret this stack has (`SECRET_KEY`, DB passwords,
`BREVO_API_KEY`, `TUNNEL_TOKEN`) — it must never leave the NAS unencrypted.
Simplest, lowest-maintenance option (no new crypto tooling introduced):

1. **DSM's own encrypted shared folder:** Control Panel → Shared Folder →
   Create → enable **Encryption** (DSM manages the AES key; you set/store
   the encryption key/password yourself, e.g. in a password manager — DSM
   can also save the key to a keystore file you back up separately). Copy
   `.env` + `docker-compose.yml` + `docker-compose.prod.yml` into that
   encrypted folder on a schedule (a second tiny Task Scheduler job, or
   simply re-copy by hand after any `.env` change — this file changes
   rarely). Hyper Backup can then back up *that* encrypted shared folder
   offsite exactly like any other, without ever exposing plaintext secrets
   to the offsite target.
2. **Alternative (no DSM encrypted-folder feature, or want portability
   independent of DSM): `age`** (already a static single-binary tool, no
   new crypto invented here — just referenced): encrypt with the operator's
   own recipient key before copying offsite:
   ```bash
   age -r <operator's-age-public-key> -o env-backup.age .env
   ```
   Store `env-backup.age` wherever the offsite copy goes; decrypt only when
   actually restoring (`age -d -i <private-key-file> env-backup.age >
   .env`). Never commit the private key, and never place it in the same
   offsite location as the encrypted file.

### 5d. Restore drill — executed and verified (R7)

This was actually run end-to-end locally (not just designed) as part of
this task. Commands below are the **real** ones that worked, run from the
repo root with a throwaway drill `.env` (copied from `.env.example`, only
`DJANGO_SETTINGS_MODULE` changed to `config.settings.dev` to avoid fighting
HTTPS-redirect/HSTS settings that need a real Cloudflare edge — irrelevant
to the backup/restore mechanics being verified):

```bash
# 1. Bring up a throwaway "source" stack and seed it with real data: a
#    tenant, an admin user (Admin role), an asset, an attachment (a real
#    file through apps.assets.services.save_attachment_file), and a label
#    PDF (through the real apps.labels.tasks.generate_label_pdf task) — via
#    `manage.py shell` snippets using tenant_context(...), the same
#    prod-safe pattern §3c's tenant/admin creation uses (NOT the dev-only
#    `seed_t0_6` management command).
docker compose -p cortex-drill-src --env-file <drill>.env build
docker compose -p cortex-drill-src --env-file <drill>.env up -d \
  postgres redis migrate web worker
# ... seed via `docker compose ... exec -T web python manage.py shell` ...

# 2. Confirm real files are on the media volume before backing up:
docker compose -p cortex-drill-src --env-file <drill>.env exec -T web \
  sh -c "find /app/media -type f -exec sha256sum {} \;"
#   -> attachments/1/1/..._drill-photo.jpg
#   -> labels/1/<job-uuid>.pdf

# 3. Run the real backup script against the running stack:
PROJECT_DIR=$(pwd) BACKUP_DIR=<drill>/backups \
  COMPOSE_FILES="-f docker-compose.yml" COMPOSE_PROJECT_NAME=cortex-drill-src \
  ENV_FILE=<drill>.env bash docker/backup/backup.sh
# -> backups/daily/cortex-<timestamp>.dump written (custom format, ~188 KB
#    for this tiny drill dataset), exit code 0.

# 4. Grab the media volume's contents the way Hyper Backup would (a plain
#    tar of the named volume, via a throwaway alpine container mounting it
#    read-only alongside the host backup dir):
docker run --rm -v cortex-drill-src_media:/media -v <drill>:/backup alpine \
  sh -c "cd /media && tar czf /backup/media-<date>.tar.gz ."

# 5. Tear the stack down COMPLETELY, including volumes (simulates total
#    data loss — the actual disaster this drill proves recovery from):
docker compose -p cortex-drill-src --env-file <drill>.env down -v

# 6. Bring up a BRAND NEW stack (different project name, fresh volumes;
#    `migrate` runs and creates an empty-but-migrated schema):
docker compose -p cortex-drill-restore --env-file <drill>.env up -d \
  postgres redis migrate

# 7. Restore the dump. Because `migrate` already created the schema/RLS
#    policies on the fresh DB, `--clean --if-exists` is required so
#    pg_restore drops those empty objects before recreating them from the
#    dump (confirms the -Fc format choice above was the right call):
docker compose -p cortex-drill-restore --env-file <drill>.env cp \
  backups/daily/cortex-<timestamp>.dump postgres:/tmp/restore.dump
docker compose -p cortex-drill-restore --env-file <drill>.env exec -T postgres \
  pg_restore -U cortex -d cortex --clean --if-exists --no-owner -v /tmp/restore.dump

# 8. Restore media into the fresh (empty) media volume:
docker run --rm -v cortex-drill-restore_media:/media -v <drill>:/backup alpine \
  sh -c "cd /media && tar xzf /backup/media-<date>.tar.gz"

# 9. Bring the app up and verify:
docker compose -p cortex-drill-restore --env-file <drill>.env up -d web worker nginx
```

**Verification — all passed:**

- `psql` query against the restored DB confirmed the exact same tenant
  (`drill-lab`), admin user, asset, attachment row, and succeeded label job
  row were all present with their original field values.
- Logged in as the same admin (`POST /api/v1/auth/login` with the tenant
  slug/email/password from step 1, via Django's test `Client` inside
  `manage.py shell` against the restored stack) — **200**, session
  established; `GET /api/v1/me` — **200**, same email; `GET /api/v1/assets/`
  — **200**, `["Drill Drone Alpha"]` (RLS/tenant-scoping intact post-restore,
  not just raw rows present).
- The attachment and the label PDF both **downloaded correctly through
  nginx** at their `/media/{storage_key}` paths (the same path shape the
  frontend uses — `apps.jobs.serializers.JobSerializer.get_download_url`,
  `AssetDetailScreen`) — **HTTP 200** for both, and `sha256sum` of the
  downloaded bytes on the restored stack **exactly matched** the pre-backup
  checksums taken in step 2 (byte-for-byte, not just "a file exists").

This confirms the whole chain — `pg_dump` → rotation → `pg_restore --clean
--if-exists` into an already-migrated fresh DB, plus a plain tar/untar of
the media volume — actually recovers a working, logged-in-able, RLS-correct
stack with intact binary attachments, not just a documented-but-unverified
procedure.

**One thing to note if you re-run this drill:** `docker-compose.yml`'s
`env_file: [.env]` on `web`/`worker`/`beat`/`migrate` resolves relative to
the **compose project directory** (wherever `docker-compose.yml` itself
lives — normally the repo root), not to whatever `--env-file` you pass on
the `docker compose` command line. `--env-file` only controls `${VAR}`
interpolation *within* the compose YAML (e.g. `POSTGRES_DB: ${POSTGRES_DB}`)
— it does not redirect the `env_file:` a service reads at container
runtime. For an isolated drill, either point `docker-compose.yml`'s
`env_file:` at your drill file (temporary local edit, not committed), or —
simpler, and what was actually done above — keep a real `.env` at the repo
root with matching throwaway values so both paths agree.

---

## 6. Cross-references

- Design-level topology, memory budget, and the full §7 hardening checklist
  with verification status: `docs/deployment.md`.
- Backup script + DSM Task Scheduler wiring, Hyper Backup media path,
  encrypted `.env` offsite copy, and the executed restore drill: §5 above.
- Load test against the deployed stack: T6.6 (`tests/load/*`).
