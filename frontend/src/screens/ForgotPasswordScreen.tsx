import { useState } from "react";
import {
  Alert,
  Anchor,
  Box,
  Button,
  Center,
  Container,
  Paper,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { Link } from "react-router-dom";
import { api } from "../api/client";

interface ForgotPasswordValues {
  tenant: string;
  email: string;
}

/**
 * Forgot-password request screen (unauthenticated) — collects the tenant slug
 * + email and asks the server to email a reset link
 * (`POST /api/v1/auth/password-reset/request`). The server ALWAYS responds
 * with the same generic message whether or not the account exists (no
 * enumeration), so this screen shows one success state regardless.
 */
export function ForgotPasswordScreen() {
  const [submitting, setSubmitting] = useState(false);
  const [sent, setSent] = useState(false);
  const [message, setMessage] = useState<string>("");

  const form = useForm<ForgotPasswordValues>({
    initialValues: { tenant: "", email: "" },
    validate: {
      tenant: (v) => (v.trim() ? null : "Tenant is required"),
      email: (v) => (/^\S+@\S+\.\S+$/.test(v) ? null : "Enter a valid email"),
    },
  });

  const handleSubmit = async (values: ForgotPasswordValues) => {
    setSubmitting(true);
    try {
      // Generic 200 either way — even a network hiccup shouldn't reveal
      // whether the account exists, so we still show the neutral confirmation.
      const res = await api.requestPasswordReset({
        tenant: values.tenant.trim(),
        email: values.email.trim(),
      });
      setMessage(res.detail);
    } catch {
      setMessage(
        "If an account matches that tenant and email, a password-reset link has been sent.",
      );
    } finally {
      setSubmitting(false);
      setSent(true);
    }
  };

  return (
    <Box mih="100vh">
      <Center mih="100vh" px="md">
        <Container size={420} w="100%" py={40}>
          <Stack align="center" gap={4} mb="lg">
            <Title ta="center" order={2}>
              Reset your password
            </Title>
            <Text ta="center" c="dimmed" size="sm">
              We&apos;ll email you a link to set a new one.
            </Text>
          </Stack>

          <Paper withBorder shadow="lg" p={30} radius="lg">
            {sent ? (
              <Stack>
                <Alert color="teal" title="Check your email" data-testid="forgot-password-sent">
                  {message}
                </Alert>
                <Button component={Link} to="/login" variant="light" fullWidth>
                  Back to sign in
                </Button>
              </Stack>
            ) : (
              <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
                <Stack>
                  <TextInput
                    label="Tenant"
                    placeholder="acme-robotics"
                    description="Your lab's tenant slug"
                    autoComplete="organization"
                    required
                    data-testid="forgot-tenant"
                    {...form.getInputProps("tenant")}
                  />
                  <TextInput
                    label="Email"
                    placeholder="you@example.com"
                    type="email"
                    autoComplete="username"
                    inputMode="email"
                    required
                    data-testid="forgot-email"
                    {...form.getInputProps("email")}
                  />
                  <Button type="submit" fullWidth mt="sm" loading={submitting} data-testid="submit-forgot">
                    Send reset link
                  </Button>
                  <Anchor component={Link} to="/login" ta="center" size="sm">
                    Back to sign in
                  </Anchor>
                </Stack>
              </form>
            )}
          </Paper>
        </Container>
      </Center>
    </Box>
  );
}
