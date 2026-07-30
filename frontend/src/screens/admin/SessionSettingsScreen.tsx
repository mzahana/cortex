import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Card, Center, Loader, NumberInput, Stack, Text } from "@mantine/core";
import { useForm } from "@mantine/form";
import { api, ApiError } from "../../api/client";
import { hasPermission, TENANT_MANAGE } from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import { AppLayout } from "../../layout/AppLayout";
import type { SessionSettings, SessionSettingsUpdate } from "../../api/types";

const IDLE_MIN = 5;
const IDLE_MAX = 480;
const ABSOLUTE_MIN = 1;
const ABSOLUTE_MAX = 168;

interface FormValues {
  idle_timeout_minutes: number;
  absolute_timeout_hours: number;
}

/**
 * Admin: Session Settings — lets a tenant admin configure the idle and
 * absolute session timeouts enforced server-side by
 * `SessionTimeoutMiddleware`, instead of a fixed 30-day cookie lifetime.
 * Singleton resource, gated server-side on `tenant.manage` for BOTH read
 * and write (`apps.tenancy.api.SessionSettingsView`) — same "nothing to
 * show a non-admin" reasoning as `EmailSettingsScreen`, so the whole screen
 * is gated rather than showing a partial read-only view.
 */
export function SessionSettingsScreen() {
  const { me } = useAuth();
  const canManage = hasPermission(me, TENANT_MANAGE);

  const [settings, setSettings] = useState<SessionSettings | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const form = useForm<FormValues>({
    initialValues: {
      idle_timeout_minutes: 60,
      absolute_timeout_hours: 24,
    },
    validate: {
      idle_timeout_minutes: (value) =>
        value >= IDLE_MIN && value <= IDLE_MAX
          ? null
          : `Must be between ${IDLE_MIN} and ${IDLE_MAX} minutes`,
      absolute_timeout_hours: (value) =>
        value >= ABSOLUTE_MIN && value <= ABSOLUTE_MAX
          ? null
          : `Must be between ${ABSOLUTE_MIN} and ${ABSOLUTE_MAX} hours`,
    },
  });

  const load = useCallback(async () => {
    if (!canManage) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setLoadError(null);
    try {
      const body = await api.getSessionSettings();
      setSettings(body);
      form.setValues({
        idle_timeout_minutes: body.idle_timeout_minutes,
        absolute_timeout_hours: body.absolute_timeout_hours,
      });
    } catch (err) {
      setLoadError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canManage]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!canManage) {
    return (
      <AppLayout title="Session Settings">
        <Alert color="red" title="Not authorized" data-testid="session-settings-forbidden">
          You don&apos;t have permission to view or change session timeout settings. This
          requires the tenant admin role.
        </Alert>
      </AppLayout>
    );
  }

  const handleSubmit = async (values: FormValues) => {
    setFormError(null);
    setSaved(false);
    setSubmitting(true);
    try {
      const payload: SessionSettingsUpdate = {
        idle_timeout_minutes: values.idle_timeout_minutes,
        absolute_timeout_hours: values.absolute_timeout_hours,
      };
      const updated = await api.updateSessionSettings(payload);
      setSettings(updated);
      form.setValues({
        idle_timeout_minutes: updated.idle_timeout_minutes,
        absolute_timeout_hours: updated.absolute_timeout_hours,
      });
      setSaved(true);
    } catch (err) {
      if (err instanceof ApiError && err.problem.errors) {
        const { non_field_errors: nonField, ...fieldErrors } = err.problem.errors;
        if (Object.keys(fieldErrors).length > 0) {
          form.setErrors(
            Object.fromEntries(
              Object.entries(fieldErrors).map(([k, v]) => [
                k,
                Array.isArray(v) ? v.join(" ") : String(v),
              ]),
            ),
          );
        }
        if (nonField) {
          setFormError(Array.isArray(nonField) ? nonField.join(" ") : String(nonField));
        }
        if (!nonField && Object.keys(fieldErrors).length === 0) {
          setFormError(err.problem.detail ?? err.problem.title);
        }
      } else if (err instanceof ApiError) {
        setFormError(err.problem.detail ?? err.problem.title);
      } else {
        setFormError("Unable to reach the server. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AppLayout title="Session Settings">
      <Stack gap="md" maw={520}>
        <Text c="dimmed" size="sm">
          Configure how long a signed-in session stays valid for this tenant. Both limits
          are enforced server-side; whichever is reached first signs the user out.
        </Text>

        {loadError && (
          <Alert color="red" data-testid="session-settings-load-error">
            <Stack gap="xs" align="flex-start">
              <Text size="sm">{loadError}</Text>
              <Button size="xs" variant="light" onClick={() => void load()}>
                Retry
              </Button>
            </Stack>
          </Alert>
        )}

        {loading && !loadError && (
          <Center p="xl">
            <Loader data-testid="session-settings-loading" />
          </Center>
        )}

        {!loading && !loadError && settings && (
          <Card withBorder padding="md" radius="md">
            <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
              <Stack gap="sm">
                {formError && (
                  <Alert color="red" data-testid="session-settings-form-error">
                    {formError}
                  </Alert>
                )}
                {saved && (
                  <Alert
                    color="teal"
                    withCloseButton
                    onClose={() => setSaved(false)}
                    data-testid="session-settings-saved"
                  >
                    Session settings saved.
                  </Alert>
                )}

                <NumberInput
                  label="Idle timeout (minutes)"
                  description="e.g. 60 = 1 hour. Signs a user out after this much inactivity."
                  min={IDLE_MIN}
                  max={IDLE_MAX}
                  clampBehavior="none"
                  allowDecimal={false}
                  data-testid="session-settings-idle-minutes"
                  {...form.getInputProps("idle_timeout_minutes")}
                />

                <NumberInput
                  label="Absolute timeout (hours)"
                  description="e.g. 24 = 1 day, 168 = 7 days. Hard cap regardless of activity."
                  min={ABSOLUTE_MIN}
                  max={ABSOLUTE_MAX}
                  clampBehavior="none"
                  allowDecimal={false}
                  data-testid="session-settings-absolute-hours"
                  {...form.getInputProps("absolute_timeout_hours")}
                />

                <Button
                  type="submit"
                  loading={submitting}
                  fullWidth
                  mt="sm"
                  data-testid="session-settings-submit"
                >
                  Save changes
                </Button>
              </Stack>
            </form>
          </Card>
        )}
      </Stack>
    </AppLayout>
  );
}
