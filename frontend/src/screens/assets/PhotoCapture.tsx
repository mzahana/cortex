import { useRef, useState } from "react";
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Card,
  FileButton,
  Group,
  Image,
  Loader,
  Modal,
  Select,
  SimpleGrid,
  Stack,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { IconTrash } from "@tabler/icons-react";
import { api, ApiError } from "../../api/client";
import type { Attachment, AttachmentDocType } from "../../api/types";

/** `apps.assets.models.Attachment.DocType`, minus the blank member (which is
 * "Unspecified" — represented by the `""` value of the picker itself). */
export const DOC_TYPE_OPTIONS: { value: AttachmentDocType; label: string }[] = [
  { value: "", label: "Unspecified" },
  { value: "invoice", label: "Invoice" },
  { value: "receipt", label: "Receipt" },
  { value: "purchase_order", label: "Purchase order" },
  { value: "quote", label: "Quote" },
  { value: "warranty", label: "Warranty" },
  { value: "manual", label: "Manual / datasheet" },
  { value: "other", label: "Other" },
];

const DOC_TYPE_LABELS: Record<string, string> = Object.fromEntries(
  DOC_TYPE_OPTIONS.filter((o) => o.value).map((o) => [o.value, o.label]),
);

/** Financial types, mirroring `Attachment.FINANCIAL_DOC_TYPES` — these get a
 * colored badge so "which one is the invoice" is answerable at a glance. */
const FINANCIAL_DOC_TYPES = new Set(["invoice", "receipt", "purchase_order", "quote"]);

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
  /** Remove one attachment (photo or doc). The server deletes the stored file
   * too, not just the row — see `api.deleteAssetAttachment`. Gated by the same
   * `asset.attach` permission as uploading (`canAttach`). */
  onDeleted: (attachmentId: number) => void;
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
export function PhotoCapture({
  assetId,
  attachments,
  canAttach,
  onUploaded,
  onDeleted,
}: PhotoCaptureProps) {
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<Attachment | null>(null);

  const handleDelete = async (attachment: Attachment) => {
    setDeletingId(attachment.id);
    setDeleteError(null);
    try {
      await api.deleteAssetAttachment(attachment.id);
      onDeleted(attachment.id);
      setConfirmTarget(null);
    } catch (err) {
      setDeleteError(
        err instanceof ApiError
          ? (err.problem.detail ?? err.problem.title)
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setDeletingId(null);
    }
  };

  const [pending, setPending] = useState<PendingPhoto[]>([]);
  // Applied to the NEXT upload from either picker. Kept as one shared control
  // rather than a per-file prompt: the common flow is "add the invoice", and
  // a modal between picking a file and uploading it would be worse. It stays
  // on the chosen value so several pages of the same invoice can go up in a
  // row without re-picking.
  const [docType, setDocType] = useState<AttachmentDocType>("");
  const photoResetRef = useRef<() => void>(null);
  const docResetRef = useRef<() => void>(null);

  const upload = async (file: File, kind: "photo" | "doc") => {
    const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const previewUrl = kind === "photo" ? URL.createObjectURL(file) : null;
    setPending((prev) => [...prev, { key, kind, previewUrl, fileName: file.name, status: "uploading" }]);

    try {
      const attachment = await api.uploadAssetAttachment(assetId, file, kind, docType);
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
            <Select
              size="xs"
              w={150}
              aria-label="Document type for the next upload"
              data={DOC_TYPE_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
              value={docType}
              onChange={(value) => setDocType((value ?? "") as AttachmentDocType)}
              allowDeselect={false}
              data-testid="attachment-doc-type"
            />
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
          {attachments.map((att) => (
            // `position: relative` wrapper so the remove button can sit on top
            // of the tile itself — the photo tile is an `<Image>` with no room
            // for a child, and the doc tile is an `<a>` (a nested button would
            // be invalid HTML and would swallow the download click).
            <div key={att.id} style={{ position: "relative" }}>
              {att.kind === "photo" ? (
                <Image
                  src={`/media/${att.storage_key}`}
                  alt={att.filename}
                  radius="sm"
                  fit="cover"
                  h={100}
                  data-testid="attachment-photo"
                />
              ) : (
                <Card
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
              )}
              {att.doc_type && (
                <Badge
                  size="xs"
                  variant="filled"
                  color={FINANCIAL_DOC_TYPES.has(att.doc_type) ? "teal" : "gray"}
                  style={{ position: "absolute", bottom: 4, left: 4 }}
                  data-testid={`attachment-doctype-${att.id}`}
                >
                  {DOC_TYPE_LABELS[att.doc_type] ?? att.doc_type}
                </Badge>
              )}
              {canAttach && (
                <ActionIcon
                  variant="filled"
                  color="red"
                  size="sm"
                  radius="xl"
                  loading={deletingId === att.id}
                  aria-label={`Remove ${att.filename}`}
                  onClick={() => setConfirmTarget(att)}
                  style={{ position: "absolute", top: 4, right: 4 }}
                  data-testid={`attachment-delete-${att.id}`}
                >
                  <IconTrash size={14} />
                </ActionIcon>
              )}
            </div>
          ))}
        </SimpleGrid>
      )}

      {deleteError && (
        <Alert color="red" data-testid="attachment-delete-error">
          {deleteError}
        </Alert>
      )}

      <Modal
        opened={confirmTarget !== null}
        onClose={() => setConfirmTarget(null)}
        title="Remove attachment"
        centered
      >
        <Stack gap="sm">
          <Text size="sm">
            Remove <strong>{confirmTarget?.filename}</strong>? The file is deleted from storage
            as well — this cannot be undone.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirmTarget(null)}>
              Cancel
            </Button>
            <Button
              color="red"
              loading={deletingId !== null}
              onClick={() => confirmTarget && void handleDelete(confirmTarget)}
              data-testid="attachment-delete-confirm"
            >
              Remove
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
