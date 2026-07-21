import { useCallback, useMemo, useState } from "react";
import {
  ActionIcon,
  Alert,
  AppShell,
  Button,
  Group,
  Loader,
  Select,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useNavigate } from "react-router-dom";
import type { Asset, LabelSheetTemplate } from "../../api/types";
import { AssetPicker } from "./AssetPicker";
import { useLabelJob } from "./useLabelJob";

const TEMPLATE_OPTIONS: { value: LabelSheetTemplate; label: string }[] = [
  { value: "avery_5160", label: 'Avery 5160 — 1" x 2⅝", 30/sheet' },
  { value: "avery_5163", label: 'Avery 5163 — 2" x 4", 10/sheet' },
];

/**
 * Label PDF generation screen (T4.5, F7: "select N assets -> print-ready
 * Avery PDF"). Select assets -> pick a sheet template -> Generate -> poll
 * `GET /api/v1/jobs/{id}` (`useLabelJob`) -> Download link once succeeded.
 * `label.generate` is enforced server-side (Admin tenant-wide, ProjectLead
 * scoped to their own project's assets, `docs/rbac.md` §3); this screen does
 * no client-side gating of its own beyond the normal "a 403 is a handled
 * outcome, not a crash" posture (CLAUDE.md) — an unauthorized submit surfaces
 * the server's error via `useLabelJob`'s `error` state.
 *
 * Every printed QR encodes the asset's bare `qr_token` (`apps.labels.
 * rendering` module docstring) — scanning it (T4.3) calls `GET /api/v1/
 * resolve/{qr_token}` and opens the exact same asset (F6/F7 closing the
 * loop, T4.6).
 */
export function LabelsScreen() {
  const navigate = useNavigate();
  const [selected, setSelected] = useState<Map<number, Asset>>(new Map());
  const [template, setTemplate] = useState<LabelSheetTemplate>("avery_5160");
  const { job, submitting, error, generate, reset } = useLabelJob();

  const selectedIds = useMemo(() => new Set(selected.keys()), [selected]);

  const toggleAsset = useCallback((asset: Asset) => {
    setSelected((prev) => {
      const next = new Map(prev);
      if (next.has(asset.id)) {
        next.delete(asset.id);
      } else {
        next.set(asset.id, asset);
      }
      return next;
    });
  }, []);

  const clearSelection = useCallback(() => setSelected(new Map()), []);

  const handleGenerate = () => {
    void generate(Array.from(selectedIds), template);
  };

  const handleStartOver = () => {
    reset();
    clearSelection();
  };

  const isPolling = job !== null && (job.status === "queued" || job.status === "running");
  const isDone = job !== null && (job.status === "succeeded" || job.status === "failed");

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="xs">
            <ActionIcon variant="subtle" aria-label="Back" onClick={() => navigate("/")}>
              &#8592;
            </ActionIcon>
            <Title order={4}>Print Labels</Title>
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Stack gap="md" data-testid="labels-screen">
          {!isPolling && !isDone && (
            <>
              <AssetPicker
                selectedIds={selectedIds}
                onToggle={toggleAsset}
                onClearSelection={clearSelection}
              />

              <Select
                label="Sheet template"
                data={TEMPLATE_OPTIONS}
                value={template}
                onChange={(v) => v && setTemplate(v as LabelSheetTemplate)}
                allowDeselect={false}
                data-testid="label-template-select"
              />

              {error && (
                <Alert color="red" title="Couldn't generate labels">
                  {error}
                </Alert>
              )}

              <Button
                size="lg"
                fullWidth
                disabled={selectedIds.size === 0}
                loading={submitting}
                onClick={handleGenerate}
                data-testid="label-generate-button"
              >
                Generate ({selectedIds.size} asset{selectedIds.size === 1 ? "" : "s"})
              </Button>
            </>
          )}

          {isPolling && (
            <Stack align="center" gap="sm" py="xl" data-testid="label-job-polling">
              <Loader />
              <Text c="dimmed">
                {job?.status === "running" ? "Rendering your label sheet…" : "Queued…"}
              </Text>
            </Stack>
          )}

          {isDone && job?.status === "succeeded" && (
            <Stack align="center" gap="sm" py="xl" data-testid="label-job-succeeded">
              <Text fw={600}>Your label sheet is ready.</Text>
              <Button
                component="a"
                href={job.download_url ?? undefined}
                download={job.result_filename || undefined}
                size="lg"
                data-testid="label-download-link"
              >
                Download PDF
              </Button>
              <Button variant="light" onClick={handleStartOver}>
                Print more labels
              </Button>
            </Stack>
          )}

          {isDone && job?.status === "failed" && (
            <Stack gap="sm" py="xl" data-testid="label-job-failed">
              <Alert color="red" title="Label generation failed">
                {job.error || "Something went wrong while rendering the PDF."}
              </Alert>
              <Button variant="light" onClick={handleStartOver}>
                Try again
              </Button>
            </Stack>
          )}
        </Stack>
      </AppShell.Main>
    </AppShell>
  );
}
