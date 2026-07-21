import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  FileInput,
  Group,
  Loader,
  ScrollArea,
  Select,
  Stack,
  Table,
  Text,
} from "@mantine/core";
import { useNavigate } from "react-router-dom";
import { IMPORT_CORE_TARGETS, type ImportMapping, type ImportReportRow } from "../../api/types";
import { AppLayout } from "../../layout/AppLayout";
import { useImportJob } from "./useImportJob";

const TARGET_OPTIONS = [
  ...IMPORT_CORE_TARGETS.map((t) => ({ value: t, label: t })),
  { value: "custom", label: "custom field (match by column name)" },
  { value: "ignore", label: "ignore this column" },
];

function rowErrorSummary(row: ImportReportRow): string {
  const parts: string[] = [];
  for (const [key, message] of Object.entries(row.errors)) {
    if (key === "custom_field_values" && message && typeof message === "object") {
      for (const [fieldKey, fieldMessage] of Object.entries(message as Record<string, unknown>)) {
        parts.push(`${fieldKey}: ${String(fieldMessage)}`);
      }
    } else {
      parts.push(String(message));
    }
  }
  return parts.join("; ");
}

/**
 * Bulk import wizard (T6.2, `docs/tasks/M6-import-export-deploy.md`): upload
 * a CSV/xlsx -> review the server's auto-detected column mapping (override
 * per-column if needed, optionally re-running the dry-run) -> review the
 * dry-run report (valid/invalid row counts + per-row errors) -> commit.
 *
 * `import.run` (Admin-only, tenant-wide, `docs/rbac.md` §3) is enforced
 * server-side on every one of these endpoints; this screen's own route is
 * gated in `App.tsx`/`DashboardScreen.tsx` via `hasImportRunPermission`
 * purely for presentation (CLAUDE.md: "a 403 is a normal, handled outcome,
 * not a bug") — an unauthorized call still just surfaces through `error`.
 *
 * Commit is all-or-nothing server-side (`apps.imports.services` module
 * docstring): the Commit button is disabled whenever the current report has
 * ANY invalid row, matching that behavior instead of optimistically letting
 * the click through only to get a `commit_failed` back.
 */
