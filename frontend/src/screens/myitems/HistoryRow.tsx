import { Group, Stack, Text } from "@mantine/core";
import type { Asset, Checkout } from "../../api/types";

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

interface HistoryRowProps {
  checkout: Checkout;
  asset: Asset | undefined;
}

/**
 * One past (already checked-in) checkout row on the My Items "History" tab.
 * Read-only — unlike `CheckoutRow` there is no check-in action, since
 * `checked_in_at` is already set for every row this component renders.
 */
export function HistoryRow({ checkout, asset }: HistoryRowProps) {
  return (
    <Stack
      gap={4}
      p="sm"
      data-testid={`history-row-${checkout.id}`}
      style={{
        border: "1px solid var(--mantine-color-default-border)",
        borderRadius: 8,
      }}
    >
      <Group justify="space-between" wrap="nowrap">
        <Text fw={600} size="sm" truncate>
          {asset?.name ?? `Asset #${checkout.asset}`}
        </Text>
      </Group>
      <Text size="xs" c="dimmed">
        Checked out {formatDateTime(checkout.checked_out_at)}
      </Text>
      <Text size="xs" c="dimmed">
        Returned {checkout.checked_in_at ? formatDateTime(checkout.checked_in_at) : "—"}
      </Text>
      {asset?.serial_number && (
        <Text size="xs" c="dimmed">
          S/N {asset.serial_number}
        </Text>
      )}
      {checkout.checkin_condition && (
        <Text size="xs" c="dimmed">
          Condition on return: {checkout.checkin_condition}
        </Text>
      )}
    </Stack>
  );
}
