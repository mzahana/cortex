import { useState } from "react";
import {
  Alert,
  Anchor,
  Box,
  Button,
  Center,
  Container,
  Paper,
  PasswordInput,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { fieldErrorsFromProblem } from "../api/problem";

interface ResetPasswordValues {
  new_password: string;
  confirm_password: string;
}

/**
 * Password-reset confirm screen (unauthenticated) — the landing page for the
 * emailed link (`{FRONTEND_BASE_URL}/reset-password?token=...&tenant=...`).
 * Reads `token` + `tenant` from the query string (never asks the user to type
 * them) and posts them with the chosen password to
 * `POST /api/v1/auth/password-reset/confirm`. A bad/expired/used link comes
 * back as `invalid-reset-token`; a weak password as `errors.new_password`.
 */
export function ResetPasswordScreen() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const token = searchParams.get("token") ?? "";
  const tenant = searchParams.get("tenant") ?? "";

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const form = useForm<ResetPasswordValues>({
    initialValues: { new_password: "", confirm_password: "" },
    validate: {
      new_password: (v) => (v ? null : "Enter a new password"),
      confirm_password: (v, values) =>
        v === values.new_password ? null : "Passwords do not match",
    },
  });

  const linkOk = token !== "" && tenant !== "";

  const handleSubmit = async (values: ResetPasswordValues) => {
    setSubmitError(null);
    setSubmitting(true);
    try {
      await api.confirmPasswordReset({
        tenant,
        token,
        new_password: values.new_password,
      });
      setDone(true);
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
    <Box mih="100vh">
      <Center mih="100vh" px="md">
        <Container size={420} w="100%" py={40}>
          <Stack align="center" gap={4} mb="lg">
            <Title ta="center" order={2}>
              Choose a new password
            </Title>
          </Stack>

          <Paper withBorder shadow="lg" p={30} radius="lg">
            {!linkOk ? (
              <Stack>
                <Alert color="red" title="Invalid link" data-testid="reset-link-invalid">
                  This password-reset link is missing information. Please request a new one.
                </Alert>
                <Button component={Link} to="/forgot-password" variant="light" fullWidth>
                  Request a new link
                </Button>
              </Stack>
            ) : done ? (
              <Stack>
                <Alert color="teal" title="Password updated" data-testid="reset-success">
                  Your password has been changed. You can now sign in with it.
                </Alert>
                <Button onClick={() => navigate("/login", { replace: true })} fullWidth>
                  Go to sign in
                </Button>
              </Stack>
            ) : (
              <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
                <Stack>
                  {submitError && (
                    <Alert color="red" title="Couldn't reset password" data-testid="reset-error">
                      <Stack gap="xs" align="flex-start">
                        <Text size="sm">{submitError}</Text>
                        <Anchor component={Link} to="/forgot-password" size="sm">
                          Request a new link
                        </Anchor>
                      </Stack>
                    </Alert>
                  )}
                  <PasswordInput
                    label="New password"
                    autoComplete="new-password"
                    required
                    data-testid="reset-new-password"
                    {...form.getInputProps("new_password")}
                  />
                  <PasswordInput
                    label="Confirm new password"
                    autoComplete="new-password"
                    required
                    data-testid="reset-confirm-password"
                    {...form.getInputProps("confirm_password")}
                  />
                  <Button type="submit" fullWidth mt="sm" loading={submitting} data-testid="submit-reset">
                    Set new password
                  </Button>
                </Stack>
              </form>
            )}
          </Paper>
        </Container>
      </Center>
    </Box>
  );
}
