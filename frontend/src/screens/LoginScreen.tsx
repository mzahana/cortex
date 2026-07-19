import { useState } from "react";
import {
  Alert,
  Button,
  Container,
  Paper,
  PasswordInput,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { useAuth } from "../hooks/useAuth";

interface LoginFormValues {
  tenant: string;
  email: string;
  password: string;
}

/**
 * Login screen — tenant slug + email + password per the frozen login body
 * (docs/api-and-ui.md). `tenant` disambiguates `email`, which is only unique
 * per tenant, before any session exists.
 */
export function LoginScreen() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const form = useForm<LoginFormValues>({
    initialValues: { tenant: "", email: "", password: "" },
    validate: {
      tenant: (value) => (value.trim() ? null : "Tenant is required"),
      email: (value) => (/^\S+@\S+\.\S+$/.test(value) ? null : "Enter a valid email"),
      password: (value) => (value ? null : "Password is required"),
    },
  });

  const handleSubmit = async (values: LoginFormValues) => {
    setSubmitError(null);
    setSubmitting(true);
    try {
      await login({
        tenant: values.tenant.trim(),
        email: values.email.trim(),
        password: values.password,
      });
      navigate("/", { replace: true });
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.isRateLimited) {
          setSubmitError(
            err.problem.detail ??
              "Too many attempts. Please wait a moment before retrying.",
          );
        } else if (err.isInvalidCredentials) {
          setSubmitError(
            err.problem.detail ?? "Invalid tenant, email, or password.",
          );
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
    <Container size={420} py={80}>
      <Title ta="center" order={2}>
        LMS
      </Title>
      <Text ta="center" c="dimmed" size="sm" mt={4}>
        Lab Asset &amp; Inventory Management
      </Text>

      <Paper withBorder shadow="md" p={30} mt={30} radius="md">
        <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
          <Stack>
            {submitError && (
              <Alert color="red" title="Sign-in failed" data-testid="login-error">
                {submitError}
              </Alert>
            )}
            <TextInput
              label="Tenant"
              placeholder="acme-robotics"
              description="Your lab's tenant slug"
              autoComplete="organization"
              inputMode="text"
              required
              {...form.getInputProps("tenant")}
            />
            <TextInput
              label="Email"
              placeholder="you@example.com"
              type="email"
              autoComplete="username"
              inputMode="email"
              required
              {...form.getInputProps("email")}
            />
            <PasswordInput
              label="Password"
              placeholder="Your password"
              autoComplete="current-password"
              required
              {...form.getInputProps("password")}
            />
            <Button type="submit" fullWidth mt="md" loading={submitting} size="md">
              Sign in
            </Button>
          </Stack>
        </form>
      </Paper>
    </Container>
  );
}
