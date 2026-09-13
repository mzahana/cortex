import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Radio,
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
import { api } from "../../api/client";
import {
  IMPORT_CORE_TARGETS,
  IMPORT_CREATABLE_TARGETS,
  type ImportCreatableTarget,
  type ImportMapping,
  type ImportOnDuplicate,
  type ImportReportRow,
} from "../../api/types";
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
  // Which kinds of missing reference the user has agreed to have created.
  // Sent on both the re-validate and the commit, so the dry-run report the
  // user is looking at is the one the commit reproduces.
  const [createMissing, setCreateMissing] = useState<ImportCreatableTarget[]>([]);
  // Server default too (`apps.imports.services.DEFAULT_ON_DUPLICATE`) — a
  // duplicate blocks the commit until the user says otherwise.
  const [onDuplicate, setOnDuplicate] = useState<ImportOnDuplicate>("reject");

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
    void upload(file, undefined, createMissing, onDuplicate);
  };

  const handleRecheckMapping = () => {
    if (!file) return;
    void upload(file, mappingDraft, createMissing, onDuplicate);
  };

  const handleCommit = () => {
    void commit(mappingDraft, createMissing, onDuplicate);
  };

  const handleStartOver = () => {
    reset();
    setFile(null);
    setMappingDraft({});
    setCreateMissing([]);
    setOnDuplicate("reject");
  };

  const toggleCreateMissing = (target: ImportCreatableTarget, checked: boolean) =>
    setCreateMissing((prev) =>
      checked ? [...prev, target] : prev.filter((entry) => entry !== target),
    );

  const report = importJob?.report ?? null;
  // Only offer to create a kind of reference the file actually names and
  // this tenant actually lacks — an empty list means there's nothing to ask.
  const missingRefs = useMemo(
    () =>
      IMPORT_CREATABLE_TARGETS.map((target) => ({
        target,
        names: report?.missing_references?.[target] ?? [],
      })).filter((entry) => entry.names.length > 0),
    [report],
  );
  const duplicateRows = report?.duplicate_rows ?? [];
  const createdRefs = useMemo(
    () =>
      IMPORT_CREATABLE_TARGETS.map((target) => ({
        target,
        names: report?.created_references?.[target] ?? [],
      })).filter((entry) => entry.names.length > 0),
    [report],
  );
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
                mapping before anything is created. Importing only <b>adds</b>{" "}
                assets; it never edits or removes assets already in Cortex.
              </Text>
              <Button
                component="a"
                href={api.importTemplateXlsxUrl()}
                variant="light"
                fullWidth
                data-testid="import-template-download"
              >
                Download blank Excel template
              </Button>
              <Text size="xs" c="dimmed">
                The template already has the right column headers, your
                categories, locations and projects listed on an Instructions
                sheet, and a column for every custom field you&apos;ve defined.
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

              {duplicateRows.length > 0 && (
                <Stack gap={6} data-testid="import-duplicates">
                  <Alert color="orange" title={`${duplicateRows.length} possible duplicate row(s)`}>
                    These rows have the same name and category as something
                    already in Cortex (or as an earlier row in this file).
                    Nothing is created until you choose what to do with them.
                  </Alert>
                  <ScrollArea.Autosize mah={180} data-testid="import-duplicate-table">
                    <Table striped withTableBorder>
                      <Table.Thead>
                        <Table.Tr>
                          <Table.Th>Row</Table.Th>
                          <Table.Th>Name</Table.Th>
                          <Table.Th>Category</Table.Th>
                          <Table.Th>Already exists as</Table.Th>
                        </Table.Tr>
                      </Table.Thead>
                      <Table.Tbody>
                        {duplicateRows.map((row) => (
                          <Table.Tr key={row.row_number}>
                            <Table.Td>{row.row_number}</Table.Td>
                            <Table.Td>{row.name || "—"}</Table.Td>
                            <Table.Td>{row.category || "—"}</Table.Td>
                            <Table.Td>
                              {row.duplicate_of_row !== null
                                ? `row ${row.duplicate_of_row} of this file`
                                : `${row.existing_asset_ids.length} existing asset(s)`}
                            </Table.Td>
                          </Table.Tr>
                        ))}
                      </Table.Tbody>
                    </Table>
                  </ScrollArea.Autosize>
                  <Radio.Group
                    value={onDuplicate}
                    onChange={(value) => setOnDuplicate(value as ImportOnDuplicate)}
                    data-testid="import-on-duplicate"
                  >
                    <Stack gap={4} mt={4}>
                      <Radio
                        value="reject"
                        label="Don't import anything until I fix them (default)"
                      />
                      <Radio
                        value="skip"
                        label={`Skip the ${duplicateRows.length} duplicate row(s), import the rest`}
                      />
                      <Radio
                        value="create"
                        label="Import them anyway — these really are separate units"
                      />
                    </Stack>
                  </Radio.Group>
                  <Button
                    variant="light"
                    size="xs"
                    loading={submitting}
                    onClick={handleRecheckMapping}
                    disabled={!file}
                    data-testid="import-recheck-duplicates-button"
                  >
                    Re-check with these settings
                  </Button>
                </Stack>
              )}

              {missingRefs.length > 0 && (
                <Stack gap={6} data-testid="import-missing-refs">
                  <Text fw={600} size="sm">
                    Not in Cortex yet
                  </Text>
                  <Text size="xs" c="dimmed">
                    These names appear in your sheet but don&apos;t exist here. Tick a box to
                    create them when you commit, then re-check. Nothing is created until you
                    press Commit, and existing records are never changed.
                  </Text>
                  {missingRefs.map(({ target, names }) => (
                    <Checkbox
                      key={target}
                      checked={createMissing.includes(target)}
                      onChange={(event) =>
                        toggleCreateMissing(target, event.currentTarget.checked)
                      }
                      data-testid={`import-create-missing-${target}`}
                      label={
                        <Text size="sm">
                          Create {names.length} new {target}
                          {names.length === 1 ? "" : "s"}:{" "}
                          <Text span c="dimmed">
                            {names.join(", ")}
                          </Text>
                        </Text>
                      }
                    />
                  ))}
                  <Button
                    variant="light"
                    size="xs"
                    loading={submitting}
                    onClick={handleRecheckMapping}
                    disabled={!file}
                    data-testid="import-recheck-create-missing-button"
                  >
                    Re-check with these settings
                  </Button>
                </Stack>
              )}

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
                  so nothing will be created until they're fixed (in the source file, by
                  adjusting the column mapping above, or by choosing what to do with the
                  duplicates and missing names above) and re-checked.
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
              {(report?.skipped_count ?? 0) > 0 && (
                <Text size="sm" c="dimmed" ta="center" data-testid="import-skipped-count">
                  {report?.skipped_count} duplicate row(s) skipped.
                </Text>
              )}
              {createdRefs.length > 0 && (
                <Text size="sm" c="dimmed" ta="center" data-testid="import-created-refs">
                  Also created:{" "}
                  {createdRefs
                    .map(({ target, names }) => `${names.length} ${target}(s) — ${names.join(", ")}`)
                    .join("; ")}
                </Text>
              )}
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
