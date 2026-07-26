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
  TextInput,
  Title,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { Link, useNavigate } from "react-router-dom";
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
    <Box
      mih="100vh"
      style={{
        background:
          "radial-gradient(1200px circle at 15% -10%, var(--mantine-color-brand-1), transparent 55%)," +
          "radial-gradient(900px circle at 100% 10%, var(--mantine-color-accent-0), transparent 45%)",
      }}
    >
      <Center mih="100vh" px="md">
        <Container size={420} w="100%" py={40}>
          <Stack align="center" gap={4} mb="lg">
            <Box
              w={48}
              h={48}
              bg="brand.6"
              style={{
                borderRadius: "var(--mantine-radius-lg)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "white",
                fontWeight: 800,
                fontSize: 18,
                boxShadow: "var(--mantine-shadow-md)",
              }}
            >
              CX
            </Box>
            <Title ta="center" order={2} mt="xs">
              Cortex
            </Title>
            <Text ta="center" c="dimmed" size="sm">
              Lab Asset &amp; Inventory Management
            </Text>
          </Stack>

          <Paper withBorder shadow="lg" p={30} radius="lg">
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
                <Anchor
                  component={Link}
                  to="/forgot-password"
                  ta="center"
                  size="sm"
                  data-testid="forgot-password-link"
                >
                  Forgot password?
                </Anchor>
              </Stack>
            </form>
          </Paper>
        </Container>
      </Center>
    </Box>
  );
}
