import { Button, Menu, Tooltip } from "@mantine/core";
import { IconChevronDown, IconPrinter } from "@tabler/icons-react";
import type { LabelSheetTemplate } from "../../api/types";
import { useLabelJob } from "../../hooks/useLabelJob";

const TEMPLATE_OPTIONS: { value: LabelSheetTemplate; label: string }[] = [
  { value: "single", label: 'Single label (2" x 4")' },
  { value: "avery_5160", label: 'Avery 5160 sheet — 1" x 2⅝"' },
  { value: "avery_5163", label: 'Avery 5163 sheet — 2" x 4"' },
];

/**
 * Asset Detail "Print label" action — the one-asset shortcut past the Labels
 * screen (which stays the place to batch-print a selection). Same contract as
 * `LabelsScreen`: `POST /api/v1/labels/generate` with this single asset id,
 * poll the `Job` (`useLabelJob`, shared), then hand over a download link.
 *
 * Defaults to the `single` template (`apps.labels.templates.SINGLE_LABEL`,
 * one label per page) rather than an Avery sheet — printing one asset onto a
 * 30-up sheet wastes the other 29 die-cut labels. The sheet templates are
 * still offered from the dropdown for the "I have the sheet loaded" case.
 *
 * Gating is presentation-only (CLAUDE.md): the caller passes `disabled` when
 * the user lacks `label.generate` **for this asset's project**, and the
 * server re-checks per-asset regardless (`apps.labels.api.LabelGenerateView`)
 * — a 403 surfaces as this component's error state, not a crash.
 */
export function PrintLabelButton({ assetId, disabled }: { assetId: number; disabled?: boolean }) {
  const { job, submitting, error, generate, reset } = useLabelJob();

  const isPolling = job !== null && (job.status === "queued" || job.status === "running");
  const busy = submitting || isPolling;

  if (disabled) {
    return (
      <Tooltip label="You don't have permission to print labels for this asset">
        <Button size="sm" variant="default" leftSection={<IconPrinter size={16} />} disabled>
          Print label
        </Button>
      </Tooltip>
    );
  }

  if (job?.status === "succeeded") {
    return (
      <Button
        component="a"
        size="sm"
        variant="filled"
        href={job.download_url ?? undefined}
        download={job.result_filename || undefined}
        leftSection={<IconPrinter size={16} />}
        onClick={() => {
          // One-shot: once the browser has the PDF, drop back to the plain
          // action so a second print starts a fresh job rather than
          // re-downloading a stale one.
          window.setTimeout(reset, 0);
        }}
        data-testid="asset-label-download"
      >
        Download label PDF
      </Button>
    );
  }

  if (job?.status === "failed" || error) {
    return (
      <Tooltip label={job?.error || error || "Label generation failed."}>
        <Button
          size="sm"
          variant="default"
          color="red"
          leftSection={<IconPrinter size={16} />}
          onClick={() => {
            reset();
            void generate([assetId], "single");
          }}
          data-testid="asset-label-retry"
        >
          Retry label
        </Button>
      </Tooltip>
    );
  }

  return (
    <Button.Group>
      <Button
        size="sm"
        variant="default"
        loading={busy}
        leftSection={<IconPrinter size={16} />}
        onClick={() => void generate([assetId], "single")}
        data-testid="asset-print-label"
      >
        Print label
      </Button>
      <Menu position="bottom-end" withinPortal>
        <Menu.Target>
          <Button
            size="sm"
            variant="default"
            px="xs"
            disabled={busy}
            aria-label="Choose label format"
            data-testid="asset-print-label-menu"
          >
            <IconChevronDown size={16} />
          </Button>
        </Menu.Target>
        <Menu.Dropdown>
          <Menu.Label>Label format</Menu.Label>
          {TEMPLATE_OPTIONS.map((option) => (
            <Menu.Item key={option.value} onClick={() => void generate([assetId], option.value)}>
              {option.label}
            </Menu.Item>
          ))}
        </Menu.Dropdown>
      </Menu>
    </Button.Group>
  );
}
