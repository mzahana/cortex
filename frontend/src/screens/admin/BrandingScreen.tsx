import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Center,
  Group,
  Image,
  Loader,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { IconPhotoUp, IconTrash } from "@tabler/icons-react";
import { api, ApiError } from "../../api/client";
import { fieldErrorsFromProblem } from "../../api/problem";
import { hasPermission, TENANT_MANAGE } from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import { AppLayout } from "../../layout/AppLayout";
import type { TenantBranding } from "../../api/types";

/** Mirrors the server-side allowlist in `apps.tenancy.services`
 * (`LOGO_CONTENT_TYPES` / `MAX_LOGO_UPLOAD_BYTES`) so an obviously-wrong file
 * is rejected before it is uploaded. The server re-checks — this is
 * convenience, never the boundary. */
const ACCEPTED_TYPES = ["image/png", "image/jpeg", "image/webp"];
const MAX_BYTES = 2 * 1024 * 1024;

/**
 * Admin: Lab Branding — uploads/removes the tenant's logo, which the app
 * chrome (`AppLayout`'s sidebar brand block and mobile top bar) shows next to
 * the lab name on every screen.
 *
 * Reads are open to any member (`GET /api/v1/tenancy/logo`), but writes need
 * `tenant.manage`; unlike Email/Session Settings the read IS useful to a
 * non-admin, so this screen renders the current logo for everyone and only
 * hides the upload/remove controls — the server still enforces the rule.
 */
export function BrandingScreen() {
  const { me, refresh } = useAuth();
  const canManage = hasPermission(me, TENANT_MANAGE);

  const [branding, setBranding] = useState<TenantBranding | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setBranding(await api.tenantBranding());
    } catch (err) {
      setLoadError(
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

  const handleFile = async (file: File | undefined) => {
    if (!file) return;
    setError(null);
    setSaved(null);

    if (!ACCEPTED_TYPES.includes(file.type)) {
      setError("Choose a PNG, JPEG, or WebP image.");
      return;
    }
    if (file.size > MAX_BYTES) {
      setError("That image is larger than 2 MB. Choose a smaller file.");
      return;
    }

    setBusy(true);
    try {
      setBranding(await api.uploadTenantLogo(file));
      // Refresh `/me` so the sidebar/top bar show the new logo immediately.
      await refresh();
      setSaved("Logo updated.");
    } catch (err) {
      // The server's size/type rejection arrives as `errors.file` (RFC-7807
      // field errors); prefer that message over the generic detail.
      setError(
        err instanceof ApiError
          ? fieldErrorsFromProblem(err.problem)?.file ??
            err.problem.detail ??
            err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setBusy(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const handleRemove = async () => {
    setError(null);
    setSaved(null);
    setBusy(true);
    try {
      setBranding(await api.deleteTenantLogo());
      await refresh();
      setSaved("Logo removed.");
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <AppLayout title="Lab Branding">
      <Stack gap="md" maw={560}>
        {loading && (
          <Center p="xl">
            <Loader />
          </Center>
        )}

        {!loading && loadError && (
          <Alert color="red" title="Couldn't load branding" data-testid="branding-load-error">
            <Stack gap="sm" align="flex-start">
              <Text size="sm">{loadError}</Text>
              <Button variant="light" onClick={() => void load()}>
                Retry
              </Button>
            </Stack>
          </Alert>
        )}

        {!loading && !loadError && branding && (
          <Card withBorder padding="lg" radius="md">
            <Stack>
              <Title order={5}>{branding.name}</Title>
              <Text size="sm" c="dimmed">
                The logo appears beside your lab name in the sidebar and on the mobile top bar.
                PNG, JPEG, or WebP, up to 2 MB. A square image works best.
              </Text>

              {saved && (
                <Alert color="teal" data-testid="branding-success">
                  {saved}
                </Alert>
              )}
              {error && (
                <Alert color="red" title="Couldn't save logo" data-testid="branding-error">
                  {error}
                </Alert>
              )}

              <Group gap="md" align="center">
                {branding.logo_url ? (
                  <Image
                    src={branding.logo_url}
                    alt={`${branding.name} logo`}
                    w={96}
                    h={96}
                    fit="contain"
                    data-testid="branding-logo-preview"
                    style={{ borderRadius: "var(--mantine-radius-md)" }}
                  />
                ) : (
                  <Text size="sm" c="dimmed" data-testid="branding-no-logo">
                    No logo uploaded yet.
                  </Text>
                )}
              </Group>

              {canManage && (
                <Group>
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept={ACCEPTED_TYPES.join(",")}
                    style={{ display: "none" }}
                    data-testid="branding-file-input"
                    onChange={(event) => void handleFile(event.currentTarget.files?.[0])}
                  />
                  <Button
                    leftSection={<IconPhotoUp size={16} />}
                    loading={busy}
                    onClick={() => fileInputRef.current?.click()}
                    data-testid="branding-upload"
                  >
                    {branding.logo_url ? "Replace logo" : "Upload logo"}
                  </Button>
                  {branding.logo_url && (
                    <Button
                      variant="light"
                      color="red"
                      leftSection={<IconTrash size={16} />}
                      loading={busy}
                      onClick={() => void handleRemove()}
                      data-testid="branding-remove"
                    >
                      Remove
                    </Button>
                  )}
                </Group>
              )}

              {!canManage && (
                <Text size="sm" c="dimmed">
                  Only an admin (`tenant.manage`) can change the lab logo.
                </Text>
              )}
            </Stack>
          </Card>
        )}
      </Stack>
    </AppLayout>
  );
}
