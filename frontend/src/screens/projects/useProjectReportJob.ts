import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { Job } from "../../api/types";

const POLL_INTERVAL_MS = 1000;

interface UseProjectReportJobResult {
  job: Job | null;
  submitting: boolean;
  error: string | null;
  /** `POST /api/v1/projects/{id}/report` then poll `GET /api/v1/jobs/{id}`
   * until it lands on `succeeded`/`failed` — same job-poll contract as
   * `useLabelJob` (T4.5's label-PDF pattern), reused here unchanged for the
   * M7 project report PDF. `options.includeInvoiceScans` opts into
   * embedding invoice/receipt images in the rendered PDF;
   * `options.includeProjectDocuments` opts into appending each uploaded
   * project document's full pages to the PDF. Both default to `false`. */
  generate: (
    projectId: number,
    options?: { includeInvoiceScans?: boolean; includeProjectDocuments?: boolean },
  ) => Promise<void>;
  reset: () => void;
}

/**
 * Submit + poll one project-report-PDF `Job`
 * (`docs/tasks/M7-project-grants.md`: "one click produces a structured PDF
 * report"). Mirrors `useLabelJob` exactly (same simple `setInterval` poll,
 * same request-id-guarded state updates so a stale in-flight poll response
 * can never overwrite newer state) — the only difference is the submit call
 * (`api.generateProjectReport` instead of `api.generateLabels`).
 */
export function useProjectReportJob(): UseProjectReportJobResult {
  const [job, setJob] = useState<Job | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestIdRef = useRef(0);

  const toMessage = (err: unknown): string =>
    err instanceof ApiError
      ? (err.problem.detail ?? err.problem.title)
      : "Unable to reach the server. Please try again.";

  const generate = useCallback(
    async (
      projectId: number,
      options?: { includeInvoiceScans?: boolean; includeProjectDocuments?: boolean },
    ) => {
      const requestId = ++requestIdRef.current;
      setSubmitting(true);
      setError(null);
      setJob(null);
      try {
        const created = await api.generateProjectReport(projectId, options);
        if (requestId !== requestIdRef.current) return;
        setJob(created);
      } catch (err) {
        if (requestId !== requestIdRef.current) return;
        setError(toMessage(err));
      } finally {
        if (requestId === requestIdRef.current) setSubmitting(false);
      }
    },
    [],
  );

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
