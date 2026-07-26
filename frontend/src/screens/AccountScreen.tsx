import { useState } from "react";
import { Alert, Button, Paper, PasswordInput, Stack, Text } from "@mantine/core";
import { useForm } from "@mantine/form";
import { api, ApiError } from "../api/client";
import { useAuth } from "../hooks/useAuth";
import { AppLayout } from "../layout/AppLayout";
import { fieldErrorsFromProblem } from "../api/problem";

interface ChangePasswordValues {
  current_password: string;
  new_password: string;
  confirm_password: string;
}

/**
 * Account screen — a signed-in user changes their OWN password
 * (`POST /api/v1/me/password`). The server re-verifies the current password
 * and refreshes the session auth hash so the user stays logged in.
 * Server-side `AUTH_PASSWORD_VALIDATORS` failures come back as RFC-7807
 * `errors.new_password` and are surfaced as a field error; a wrong current
 * password comes back as `errors.current_password`.
 */
export function AccountScreen() {
  const { me } = useAuth();
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const form = useForm<ChangePasswordValues>({
    initialValues: { current_password: "", new_password: "", confirm_password: "" },
    validate: {
      current_password: (v) => (v ? null : "Enter your current password"),
      new_password: (v) => (v ? null : "Enter a new password"),
      confirm_password: (v, values) =>
        v === values.new_password ? null : "Passwords do not match",
    },
  });

  const handleSubmit = async (values: ChangePasswordValues) => {
    setSubmitError(null);
    setSuccess(false);
    setSubmitting(true);
    try {
      await api.changePassword({
        current_password: values.current_password,
        new_password: values.new_password,
      });
      form.reset();
      setSuccess(true);
    } catch (err) {
      if (err instanceof ApiError) {
        const fieldErrors = fieldErrorsFromProblem(err.problem);
        if (fieldErrors) {
          form.setErrors(fieldErrors);
        } else {
          setSubmitError(err.problem.detail ?? err.problem.title);
        }
      } else {
        setSubmitError("Unable to reach the server. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AppLayout title="Account">
      <Stack gap="md" maw={480}>
        <Text size="sm" c="dimmed">
          Signed in as {me?.email}
        </Text>

        <Paper withBorder p="lg" radius="md">
          <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
            <Stack>
              {success && (
                <Alert color="teal" title="Password changed" data-testid="change-password-success">
                  Your password has been updated.
                </Alert>
              )}
              {submitError && (
                <Alert color="red" title="Couldn't change password" data-testid="change-password-error">
                  {submitError}
                </Alert>
              )}
              <PasswordInput
                label="Current password"
                autoComplete="current-password"
                required
                data-testid="current-password"
                {...form.getInputProps("current_password")}
              />
              <PasswordInput
                label="New password"
                autoComplete="new-password"
                required
                data-testid="new-password"
                {...form.getInputProps("new_password")}
              />
              <PasswordInput
                label="Confirm new password"
                autoComplete="new-password"
                required
                data-testid="confirm-password"
                {...form.getInputProps("confirm_password")}
              />
              <Button type="submit" loading={submitting} data-testid="submit-change-password">
                Change password
              </Button>
            </Stack>
          </form>
        </Paper>
      </Stack>
    </AppLayout>
  );
}
