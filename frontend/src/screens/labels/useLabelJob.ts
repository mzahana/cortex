import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { Job, LabelSheetTemplate } from "../../api/types";

const POLL_INTERVAL_MS = 1000;

interface UseLabelJobResult {
  job: Job | null;
  submitting: boolean;
  error: string | null;
  /** `POST /api/v1/labels/generate` then start polling `GET /api/v1/jobs/{id}`
   * until it lands on `succeeded`/`failed`. */
  generate: (assetIds: number[], template: LabelSheetTemplate) => Promise<void>;
  /** Clears the current job/error so the picker can start a fresh run
   * (Generate button re-enables once a run has finished). */
  reset: () => void;
}

/**
 * Submit + poll one label-generation `Job` (T4.5, `docs/api-and-ui.md`'s
 * "poll `GET /api/v1/jobs/{id}`" contract). A simple `setInterval` poll —
 * matching the task's own "simple interval poll, a few hundred ms-1s"
 * guidance, same `useDashboardSummary`-style request-id-guarded state
 * updates (a stale in-flight poll response landing after `reset()`/a new
 * `generate()` call is dropped, never overwrites newer state).
 */
export function useLabelJob(): UseLabelJobResult {
  const [job, setJob] = useState<Job | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestIdRef = useRef(0);

  const toMessage = (err: unknown): string =>
    err instanceof ApiError
      ? (err.problem.detail ?? err.problem.title)
      : "Unable to reach the server. Please try again.";

  const generate = useCallback(async (assetIds: number[], template: LabelSheetTemplate) => {
    const requestId = ++requestIdRef.current;
    setSubmitting(true);
    setError(null);
    setJob(null);
    try {
      const created = await api.generateLabels({ asset_ids: assetIds, template });
      if (requestId !== requestIdRef.current) return; // superseded — drop
      setJob(created);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(toMessage(err));
    } finally {
      if (requestId === requestIdRef.current) setSubmitting(false);
    }
  }, []);

  // Poll while the current job is still in flight (queued/running). Stops
  // itself the moment the job lands on a terminal status, or if `reset()`/
  // a new `generate()` call bumps `requestIdRef` out from under it.
  useEffect(() => {
    if (!job || job.status === "succeeded" || job.status === "failed") return;
    const requestId = requestIdRef.current;
    const jobId = job.id;

    const timer = window.setInterval(async () => {
      try {
        const updated = await api.getJob(jobId);
        if (requestId !== requestIdRef.current) return; // superseded — drop
        setJob(updated);
      } catch (err) {
        if (requestId !== requestIdRef.current) return;
        setError(toMessage(err));
      }
    }, POLL_INTERVAL_MS);

    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.id, job?.status]);

  const reset = useCallback(() => {
    requestIdRef.current += 1; // invalidate any in-flight poll/submit
    setJob(null);
    setError(null);
    setSubmitting(false);
  }, []);

  return { job, submitting, error, generate, reset };
}
