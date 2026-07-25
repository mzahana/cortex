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
  kind: "photo" | "doc";
  previewUrl: string | null;
  fileName: string;
  status: "uploading" | "error";
  error?: string;
}

interface PhotoCaptureProps {
  assetId: number;
  /** Non-photo attachments (`kind: "doc"`) render alongside photos in the
   * same grid — this component uploads both `kind="photo"` (camera/photo
   * picker) and `kind="doc"` (receipt/purchase-order/other document
   * picker), and displays whatever the asset already has of either kind. */
  attachments: Attachment[];
  canAttach: boolean;
  onUploaded: (attachment: Attachment) => void;
}

/** Matches `DOC_CONTENT_TYPES`/`CONTENT_TYPE_EXTENSIONS` in
 * `apps.assets.services` — kept in sync manually since the allowlist is
 * enforced server-side regardless of what the picker offers here. */
const DOC_ACCEPT = ".pdf,.doc,.docx,.xls,.xlsx,.txt";

/**
 * T4.4 — Camera photo capture, embedded in Asset Detail's "Photos &
 * attachments" card. Also handles `kind="doc"` uploads (receipts, purchase
 * orders, and other non-image documents) via a second picker that reuses
 * the same upload/pending/error plumbing.
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
  const photoResetRef = useRef<() => void>(null);
  const docResetRef = useRef<() => void>(null);

  const upload = async (file: File, kind: "photo" | "doc") => {
    const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const previewUrl = kind === "photo" ? URL.createObjectURL(file) : null;
    setPending((prev) => [...prev, { key, kind, previewUrl, fileName: file.name, status: "uploading" }]);

    try {
      const attachment = await api.uploadAssetAttachment(assetId, file, kind);
      onUploaded(attachment);
      setPending((prev) => prev.filter((p) => p.key !== key));
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    } catch (err) {
      const message =
        err instanceof ApiError ? err.problem.detail ?? err.problem.title : "Upload failed. Please try again.";
      setPending((prev) => prev.map((p) => (p.key === key ? { ...p, status: "error", error: message } : p)));
    } finally {
      photoResetRef.current?.();
      docResetRef.current?.();
    }
  };

  const handlePhotoChange = (file: File | null) => {
    if (!file) return;
    void upload(file, "photo");
  };

  const handleDocChange = (file: File | null) => {
    if (!file) return;
    void upload(file, "doc");
  };

  const dismissPending = (key: string) => {
    setPending((prev) => {
      const found = prev.find((p) => p.key === key);
      if (found?.previewUrl) URL.revokeObjectURL(found.previewUrl);
      return prev.filter((p) => p.key !== key);
    });
  };

  const isUploading = pending.some((p) => p.status === "uploading");

  return (
    <Stack gap="xs">
      <Group justify="space-between">
        <Title order={6}>Photos &amp; attachments</Title>
        {canAttach ? (
          <Group gap="xs">
            <FileButton resetRef={photoResetRef} onChange={handlePhotoChange} accept="image/*" capture="environment">
              {(props) => (
                <Button size="xs" variant="light" loading={isUploading} data-testid="capture-photo-button" {...props}>
                  Take / add photo
                </Button>
              )}
            </FileButton>
            <FileButton resetRef={docResetRef} onChange={handleDocChange} accept={DOC_ACCEPT}>
              {(props) => (
                <Button size="xs" variant="light" loading={isUploading} data-testid="upload-receipt-button" {...props}>
                  Upload receipt / PO
                </Button>
              )}
            </FileButton>
          </Group>
        ) : (
          <Group gap="xs">
            <Tooltip label="You don't have permission to attach files to this asset">
              <Button size="xs" variant="light" disabled>
                Take / add photo
              </Button>
            </Tooltip>
            <Tooltip label="You don't have permission to attach files to this asset">
              <Button size="xs" variant="light" disabled>
                Upload receipt / PO
              </Button>
            </Tooltip>
          </Group>
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
            .map((p) =>
              p.kind === "photo" && p.previewUrl ? (
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
              ) : (
                <Card key={p.key} withBorder padding="xs" h={100} data-testid="doc-pending-tile">
                  <Stack gap={4} justify="center" align="center" h="100%">
                    <Loader size="sm" />
                    <Text size="xs" truncate="end" ta="center">
                      {p.fileName}
                    </Text>
                  </Stack>
                </Card>
              ),
            )}
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
              <Card
                key={att.id}
                component="a"
                href={`/media/${att.storage_key}`}
                target="_blank"
                rel="noopener noreferrer"
                withBorder
                padding="xs"
                h={100}
                data-testid="attachment-doc"
              >
                <Stack gap={4} justify="center" align="center" h="100%">
                  <Text size="xs" truncate="end" ta="center">
                    {att.filename}
                  </Text>
                </Stack>
              </Card>
            ),
          )}
        </SimpleGrid>
      )}
    </Stack>
  );
}
