# docker/

Infra scaffolding for the 7-service Synology deployment (filled in T0.2+):

```
docker/
  Dockerfile          # single image shared by web + worker (different commands)
  redis.conf          # maxmemory 256mb, allkeys-lru
  nginx/
    Dockerfile         # multi-stage: node builds ../frontend, then nginx:alpine
    nginx.conf
    default.conf      # reverse proxy to web:8000, serves /static and /media
  cloudflared/
    config.yml        # (if using config-based tunnel instead of token-only)
```

Root `docker-compose.yml` wires: postgres, redis, web, worker, beat, nginx,
cloudflared. See `../docs/deployment.md` for `mem_limit`s and the Container
Manager runbook.

For a production deploy, layer `docker-compose.prod.yml` (repo root) on top —
forces `DJANGO_SETTINGS_MODULE=config.settings.prod` and exposes
`GUNICORN_WORKERS` (default 2). See `../docs/deployment.md` §9 for the
hardening checklist verification and memory-budget arithmetic:

```
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```
