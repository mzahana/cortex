# docker/

Infra scaffolding for the 7-service Synology deployment (filled in T0.2+):

```
docker/
  Dockerfile          # single image shared by web + worker (different commands)
  redis.conf          # maxmemory 256mb, allkeys-lru
  nginx/
    nginx.conf
    default.conf      # reverse proxy to web:8000, serves /static and /media
  cloudflared/
    config.yml        # (if using config-based tunnel instead of token-only)
```

Root `docker-compose.yml` wires: postgres, redis, web, worker, beat, nginx,
cloudflared. See `../docs/deployment.md` for `mem_limit`s and the Container
Manager runbook.