export function ImportScreen() {
  const navigate = useNavigate();
  const { importJob, submitting, polling, error, upload, commit, reset } = useImportJob();

  const [file, setFile] = useState<File | null>(null);
  const [mappingDraft, setMappingDraft] = useState<ImportMapping>({});

  // Re-seed the editable mapping draft whenever a NEW report arrives (a
  // fresh dry-run/commit result, i.e. a different `resolved_mapping`) — but
  // never clobber in-progress edits the user hasn't re-submitted yet.
  const resolvedMapping = importJob?.report?.resolved_mapping ?? null;
  useEffect(() => {
    if (resolvedMapping) setMappingDraft(resolvedMapping);
  }, [resolvedMapping]);

  const headers = useMemo(() => Object.keys(resolvedMapping ?? {}), [resolvedMapping]);

  const handleUpload = () => {
    if (!file) return;
    void upload(file);
  };

  const handleRecheckMapping = () => {
    if (!file) return;
    void upload(file, mappingDraft);
  };

  const handleCommit = () => {
    void commit(mappingDraft);
  };

  const handleStartOver = () => {
    reset();
    setFile(null);
    setMappingDraft({});
  };

  const report = importJob?.report ?? null;
  const invalidRows = useMemo(() => report?.rows.filter((r) => r.errors && Object.keys(r.errors).length > 0) ?? [], [report]);

  const showUploadStep = !importJob;
  const showProgressStep = importJob !== null && polling;
  const showHardFailureStep =
    importJob !== null && !polling && importJob.status === "dry_run_failed" && !report;
  const showSuccessStep = importJob !== null && !polling && importJob.status === "committed";
  const showReviewStep =
    importJob !== null &&
    !polling &&
    !showHardFailureStep &&
    !showSuccessStep &&
    report !== null;

  return (
    <AppLayout title="Bulk Import">
        <Stack gap="md" data-testid="import-screen">
          {error && (
            <Alert color="red" title="Something went wrong">
              {error}
            </Alert>
          )}

          {showUploadStep && (
            <Stack gap="sm" data-testid="import-upload-step">
              <Text size="sm" c="dimmed">
                Upload a CSV or Excel (.xlsx) spreadsheet of assets. Columns are
                auto-matched to asset fields — you can review and override the
                mapping before anything is created.
              </Text>
              <FileInput
                label="Spreadsheet"
                placeholder="Choose a .csv or .xlsx file"
                accept=".csv,.xlsx"
                value={file}
                onChange={setFile}
                data-testid="import-file-input"
              />
              <Button
                fullWidth
                size="lg"
                disabled={!file}
                loading={submitting}
                onClick={handleUpload}
                data-testid="import-upload-button"
              >
                Upload &amp; validate
              </Button>
            </Stack>
          )}

          {showProgressStep && (
            <Stack align="center" gap="sm" py="xl" data-testid="import-progress-step">
              <Loader />
              <Text c="dimmed">
                {importJob?.status === "committing" ? "Committing your import…" : "Validating your file…"}
              </Text>
            </Stack>
          )}

          {showHardFailureStep && (
            <Stack gap="sm" data-testid="import-hard-failure-step">
              <Alert color="red" title="Import failed">
                {importJob?.dry_run_job?.error || "The file couldn't be read. Check the format and try again."}
              </Alert>
              <Button variant="light" onClick={handleStartOver} data-testid="import-retry-button">
                Choose a different file
              </Button>
            </Stack>
          )}

          {showReviewStep && report && (
            <Stack gap="md" data-testid="import-review-step">
              <Group gap="xs">
                <Badge color="green" data-testid="import-valid-count">
                  {report.valid_count} valid
                </Badge>
                <Badge color={report.invalid_count > 0 ? "red" : "gray"} data-testid="import-invalid-count">
                  {report.invalid_count} invalid
                </Badge>
                <Badge variant="light">{report.total_rows} total rows</Badge>
              </Group>

              {importJob?.status === "commit_failed" && (
                <Alert color="red" title="Commit failed">
                  {importJob.commit_job?.error ||
                    "This import couldn't be committed — see the row errors below."}
                </Alert>
              )}

              <Stack gap={4}>
                <Text fw={600} size="sm">
                  Column mapping
                </Text>
                <Text size="xs" c="dimmed">
                  Adjust any column below, then re-check to re-run validation with the new
                  mapping before committing.
                </Text>
                {headers.map((header) => (
                  <Group key={header} gap="xs" wrap="nowrap">
                    <Text size="sm" style={{ flex: 1 }} truncate>
                      {header}
                    </Text>
                    <Select
                      style={{ flex: 1 }}
                      size="xs"
                      data={TARGET_OPTIONS}
                      value={mappingDraft[header] ?? resolvedMapping?.[header] ?? "custom"}
                      onChange={(v) => v && setMappingDraft((prev) => ({ ...prev, [header]: v }))}
                      allowDeselect={false}
                      data-testid={`import-mapping-select-${header}`}
                    />
                  </Group>
                ))}
                <Button
                  variant="light"
                  size="xs"
                  loading={submitting}
                  onClick={handleRecheckMapping}
                  data-testid="import-recheck-button"
                >
                  Re-check mapping
                </Button>
              </Stack>

              {invalidRows.length > 0 && (
                <Stack gap={4}>
                  <Text fw={600} size="sm">
                    Row errors
                  </Text>
                  <ScrollArea h={240} data-testid="import-error-table">
                    <Table striped withTableBorder>
                      <Table.Thead>
                        <Table.Tr>
                          <Table.Th>Row</Table.Th>
                          <Table.Th>Name</Table.Th>
                          <Table.Th>Errors</Table.Th>
                        </Table.Tr>
                      </Table.Thead>
                      <Table.Tbody>
                        {invalidRows.map((row) => (
                          <Table.Tr key={row.row_number}>
                            <Table.Td>{row.row_number}</Table.Td>
                            <Table.Td>{row.values.name || "—"}</Table.Td>
                            <Table.Td>{rowErrorSummary(row)}</Table.Td>
                          </Table.Tr>
                        ))}
                      </Table.Tbody>
                    </Table>
                  </ScrollArea>
                </Stack>
              )}

              {report.invalid_count > 0 && (
                <Alert color="yellow" title="Fix invalid rows before committing">
                  This import is all-or-nothing: {report.invalid_count} row(s) still have errors,
                  so nothing will be created until they're fixed (in the source file, or by
                  adjusting the column mapping above) and re-checked.
                </Alert>
              )}

              <Group grow>
                <Button variant="light" onClick={handleStartOver} data-testid="import-start-over-button">
                  Start over
                </Button>
                <Button
                  disabled={report.invalid_count > 0 || report.total_rows === 0}
                  loading={submitting}
                  onClick={handleCommit}
                  data-testid="import-commit-button"
                >
                  Commit ({report.valid_count} asset{report.valid_count === 1 ? "" : "s"})
                </Button>
              </Group>
            </Stack>
          )}

          {showSuccessStep && (
            <Stack align="center" gap="sm" py="xl" data-testid="import-success-step">
              <Text fw={600}>
                Import complete — {importJob?.created_asset_ids.length ?? 0} asset
                {(importJob?.created_asset_ids.length ?? 0) === 1 ? "" : "s"} created.
              </Text>
              <Button size="lg" onClick={() => navigate("/assets")} data-testid="import-view-assets-button">
                View assets
              </Button>
              <Button variant="light" onClick={handleStartOver} data-testid="import-another-button">
                Import another file
              </Button>
            </Stack>
          )}
        </Stack>
    </AppLayout>
  );
}
