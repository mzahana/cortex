import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActionIcon,
  Alert,
  AppShell,
  Button,
  Card,
  Center,
  Group,
  Loader,
  Stack,
  Switch,
  Text,
  Title,
} from "@mantine/core";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import { NOTIFICATION_EVENT_TYPES, type NotificationPref } from "../../api/types";

/** Human-readable labels for the known `event_type` keys
 * (`apps.notifications.receivers.EVENT_*`, see `NOTIFICATION_EVENT_TYPES`
 * doc comment). Falls back to the raw key for anything unrecognized (a
 * future event type the frontend hasn't been updated for yet still renders,
 * just without a friendly label). */
const EVENT_LABELS: Record<string, string> = {
  reservation_confirmed: "Reservation confirmed",
  approval_request: "Approval requested (needs your decision)",
  approval_decision: "Your reservation was approved/rejected",
  overdue_reminder: "Overdue checkout reminder",
  low_stock_alert: "Low stock alert",
};

/**
 * My Notifications screen (T5.6, `docs/api-and-ui.md`: "Per-event email
 * preferences"). Gated by `notify.self`, which every role holds
 * (`docs/rbac.md` §3) — so this screen is reachable by anyone signed in;
 * there is no client-side gate to apply beyond the nav link itself.
 *
 * Shows one row per KNOWN event type (`NOTIFICATION_EVENT_TYPES`), not just
 * the rows the server already has — a user with no explicit `NotificationPref`
 * row yet for an event type defaults to enabled (`apps.notifications.api`
 * module docstring: the first `PATCH` for a never-before-seen event type
 * upserts). Existing rows (`GET /notification-prefs/`) are merged in by
 * `event_type` to reflect the user's actual saved preference where one
 * exists.
 */
export function NotificationsScreen() {
  const navigate = useNavigate();
  const [prefsByEvent, setPrefsByEvent] = useState<Map<string, NotificationPref>>(new Map());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [savingEvent, setSavingEvent] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // Bounded, tenant-config-like list (a handful of event types) — a
      // single page covers every row today; walking further pages would
      // never be needed at this size, but if it ever grew this would need
      // `fetchAllPages`-style pagination like the catalog screens.
      const body = await api.listNotificationPrefs();
      setPrefsByEvent(new Map(body.results.map((pref) => [pref.event_type, pref])));
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const rows = useMemo(
    () =>
      NOTIFICATION_EVENT_TYPES.map((eventType) => ({
        eventType,
        pref: prefsByEvent.get(eventType) ?? null,
        // No explicit row yet -> defaults to enabled (opt-out semantics).
        emailEnabled: prefsByEvent.get(eventType)?.email_enabled ?? true,
      })),
    [prefsByEvent],
  );

  const handleToggle = async (eventType: string, nextValue: boolean) => {
    setSavingEvent(eventType);
    setSaveError(null);
    try {
      const updated = await api.updateNotificationPref(eventType, { email_enabled: nextValue });
      setPrefsByEvent((prev) => {
        const next = new Map(prev);
        next.set(eventType, updated);
        return next;
      });
    } catch (err) {
      setSaveError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setSavingEvent(null);
    }
  };

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md">
          <ActionIcon variant="subtle" aria-label="Back" onClick={() => navigate("/")}>
            &#8592;
          </ActionIcon>
          <Title order={4}>My Notifications</Title>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Stack gap="md">
          <Text c="dimmed" size="sm">
            Choose which events send you an email. These are your own
            preferences only.
          </Text>

          {saveError && (
            <Alert color="red" withCloseButton onClose={() => setSaveError(null)} title="Couldn't save">
              {saveError}
            </Alert>
          )}

          {error && (
            <Alert color="red" title="Couldn't load your preferences">
              <Stack gap="xs" align="flex-start">
                <Text size="sm">{error}</Text>
                <Button size="xs" variant="light" onClick={load}>
                  Retry
                </Button>
              </Stack>
            </Alert>
          )}

          {loading && !error && (
            <Center p="xl">
              <Loader data-testid="notification-prefs-loading" />
            </Center>
          )}

          {!loading && !error && (
            <Stack gap="sm" data-testid="notification-prefs-list">
              {rows.map(({ eventType, emailEnabled }) => (
                <Card key={eventType} withBorder padding="md" radius="md">
                  <Group justify="space-between" wrap="nowrap">
                    <Stack gap={0}>
                      <Text fw={500}>{EVENT_LABELS[eventType] ?? eventType}</Text>
                      <Text size="xs" c="dimmed">
                        {eventType}
                      </Text>
                    </Stack>
                    <Switch
                      checked={emailEnabled}
                      disabled={savingEvent === eventType}
                      onChange={(e) => void handleToggle(eventType, e.currentTarget.checked)}
                      data-testid={`notification-toggle-${eventType}`}
                      aria-label={`Email me for ${EVENT_LABELS[eventType] ?? eventType}`}
                    />
                  </Group>
                </Card>
              ))}
            </Stack>
          )}
        </Stack>
      </AppShell.Main>
    </AppShell>
  );
}
