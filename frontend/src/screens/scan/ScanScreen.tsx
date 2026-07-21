import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  Alert,
  AppShell,
  Box,
  Button,
  Card,
  Center,
  Group,
  Loader,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useNavigate } from "react-router-dom";
import { BrowserQRCodeReader } from "@zxing/browser";
import type { IScannerControls } from "@zxing/browser";
import { api, ApiError } from "../../api/client";
import { extractQrToken, isNumericAssetId } from "./qrToken";

type CameraState = "idle" | "starting" | "scanning" | "unavailable";

/**
 * Scan screen (T4.3, primary mobile FAB entry point from the Dashboard):
 * `@zxing/browser` camera QR scan -> extract the asset's stable `qr_token`
 * -> `GET /api/v1/resolve/{qr_token}` (T4.1) -> navigate to Asset Detail,
 * which already offers check-in/out contextually (T3.5). Requires a secure
 * context (HTTPS or localhost) for `getUserMedia` — already satisfied by
 * T4.2's PWA setup; no extra work needed here.
 *
 * Risk R5 (camera may be denied/unavailable): a manual token/asset-ID entry
 * field is ALWAYS rendered alongside the camera view, not gated behind a
 * failure state, so a camera problem never blocks the flow — it just goes
 * unused. Camera failures (permission denied, no device, insecure context)
 * degrade the camera panel to an inline explanation without touching the
 * manual path.
 */
