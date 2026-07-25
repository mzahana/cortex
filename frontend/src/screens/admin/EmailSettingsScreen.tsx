import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Anchor,
  Button,
  Card,
  Center,
  Loader,
  Select,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { api, ApiError } from "../../api/client";
import { hasPermission, TENANT_MANAGE } from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import { AppLayout } from "../../layout/AppLayout";
import type { EmailSettings, EmailSettingsUpdate } from "../../api/types";

const EMAIL_LIKE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

interface FormValues {
  provider: "console" | "brevo";
  sender_email: string;
  reply_to: string;
  api_key: string;
  clear_api_key: boolean;
}

/**
 * Admin: Email Settings — lets a tenant admin configure email delivery
 * (Brevo API key, sender, reply-to) from the UI instead of only via env
 * vars. Singleton resource, gated server-side on `tenant.manage` for BOTH
 * read and write (`apps.notifications.api.EmailSettingsView`) — unlike
 * `ProjectsScreen`, which shows a read-only list to anyone, a non-admin
 * gets a 403 on the GET itself here, so the whole screen is gated (same
 * "nothing to show a non-admin" reasoning, no partial read-only view).
 */
export function EmailSettingsScreen() {
  const { me } = useAuth();
  const canManage = hasPermission(me, TENANT_MANAGE);

  const [settings, setSettings] = useState<EmailSettings | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const form = useForm<FormValues>({
    initialValues: {
      provider: "console",
      sender_email: "",
      reply_to: "",
      api_key: "",
      clear_api_key: false,
    },
    validate: {
      sender_email: (value) =>
        !value.trim() || EMAIL_LIKE.test(value.trim()) ? null : "Enter a valid email address",
      reply_to: (value) =>
        !value.trim() || EMAIL_LIKE.test(value.trim()) ? null : "Enter a valid email address",
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
      const body = await api.getEmailSettings();
      setSettings(body);
      form.setValues({
        provider: body.provider,
        sender_email: body.sender_email,
        reply_to: body.reply_to,
        api_key: "",
        clear_api_key: false,
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
      <AppLayout title="Email Settings">
        <Alert color="red" title="Not authorized" data-testid="email-settings-forbidden">
          You don&apos;t have permission to view or change email delivery settings. This
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
      const payload: EmailSettingsUpdate = {
        provider: values.provider,
        sender_email: values.sender_email.trim(),
        reply_to: values.reply_to.trim(),
      };
      // Omit `api_key` entirely unless the user typed a new one or
      // explicitly asked to clear it — omitting leaves the stored key
      // untouched server-side (see `EmailSettingsUpdate` doc comment).
      if (values.clear_api_key) {
        payload.api_key = "";
      } else if (values.api_key.trim()) {
        payload.api_key = values.api_key.trim();
      }

      const updated = await api.updateEmailSettings(payload);
      setSettings(updated);
      form.setValues({
        provider: updated.provider,
        sender_email: updated.sender_email,
        reply_to: updated.reply_to,
        api_key: "",
        clear_api_key: false,
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
    <AppLayout title="Email Settings">
      <Stack gap="md" maw={520}>
        <Text c="dimmed" size="sm">
          Configure how Cortex sends email (reservation confirmations, approval
          requests, overdue reminders, low-stock alerts) for this tenant.
        </Text>

        {loadError && (
          <Alert color="red" data-testid="email-settings-load-error">
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
            <Loader data-testid="email-settings-loading" />
          </Center>
        )}

        {!loading && !loadError && settings && (
          <Card withBorder padding="md" radius="md">
            <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
              <Stack gap="sm">
                {formError && (
                  <Alert color="red" data-testid="email-settings-form-error">
                    {formError}
                  </Alert>
                )}
                {saved && (
                  <Alert
                    color="teal"
                    withCloseButton
                    onClose={() => setSaved(false)}
                    data-testid="email-settings-saved"
                  >
                    Email settings saved.
                  </Alert>
                )}

                <Select
                  label="Provider"
                  data={[
                    { value: "console", label: "Console (dev/test — logs emails, sends nothing)" },
                    { value: "brevo", label: "Brevo" },
                  ]}
                  allowDeselect={false}
                  {...form.getInputProps("provider")}
                />

                <TextInput
                  label="Sender email"
                  placeholder="notifications@yourlab.org"
                  description="The From address for outgoing emails"
                  {...form.getInputProps("sender_email")}
                />

                <TextInput
                  label="Reply-to"
                  placeholder="labmanager@yourlab.org"
                  description="Optional — where replies should go"
                  {...form.getInputProps("reply_to")}
                />

                <TextInput
                  label="Brevo API key"
                  type="password"
                  placeholder={settings.has_api_key ? "Leave blank to keep the current key" : "Enter a Brevo API key"}
                  description={
                    settings.has_api_key
                      ? `Current key ends in ••••${settings.api_key_last4}. Typing here sets a new key.`
                      : "No API key stored yet."
                  }
                  disabled={form.values.clear_api_key}
                  {...form.getInputProps("api_key")}
                />

                {settings.has_api_key && (
                  <Anchor
                    size="xs"
                    c={form.values.clear_api_key ? "red" : "dimmed"}
                    onClick={() => {
                      form.setFieldValue("clear_api_key", !form.values.clear_api_key);
                      if (!form.values.clear_api_key) form.setFieldValue("api_key", "");
                    }}
                    data-testid="email-settings-clear-key-toggle"
                    component="button"
                    type="button"
                  >
                    {form.values.clear_api_key
                      ? "Cancel — keep the stored key"
                      : "Clear stored key"}
                  </Anchor>
                )}

                <Button
                  type="submit"
                  loading={submitting}
                  fullWidth
                  mt="sm"
                  data-testid="email-settings-submit"
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
