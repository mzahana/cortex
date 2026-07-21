#!/usr/bin/env bash
#
# Cortex nightly backup script (T6.5).
#
# What DSM Task Scheduler runs on a cron. Dumps the `postgres` service's
# database via `pg_dump` INSIDE the running postgres container (no client
# tools need to be installed on the NAS host — the postgres:16-alpine image
# already ships pg_dump), rotates old dumps (7 daily + 4 weekly), and logs to
# a dated log file. No interactive prompts; proper exit codes for cron.
#
# Usage (invoked by DSM Task Scheduler, or manually for a drill):
#   BACKUP_DIR=/volume1/docker/cortex/backups \
#   PROJECT_DIR=/volume1/docker/cortex \
#     docker/backup/backup.sh
#
# See docs/deployment-runbook.md's "Backups & restore" section for the exact
# Task Scheduler wiring (cron expression, working directory, log location)
# and docs/deployment.md §5 for the design-level rationale.
set -euo pipefail

# --- Configuration (override via environment) --------------------------------
# Directory the compose project lives in (where `docker-compose.yml` and
# `.env` sit) — needed so `docker compose` resolves the right project/env
# file regardless of the cron job's own working directory.
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
# Where dump files + logs are written. Must be a path OUTSIDE the containers
# (a Synology shared folder in prod) so Hyper Backup/offsite copies can pick
# it up independently of the compose stack's own volumes.
BACKUP_DIR="${BACKUP_DIR:-${PROJECT_DIR}/backups}"
# Compose files to use — same pair the prod deploy runs (T6.3 overlay), but
# overridable for the local restore-drill stack (which may use a project
# name / env-file that differs from the real NAS layout).
COMPOSE_FILES="${COMPOSE_FILES:--f docker-compose.yml -f docker-compose.prod.yml}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-}"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"
POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
# Retention: keep the last N daily dumps and the last M weekly dumps.
KEEP_DAILY="${KEEP_DAILY:-7}"
KEEP_WEEKLY="${KEEP_WEEKLY:-4}"

DATE_STAMP="$(date +%Y%m%d-%H%M%S)"
DAY_OF_WEEK="$(date +%u)"  # 1 = Monday .. 7 = Sunday
LOG_DIR="${BACKUP_DIR}/logs"
LOG_FILE="${LOG_DIR}/backup-${DATE_STAMP}.log"

mkdir -p "${BACKUP_DIR}/daily" "${BACKUP_DIR}/weekly" "${LOG_DIR}"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "${LOG_FILE}"
}

fail() {
  log "ERROR: $*"
  exit 1
}

cd "${PROJECT_DIR}" || fail "cannot cd into PROJECT_DIR=${PROJECT_DIR}"

[ -f "${ENV_FILE}" ] || fail "env file not found at ${ENV_FILE}"

# Load POSTGRES_DB / POSTGRES_USER from .env without sourcing the whole file
# (avoids executing anything unexpected in a secrets file).
POSTGRES_DB="$(grep -E '^POSTGRES_DB=' "${ENV_FILE}" | tail -n1 | cut -d= -f2-)"
POSTGRES_USER="$(grep -E '^POSTGRES_USER=' "${ENV_FILE}" | tail -n1 | cut -d= -f2-)"
[ -n "${POSTGRES_DB}" ] || fail "POSTGRES_DB not set in ${ENV_FILE}"
[ -n "${POSTGRES_USER}" ] || fail "POSTGRES_USER not set in ${ENV_FILE}"

# shellcheck disable=SC2206
COMPOSE_ARGS=(${COMPOSE_FILES})
if [ -n "${COMPOSE_PROJECT_NAME}" ]; then
  COMPOSE_ARGS=(-p "${COMPOSE_PROJECT_NAME}" "${COMPOSE_ARGS[@]}")
fi

log "Starting backup: db=${POSTGRES_DB} user=${POSTGRES_USER} project_dir=${PROJECT_DIR}"

# Confirm the postgres service is actually up before attempting the dump —
# fail loudly (and non-zero) rather than silently writing an empty/broken
# dump file that a later restore would fail on.
if ! docker compose "${COMPOSE_ARGS[@]}" ps --status running "${POSTGRES_SERVICE}" \
      | grep -q "${POSTGRES_SERVICE}"; then
  fail "${POSTGRES_SERVICE} service is not running — aborting backup"
fi

DAILY_FILE="${BACKUP_DIR}/daily/cortex-${DATE_STAMP}.dump"
TMP_FILE="${DAILY_FILE}.in-progress"

# Custom format (-Fc): compressed, supports selective/parallel restore
# (pg_restore -j), and lets us restore into a database that doesn't
# perfectly pre-exist (pg_restore -C can create it) or re-run against an
# already-populated one with --clean --if-exists. Plain SQL (-Fp) would be
# simpler to eyeball/grep but loses those restore-flexibility properties and
# is slower to restore on anything beyond toy data volumes — not worth the
# tradeoff here since pg_restore is already available in the same image.
if ! docker compose "${COMPOSE_ARGS[@]}" exec -T "${POSTGRES_SERVICE}" \
      pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -Fc -f "/tmp/cortex-${DATE_STAMP}.dump"; then
  fail "pg_dump failed"
fi

if ! docker compose "${COMPOSE_ARGS[@]}" cp \
      "${POSTGRES_SERVICE}:/tmp/cortex-${DATE_STAMP}.dump" "${TMP_FILE}"; then
  fail "failed to copy dump out of the ${POSTGRES_SERVICE} container"
fi

# Clean up the in-container temp file regardless of the copy's outcome above
# having already been checked.
docker compose "${COMPOSE_ARGS[@]}" exec -T "${POSTGRES_SERVICE}" \
  rm -f "/tmp/cortex-${DATE_STAMP}.dump" || log "WARNING: could not remove in-container temp dump (non-fatal)"

[ -s "${TMP_FILE}" ] || fail "dump file is empty or missing after copy: ${TMP_FILE}"
mv "${TMP_FILE}" "${DAILY_FILE}"
log "Daily dump written: ${DAILY_FILE} ($(du -h "${DAILY_FILE}" | cut -f1))"

# Sunday (day 7) dumps are additionally kept as the week's weekly snapshot —
# a simple copy, cheap relative to the dump itself, and keeps the weekly
# rotation logic trivial to audit (no separate weekly pg_dump run needed).
if [ "${DAY_OF_WEEK}" = "7" ]; then
  WEEKLY_FILE="${BACKUP_DIR}/weekly/cortex-${DATE_STAMP}.dump"
  cp "${DAILY_FILE}" "${WEEKLY_FILE}"
  log "Weekly snapshot written: ${WEEKLY_FILE}"
fi

# --- Rotation: keep the newest KEEP_DAILY daily dumps and KEEP_WEEKLY
# weekly dumps; delete anything older. Listing by mtime (newest first) and
# tailing past the keep-count is simpler and easier to audit than
# date-arithmetic-based `find -mtime`, and works the same regardless of how
# often the job actually runs.
rotate() {
  local dir="$1" keep="$2"
  local files
  mapfile -t files < <(ls -1t "${dir}"/cortex-*.dump 2>/dev/null || true)
  local count=${#files[@]}
  if [ "${count}" -gt "${keep}" ]; then
    local i
    for ((i = keep; i < count; i++)); do
      log "Rotating out old backup: ${files[$i]}"
      rm -f "${files[$i]}"
    done
  fi
}

rotate "${BACKUP_DIR}/daily" "${KEEP_DAILY}"
rotate "${BACKUP_DIR}/weekly" "${KEEP_WEEKLY}"

log "Backup complete."
exit 0