export function ScanScreen() {
  const navigate = useNavigate();

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const controlsRef = useRef<IScannerControls | null>(null);
  const resolvingRef = useRef(false);

  const [cameraState, setCameraState] = useState<CameraState>("idle");
  const [cameraError, setCameraError] = useState<string | null>(null);

  const [manualValue, setManualValue] = useState("");
  const [resolving, setResolving] = useState(false);
  const [resolveError, setResolveError] = useState<string | null>(null);

  const stopCamera = useCallback(() => {
    controlsRef.current?.stop();
    controlsRef.current = null;
  }, []);

  const resolveAndNavigate = useCallback(
    async (input: { token?: string; assetId?: number }) => {
      if (resolvingRef.current) return;
      resolvingRef.current = true;
      setResolving(true);
      setResolveError(null);
      try {
        const asset = input.assetId
          ? await api.getAsset(input.assetId)
          : await api.resolveQrToken(input.token as string);
        stopCamera();
        setCameraState("idle");
        navigate(`/assets/${asset.id}`);
      } catch (err) {
        const message =
          err instanceof ApiError
            ? err.status === 404
              ? "No asset found for that code. Check the code and try again."
              : err.problem.detail ?? err.problem.title
            : "Unable to reach the server. Please try again.";
        setResolveError(message);
      } finally {
        resolvingRef.current = false;
        setResolving(false);
      }
    },
    [navigate, stopCamera],
  );

  useEffect(() => {
    let cancelled = false;

    async function start() {
      if (!navigator.mediaDevices?.getUserMedia) {
        setCameraState("unavailable");
        setCameraError("This browser/device doesn't support camera access.");
        return;
      }
      if (!window.isSecureContext) {
        setCameraState("unavailable");
        setCameraError("Camera scanning requires a secure (HTTPS) connection.");
        return;
      }

      setCameraState("starting");
      setCameraError(null);
      try {
        const reader = new BrowserQRCodeReader();
        const controls = await reader.decodeFromVideoDevice(
          undefined,
          videoRef.current ?? undefined,
          (result, error) => {
            if (cancelled) return;
            if (result) {
              const token = extractQrToken(result.getText());
              if (token) void resolveAndNavigate({ token });
              return;
            }
            // `error` fires on every frame with nothing decoded
            // (`NotFoundException`) — that's the normal steady state while
            // aiming at a code, not a failure to surface. Any other decode
            // error is transient too; the manual fallback is always
            // available regardless, so we just keep the scanner running.
            void error;
          },
        );
        if (cancelled) {
          controls.stop();
          return;
        }
        controlsRef.current = controls;
        setCameraState("scanning");
      } catch (err) {
        if (cancelled) return;
        setCameraState("unavailable");
        setCameraError(cameraFailureMessage(err));
      }
    }

    void start();

    return () => {
      cancelled = true;
      stopCamera();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleManualSubmit = (e: FormEvent) => {
    e.preventDefault();
    const value = manualValue.trim();
    if (!value) return;
    if (isNumericAssetId(value)) {
      void resolveAndNavigate({ assetId: Number(value) });
    } else {
      void resolveAndNavigate({ token: extractQrToken(value) });
    }
  };

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Title order={4}>Scan asset</Title>
          <Button variant="subtle" onClick={() => navigate("/")} data-testid="scan-close">
            Close
          </Button>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Stack gap="lg" maw={480} mx="auto">
          <Card withBorder padding="sm">
            <Stack gap="xs">
              <Text size="sm" fw={600}>
                Camera
              </Text>
              <Box
                pos="relative"
                style={{
                  aspectRatio: "1",
                  background: "var(--mantine-color-dark-7)",
                  borderRadius: "var(--mantine-radius-sm)",
                  overflow: "hidden",
                }}
              >
                <video
                  ref={videoRef}
                  data-testid="scan-video"
                  style={{
                    width: "100%",
                    height: "100%",
                    objectFit: "cover",
                    display: cameraState === "scanning" || cameraState === "starting" ? "block" : "none",
                  }}
                  muted
                  playsInline
                />
                {cameraState === "starting" && (
                  <Center pos="absolute" inset={0}>
                    <Loader color="white" />
                  </Center>
                )}
                {cameraState === "unavailable" && (
                  <Center pos="absolute" inset={0} p="md">
                    <Text size="sm" c="white" ta="center">
                      Camera unavailable
                    </Text>
                  </Center>
                )}
              </Box>
              {cameraState === "unavailable" && cameraError && (
                <Alert color="yellow" data-testid="camera-unavailable">
                  {cameraError} Use the manual entry below instead.
                </Alert>
              )}
              {cameraState === "scanning" && (
                <Text size="xs" c="dimmed" ta="center">
                  Point the camera at the asset's QR label.
                </Text>
              )}
            </Stack>
          </Card>

          <Card withBorder padding="sm">
            <form onSubmit={handleManualSubmit}>
              <Stack gap="xs">
                <Text size="sm" fw={600}>
                  Enter manually
                </Text>
                <Text size="xs" c="dimmed">
                  Camera not working? Type or paste the asset's QR token or its asset ID.
                </Text>
                <TextInput
                  placeholder="Token or asset ID"
                  value={manualValue}
                  onChange={(e) => setManualValue(e.currentTarget.value)}
                  data-testid="manual-token-input"
                  disabled={resolving}
                  autoComplete="off"
                />
                <Button
                  type="submit"
                  loading={resolving}
                  disabled={!manualValue.trim()}
                  data-testid="manual-token-submit"
                >
                  Go
                </Button>
              </Stack>
            </form>
          </Card>

          {resolveError && (
            <Alert color="red" withCloseButton onClose={() => setResolveError(null)} data-testid="resolve-error">
              {resolveError}
            </Alert>
          )}
        </Stack>
      </AppShell.Main>
    </AppShell>
  );
}

/** Turns a `getUserMedia`/decoder failure into a plain-language message —
 * covers the documented risk R5 failure modes (permission denied, no
 * camera device, camera already in use). */
function cameraFailureMessage(err: unknown): string {
  const name = err instanceof DOMException ? err.name : undefined;
  switch (name) {
    case "NotAllowedError":
      return "Camera access was denied.";
    case "NotFoundError":
    case "OverconstrainedError":
      return "No camera was found on this device.";
    case "NotReadableError":
      return "The camera is already in use by another app.";
    default:
      return "Couldn't start the camera.";
  }
}
