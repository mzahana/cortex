import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { Job } from "../../api/types";

const POLL_INTERVAL_MS = 1000;

export interface ProjectArchiveOptions {
  includeDocuments?: boolean;
  includeInvoices?: boolean;
  includeAssetAttachments?: boolean;
}

interface UseProjectArchiveJobResult {
  job: Job | null;
  submitting: boolean;
  error: string | null;
  generate: (projectId: number, options?: ProjectArchiveOptions) => Promise<void>;
  reset: () => void;
}

/**
 * Submit + poll one project-archive ZIP `Job`
 * (`POST /api/v1/projects/{id}/archive`). Mirrors `useProjectReportJob`/
 * `useLabelJob` exactly — same `setInterval` poll, same request-id guard so a
 * stale in-flight poll response can never overwrite newer state. The only
 * difference is the submit call.
 *
 * Failure is a normal outcome here, not an exception: the server fails the
 * job (rather than the request) when a project's files exceed the archive
 * size cap, and its message tells the user what to deselect — so `job.error`
 * is rendered as-is by the caller.
 */
export function useProjectArchiveJob(): UseProjectArchiveJobResult {
  const [job, setJob] = useState<Job | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestIdRef = useRef(0);

  const toMessage = (err: unknown): string =>
    err instanceof ApiError
      ? (err.problem.detail ?? err.problem.title)
      : "Unable to reach the server. Please try again.";

  const generate = useCallback(async (projectId: number, options?: ProjectArchiveOptions) => {
    const requestId = ++requestIdRef.current;
    setSubmitting(true);
    setError(null);
    setJob(null);
    try {
      const created = await api.generateProjectArchive(projectId, options);
      if (requestId !== requestIdRef.current) return;
      setJob(created);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(toMessage(err));
    } finally {
      if (requestId === requestIdRef.current) setSubmitting(false);
    }
  }, []);

  useEffect(() => {
    if (!job || job.status === "succeeded" || job.status === "failed") return;
    const requestId = requestIdRef.current;
    const jobId = job.id;

    const timer = window.setInterval(async () => {
      try {
        const updated = await api.getJob(jobId);
        if (requestId !== requestIdRef.current) return;
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
    requestIdRef.current += 1;
    setJob(null);
    setError(null);
    setSubmitting(false);
  }, []);

  return { job, submitting, error, generate, reset };
}
