import { useEffect, useState } from "react";
import { Alert, Button, Paper, PasswordInput, Stack, Text, TextInput, Title } from "@mantine/core";
import { useForm } from "@mantine/form";
import { api, ApiError } from "../api/client";
import { useAuth } from "../hooks/useAuth";
import { AppLayout } from "../layout/AppLayout";
import { fieldErrorsFromProblem } from "../api/problem";

interface ProfileValues {
  name: string;
}

interface ChangePasswordValues {
  current_password: string;
  new_password: string;
  confirm_password: string;
}

/**
 * Account screen — a signed-in user edits their OWN profile name
 * (`PATCH /api/v1/me`) and changes their OWN password
 * (`POST /api/v1/me/password`). The server re-verifies the current password
 * and refreshes the session auth hash so the user stays logged in.
 * Server-side `AUTH_PASSWORD_VALIDATORS` failures come back as RFC-7807
 * `errors.new_password` and are surfaced as a field error; a wrong current
 * password comes back as `errors.current_password`.
 */
export function AccountScreen() {
  const { me, refresh } = useAuth();
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const [profileError, setProfileError] = useState<string | null>(null);
  const [profileSaved, setProfileSaved] = useState(false);
  const [savingProfile, setSavingProfile] = useState(false);

  // `me.name` is the RAW stored name (may be ""), never the email fallback —
  // see `Me.name` vs `Me.display_name` — so this form always round-trips
  // exactly what is in the DB.
  const profileForm = useForm<ProfileValues>({
    initialValues: { name: me?.name ?? "" },
    validate: {
      name: (v) => (v.trim().length <= 255 ? null : "Name must be 255 characters or fewer"),
    },
  });

  // `me` is null on the very first render (auth context still loading), so
  // seed the field once it arrives — without clobbering anything the user has
  // already typed.
  useEffect(() => {
    if (me && !profileForm.isDirty("name")) {
      profileForm.setFieldValue("name", me.name);
      profileForm.resetDirty({ name: me.name });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me?.id, me?.name]);

  const handleProfileSubmit = async (values: ProfileValues) => {
    setProfileError(null);
    setProfileSaved(false);
    setSavingProfile(true);
    try {
      await api.updateMe({ name: values.name.trim() });
      // Refresh the auth context so the sidebar/greeting pick the new name up
      // immediately rather than on the next full page load.
      await refresh();
      setProfileSaved(true);
    } catch (err) {
      if (err instanceof ApiError) {
        const fieldErrors = fieldErrorsFromProblem(err.problem);
        if (fieldErrors) {
          profileForm.setErrors(fieldErrors);
        } else {
          setProfileError(err.problem.detail ?? err.problem.title);
        }
      } else {
        setProfileError("Unable to reach the server. Please try again.");
      }
    } finally {
      setSavingProfile(false);
    }
  };

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
          Signed in as {me?.email} · {me?.tenant.name}
        </Text>

        <Paper withBorder p="lg" radius="md">
          <form onSubmit={profileForm.onSubmit(handleProfileSubmit)} noValidate>
            <Stack>
              <Title order={5}>Profile</Title>
              {profileSaved && (
                <Alert color="teal" title="Profile updated" data-testid="update-profile-success">
                  Your name has been updated.
                </Alert>
              )}
              {profileError && (
                <Alert color="red" title="Couldn't update profile" data-testid="update-profile-error">
                  {profileError}
                </Alert>
              )}
              <TextInput
                label="Display name"
                description="Shown in the app instead of your email address."
                placeholder="e.g. Mohamed Abdelkader"
                autoComplete="name"
                data-testid="profile-name"
                {...profileForm.getInputProps("name")}
              />
              <Button
                type="submit"
                loading={savingProfile}
                data-testid="submit-update-profile"
                style={{ alignSelf: "flex-start" }}
              >
                Save name
              </Button>
            </Stack>
          </form>
        </Paper>

        <Paper withBorder p="lg" radius="md">
          <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
            <Stack>
              <Title order={5}>Password</Title>
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
