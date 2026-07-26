# ------------------------------------------------------------------------------
# Multi-stage nginx image: bundles the built React/Vite PWA into the final
# `nginx:alpine` image, so `docker compose build` alone is enough to produce a
# working frontend — no manual `npm install && npm run build` step, and no
# `frontend/dist` bind mount required. Needed because the operator only ever
# uses Synology Container Manager's GUI (Project → Build/Start), never a NAS
# terminal, so any pre-build CLI step would silently block deploys.
#
# Stage 1 builds the SPA with the same Node version pinned in CI
# (.github/workflows/ci.yml NODE_VERSION). Stage 2 is the real, final nginx
# image — base pinned by digest per docs/deployment.md §7 hardening checklist
# (kept in sync with the digest previously pinned directly in
# docker-compose.yml's `nginx.image`). `nginx.conf`/`default.conf` are NOT
# baked in here; docker-compose.yml still bind-mounts them read-only so an
# operator can tweak nginx config without rebuilding the image.
# ------------------------------------------------------------------------------
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS frontend-builder

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10

COPY --from=frontend-builder /app/dist /usr/share/nginx/html
