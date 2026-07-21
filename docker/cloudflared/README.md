# `docker/cloudflared/` — no local config file needed (by design)

This directory intentionally holds **no `config.yml`**. Cortex uses
Cloudflare's **token-based ("remotely-managed") tunnel** pattern end to end:

- `docker-compose.yml`'s `cloudflared` service already runs
  `command: ["tunnel", "run"]` with `TUNNEL_TOKEN` supplied via `.env`
  (never committed — see `.env.example`).
- With a **token** (as opposed to a locally-generated tunnel **credentials
  file** + `config.yml`), `cloudflared` fetches its entire ingress
  configuration — the public hostname route, the origin service
  (`http://nginx:80`), TLS settings — from the **Cloudflare dashboard** at
  connect time. There is nothing left to template into a local file: every
  value that would normally live in `config.yml`'s `ingress:` block is
  configured once in **Zero Trust → Networks → Tunnels → (tunnel) → Public
  Hostname** instead (see `docs/deployment-runbook.md` §1).

**Why token-based over a local `config.yml` for this deployment:**
- One fewer secret/config artifact to keep in sync between the NAS and the
  dashboard — the route lives in exactly one place (Cloudflare), not
  duplicated in a mounted file the operator could forget to update.
- No tunnel credentials JSON file to generate, mount read-only, and protect
  on the NAS filesystem — the `TUNNEL_TOKEN` env var (already in the
  mounted, read-only `.env` per `docs/deployment.md` §6) is the only secret,
  consistent with this project's "all secrets from `.env`" invariant.
- Matches this repo's existing `docker-compose.yml` service definition
  exactly as-is (`command: ["tunnel", "run"]`, no `--config` flag, no extra
  bind mount) — T0.2 already chose this pattern; this task does not change
  it.
- A local `config.yml` + ingress rules is the better fit when a single
  tunnel must front **many** origin services with complex path-based
  routing, or when the ingress must be reviewed/diffed in git. Cortex fronts
  exactly one origin (`nginx:80`) behind exactly one hostname, so that
  complexity buys nothing here.

If a second public hostname is ever added (e.g. a separate staging tunnel),
prefer a second dashboard-managed tunnel + token over introducing a
`config.yml` here, to keep the "one secret, one source of truth" property.
Only revisit this decision if ingress rules genuinely outgrow the dashboard
UI (e.g. many origins on one tunnel needing path-based routing not
expressible as separate hostnames).

See `docs/deployment-runbook.md` for the exact dashboard steps (create
tunnel, copy token into `.env`, add the public hostname route, TLS/HSTS/Bot
Fight/rate-limit settings).
