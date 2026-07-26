import { useState } from "react";
import { Alert, Button, Card, Checkbox, Group, Loader, SimpleGrid, Stack, Text, Title } from "@mantine/core";
import { api } from "../../api/client";
import { useProjectReportJob } from "./useProjectReportJob";

const CSV_FIELDS: { value: string; label: string }[] = [
  { value: "date", label: "Date" },
  { value: "category", label: "Category" },
  { value: "vendor", label: "Vendor" },
  { value: "invoice_number", label: "Invoice #" },
  { value: "amount", label: "Amount" },
  { value: "currency", label: "Currency" },
  { value: "description", label: "Description" },
  { value: "asset", label: "Asset" },
];

interface ReportExportTabProps {
  projectId: number;
}

/**
 * Project hub — Report tab (`docs/tasks/M7-project-grants.md`: "one click
 * produces a structured PDF report and a field-selectable spreadsheet").
 *
 * **Report PDF**: `POST /projects/{id}/report` -> poll `GET /jobs/{id}` ->
 * download, reusing the EXACT same async-job poller pattern the Labels
 * screen established for label-sheet PDFs (`useProjectReportJob` mirrors
 * `useLabelJob`; the polling/status-rendering JSX below mirrors
 * `LabelsScreen`'s own).
 *
 * **Export CSV**: a field-selection checklist -> a plain `<a>` navigation to
 * `GET /projects/{id}/export.csv?fields=...` (same "browser navigation,
 * session cookie rides along" pattern as the Asset List's own CSV export —
 * `api.exportProjectCsvUrl` builds the URL, never fetches the body itself).
 *
 * Both actions require `expense.view` scoped to this project server-side
 * (the report/export inline the same redacted financial figures as the
 * Overview tab) — a caller without it gets a 403 on `POST report`,
 * surfaced via `useProjectReportJob`'s `error` state, same "403 is a normal
 * handled outcome" posture as everywhere else in this app.
 */
export function ReportExportTab({ projectId }: ReportExportTabProps) {
  const { job, submitting, error, generate, reset } = useProjectReportJob();
  const [selectedFields, setSelectedFields] = useState<string[]>(CSV_FIELDS.map((f) => f.value));

  const isPolling = job !== null && (job.status === "queued" || job.status === "running");
  const isDone = job !== null && (job.status === "succeeded" || job.status === "failed");

  const toggleField = (value: string) => {
    setSelectedFields((prev) =>
      prev.includes(value) ? prev.filter((f) => f !== value) : [...prev, value],
    );
  };

  return (
    <Stack gap="lg" data-testid="report-export-tab">
      <Card withBorder padding="md">
        <Title order={5} mb="sm">
          PDF report
        </Title>
        <Text size="sm" c="dimmed" mb="sm">
          A structured audit-ready report: grant metadata, budget vs. spend, the
          per-category breakdown, and the itemized expense ledger.
        </Text>

        {!isPolling && !isDone && (
          <>
            {error && (
              <Alert color="red" mb="sm" title="Couldn't generate the report">
                {error}
              </Alert>
            )}
            <Button
              onClick={() => void generate(projectId)}
              loading={submitting}
              data-testid="generate-report-button"
            >
              Generate report
            </Button>
          </>
        )}

        {isPolling && (
          <Group gap="sm" data-testid="report-job-polling">
            <Loader size="sm" />
            <Text c="dimmed">{job?.status === "running" ? "Rendering your report…" : "Queued…"}</Text>
          </Group>
        )}

        {isDone && job?.status === "succeeded" && (
          <Stack gap="sm" data-testid="report-job-succeeded">
            <Text fw={600}>Your report is ready.</Text>
            <Group>
              <Button
                component="a"
                href={job.download_url ?? undefined}
                download={job.result_filename || undefined}
                data-testid="report-download-link"
              >
                Download PDF
              </Button>
              <Button variant="light" onClick={reset}>
                Generate again
              </Button>
            </Group>
          </Stack>
        )}

        {isDone && job?.status === "failed" && (
          <Stack gap="sm" data-testid="report-job-failed">
            <Alert color="red" title="Report generation failed">
              {job.error || "Something went wrong while rendering the PDF."}
            </Alert>
            <Button variant="light" onClick={reset}>
              Try again
            </Button>
          </Stack>
        )}
      </Card>

      <Card withBorder padding="md">
        <Title order={5} mb="sm">
          Export CSV
        </Title>
        <Text size="sm" c="dimmed" mb="sm">
          Choose which columns to include in the expense ledger export.
        </Text>
        <SimpleGrid cols={{ base: 2, sm: 4 }} mb="sm">
          {CSV_FIELDS.map((f) => (
            <Checkbox
              key={f.value}
              label={f.label}
              checked={selectedFields.includes(f.value)}
              onChange={() => toggleField(f.value)}
            />
          ))}
        </SimpleGrid>
        <Button
          component="a"
          href={api.exportProjectCsvUrl(projectId, selectedFields)}
          variant="light"
          disabled={selectedFields.length === 0}
          data-testid="export-project-csv-button"
        >
          Export CSV
        </Button>
      </Card>
    </Stack>
  );
}
