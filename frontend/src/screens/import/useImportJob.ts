import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { ImportJob, ImportMapping } from "../../api/types";

const POLL_INTERVAL_MS = 1000;

const IN_FLIGHT_STATUSES = new Set<ImportJob["status"]>([
  "pending",
  "dry_run_running",
  "committing",
]);

interface UseImportJobResult {
  importJob: ImportJob | null;
  /** True while the initial upload (`createImport`) or a commit
   * (`commitImport`) request itself is in flight — distinct from `polling`
   * (the async dry-run/commit task running server-side afterward). */
  submitting: boolean;
  /** True while the current job's dry-run/commit task is still
   * queued/running server-side (`useImportJob` is polling `GET /imports/
   * {id}` for it). */
  polling: boolean;
  error: string | null;
  /** `POST /api/v1/imports` (multipart upload + optional mapping override),
   * then starts polling `GET /api/v1/imports/{id}` until the dry-run lands
   * on `dry_run_succeeded`/`dry_run_failed`. */
  upload: (file: File, mapping?: ImportMapping) => Promise<void>;
  /** `POST /api/v1/imports/{id}/commit` against the CURRENT `importJob`,
   * then polls until `committed`/`commit_failed`. */
  commit: (mapping?: ImportMapping) => Promise<void>;
  /** Clears all state so the wizard can start over with a fresh file. */
  reset: () => void;
}

/**
 * Submit + poll one bulk-import `ImportJob` (T6.2, `docs/tasks/
 * M6-import-export-deploy.md`). Polls `GET /api/v1/imports/{id}` directly
 * (not the underlying `GET /jobs/{id}`) since that's where the richer
 * mapping/report state actually lives (`apps.imports.models.ImportJob`
 * docstring) — same simple `setInterval` + request-id-guarded-state-update
 * shape as `useLabelJob` (this screen's closest sibling).
 */
export function useImportJob(): UseImportJobResult {
  const [importJob, setImportJob] = useState<ImportJob | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestIdRef = useRef(0);

  const toMessage = (err: unknown): string =>
    err instanceof ApiError
      ? (err.problem.detail ?? err.problem.title)
      : "Unable to reach the server. Please try again.";

  const upload = useCallback(async (file: File, mapping?: ImportMapping) => {
    const requestId = ++requestIdRef.current;
    setSubmitting(true);
    setError(null);
    setImportJob(null);
    try {
      const created = await api.createImport(file, mapping);
      if (requestId !== requestIdRef.current) return; // superseded — drop
      setImportJob(created);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(toMessage(err));
    } finally {
      if (requestId === requestIdRef.current) setSubmitting(false);
    }
  }, []);

  const commit = useCallback(
    async (mapping?: ImportMapping) => {
      if (!importJob) return;
      const requestId = ++requestIdRef.current;
      setSubmitting(true);
      setError(null);
      try {
        const updated = await api.commitImport(importJob.id, mapping);
        if (requestId !== requestIdRef.current) return; // superseded — drop
        setImportJob(updated);
      } catch (err) {
        if (requestId !== requestIdRef.current) return;
        setError(toMessage(err));
      } finally {
        if (requestId === requestIdRef.current) setSubmitting(false);
      }
    },
    [importJob],
  );

  // Poll while the current job's dry-run/commit task is still in flight.
  // Stops itself the moment it lands on a terminal status, or if `reset()`/
  // a new `upload()`/`commit()` call bumps `requestIdRef` out from under it.
  useEffect(() => {
    if (!importJob || !IN_FLIGHT_STATUSES.has(importJob.status)) return;
    const requestId = requestIdRef.current;
    const importJobId = importJob.id;

    const timer = window.setInterval(async () => {
      try {
        const updated = await api.getImport(importJobId);
        if (requestId !== requestIdRef.current) return; // superseded — drop
        setImportJob(updated);
      } catch (err) {
        if (requestId !== requestIdRef.current) return;
        setError(toMessage(err));
      }
    }, POLL_INTERVAL_MS);

    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [importJob?.id, importJob?.status]);

  const polling = importJob !== null && IN_FLIGHT_STATUSES.has(importJob.status);

  const reset = useCallback(() => {
    requestIdRef.current += 1; // invalidate any in-flight poll/submit
    setImportJob(null);
    setError(null);
    setSubmitting(false);
  }, []);

  return { importJob, submitting, polling, error, upload, commit, reset };
}
