import { useState } from "react";
import { Alert, Badge, Button, Group, Stack, Text } from "@mantine/core";
import { api, ApiError } from "../../api/client";
import type { Asset, Checkout } from "../../api/types";

function formatDueDate(dueAt: string): string {
  const due = new Date(dueAt);
  return due.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

interface CheckoutRowProps {
  checkout: Checkout;
  asset: Asset | undefined;
  onCheckedIn: (updated: Checkout) => void;
}

/**
 * One open-checkout row on the My Items screen (T3.5). `is_overdue` is
 * trusted verbatim from the API (CLAUDE.md task note: never recompute the
 * overdue logic client-side) and drives the visual highlight. "Check in" is
 * a quick, one-tap self-service action — the server enforces holder-only
 * (`apps.reservations.checkout.CheckoutPermission`); a 403 here is a normal,
 * handled outcome, surfaced inline rather than assumed away.
 */
export function CheckoutRow({ checkout, asset, onCheckedIn }: CheckoutRowProps) {
  const [checkingIn, setCheckingIn] = useState(false);
  const [rowError, setRowError] = useState<string | null>(null);

  const handleCheckIn = async () => {
    setCheckingIn(true);
    setRowError(null);
    try {
      const updated = await api.checkinCheckout(checkout.id);
      onCheckedIn(updated);
    } catch (err) {
      setRowError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setCheckingIn(false);
    }
  };

  return (
    <Stack
      gap={4}
      p="sm"
      data-testid={`checkout-row-${checkout.id}`}
      style={{
        border: `1px solid ${
          checkout.is_overdue ? "var(--mantine-color-red-6)" : "var(--mantine-color-default-border)"
        }`,
        borderRadius: 8,
      }}
      bg={checkout.is_overdue ? "var(--mantine-color-red-light)" : undefined}
    >
      <Group justify="space-between" wrap="nowrap">
        <Text fw={600} size="sm" truncate>
          {asset?.name ?? `Asset #${checkout.asset}`}
        </Text>
        {checkout.is_overdue && (
          <Badge color="red" size="sm" data-testid={`overdue-${checkout.id}`}>
            Overdue
          </Badge>
        )}
      </Group>
      <Text size="xs" c={checkout.is_overdue ? "red" : "dimmed"}>
        Due {formatDueDate(checkout.due_at)}
      </Text>
      {asset?.serial_number && (
        <Text size="xs" c="dimmed">
          S/N {asset.serial_number}
        </Text>
      )}

      {rowError && (
        <Alert color="red" py={4} px="xs">
          <Text size="xs">{rowError}</Text>
        </Alert>
      )}

      <Group gap="xs" mt={4}>
        <Button
          size="xs"
          loading={checkingIn}
          onClick={() => void handleCheckIn()}
          data-testid={`checkin-${checkout.id}`}
        >
          Check in
        </Button>
      </Group>
    </Stack>
  );
}
