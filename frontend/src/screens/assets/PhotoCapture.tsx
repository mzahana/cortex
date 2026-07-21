import { useRef, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  FileButton,
  Group,
  Image,
  Loader,
  SimpleGrid,
  Stack,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { api, ApiError } from "../../api/client";
import type { Attachment } from "../../api/types";

interface PendingPhoto {
  key: string;
  previewUrl: string;
  fileName: string;
  status: "uploading" | "error";
  error?: string;
}

interface PhotoCaptureProps {
  assetId: number;
  /** Non-photo attachments (`kind: "doc"`) render alongside photos in the
   * same grid, same as the previous inline implementation — this component
   * only ever *uploads* `kind="photo"`, but still displays whatever the
   * asset already has. */
  attachments: Attachment[];
  canAttach: boolean;
  onUploaded: (attachment: Attachment) => void;
}

/**
 * T4.4 — Camera photo capture, embedded in Asset Detail's "Photos &
 * attachments" card.
 *
 * `capture="environment"` on the underlying `<input type="file"
 * accept="image/*">` (Mantine's `FileButton` passes both straight through
 * to the native input) is the standard mobile-web pattern for opening the
 * rear camera directly — it works on iOS Safari and Android Chrome without
 * `getUserMedia`/canvas capture, and only needs a secure context (the
 * Cloudflare Tunnel's `https://cortex.<domain>` in prod; localhost is a
 * secure context too for dev — docs/deployment.md §3). Desktop browsers
 * without a camera silently fall back to a normal file picker, which
 * doubles as the R5 manual-entry-adjacent fallback for the *file* half of
 * F6 (any image the browser hands back through this input still uploads
 * normally) — the QR-scan half's manual token-entry fallback lives in the
 * scan screen, not here.
 *
 * Upload is fire-and-forget from the caller's perspective (CLAUDE.md "slow
 * work never blocks the request" applies to the UI thread here too, not
 * just Celery): selecting/capturing a file immediately renders an
 * optimistic in-progress tile (an object-URL preview of the raw capture)
 * and kicks off the multipart POST in the background. Nothing here blocks
 * interaction or forces the parent to refetch the whole asset — on success
 * the real `Attachment` the server returned is handed to `onUploaded` so
 * the parent can splice it straight into its attachments list (renders
 * within seconds, no page reload, no `GET` asset detail round-trip); on
 * failure the tile turns into an inline, dismissible error instead of
 * vanishing silently.
 */
export function PhotoCapture({ assetId, attachments, canAttach, onUploaded }: PhotoCaptureProps) {
  const [pending, setPending] = useState<PendingPhoto[]>([]);
  const resetRef = useRef<() => void>(null);

  const upload = async (file: File) => {
    const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const previewUrl = URL.createObjectURL(file);
    setPending((prev) => [...prev, { key, previewUrl, fileName: file.name, status: "uploading" }]);

    try {
      const attachment = await api.uploadAssetAttachment(assetId, file, "photo");
      onUploaded(attachment);
      setPending((prev) => prev.filter((p) => p.key !== key));
      URL.revokeObjectURL(previewUrl);
    } catch (err) {
      const message =
        err instanceof ApiError ? err.problem.detail ?? err.problem.title : "Upload failed. Please try again.";
      setPending((prev) => prev.map((p) => (p.key === key ? { ...p, status: "error", error: message } : p)));
    } finally {
      resetRef.current?.();
    }
  };

  const handleChange = (file: File | null) => {
    if (!file) return;
    void upload(file);
  };

  const dismissPending = (key: string) => {
    setPending((prev) => {
      const found = prev.find((p) => p.key === key);
      if (found) URL.revokeObjectURL(found.previewUrl);
      return prev.filter((p) => p.key !== key);
    });
  };

  const isUploading = pending.some((p) => p.status === "uploading");

  return (
    <Stack gap="xs">
      <Group justify="space-between">
        <Title order={6}>Photos &amp; attachments</Title>
        {canAttach ? (
          <FileButton resetRef={resetRef} onChange={handleChange} accept="image/*" capture="environment">
            {(props) => (
              <Button size="xs" variant="light" loading={isUploading} data-testid="capture-photo-button" {...props}>
                Take / add photo
              </Button>
            )}
          </FileButton>
        ) : (
          <Tooltip label="You don't have permission to attach files to this asset">
            <Button size="xs" variant="light" disabled>
              Take / add photo
            </Button>
          </Tooltip>
        )}
      </Group>

      {pending
        .filter((p) => p.status === "error")
        .map((p) => (
          <Alert
            key={p.key}
            color="red"
            withCloseButton
            onClose={() => dismissPending(p.key)}
            data-testid="attachment-upload-error"
          >
            {p.fileName}: {p.error}
          </Alert>
        ))}

      {attachments.length === 0 && pending.length === 0 ? (
        <Text size="sm" c="dimmed">
          No photos or documents yet.
        </Text>
      ) : (
        <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="xs">
          {pending
            .filter((p) => p.status === "uploading")
            .map((p) => (
              <Card key={p.key} withBorder padding={0} pos="relative" data-testid="photo-pending-tile">
                <Image src={p.previewUrl} alt={p.fileName} radius="sm" fit="cover" h={100} style={{ opacity: 0.5 }} />
                <Group
                  pos="absolute"
                  top={0}
                  left={0}
                  right={0}
                  bottom={0}
                  justify="center"
                  align="center"
                  gap={4}
                  wrap="nowrap"
                >
                  <Loader size="sm" />
                </Group>
                <Badge size="xs" variant="filled" color="blue" pos="absolute" bottom={4} left={4}>
                  Uploading…
                </Badge>
              </Card>
            ))}
          {attachments.map((att) =>
            att.kind === "photo" ? (
              <Image
                key={att.id}
                src={`/media/${att.storage_key}`}
                alt={att.filename}
                radius="sm"
                fit="cover"
                h={100}
                data-testid="attachment-photo"
              />
            ) : (
              <Card key={att.id} withBorder padding="xs">
                <Text size="xs" truncate>
                  {att.filename}
                </Text>
              </Card>
            ),
          )}
        </SimpleGrid>
      )}
    </Stack>
  );
}
