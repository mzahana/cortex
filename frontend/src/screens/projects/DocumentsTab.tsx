import { useCallback, useEffect, useRef, useState } from "react";
import {
  ActionIcon,
  Alert,
  Anchor,
  Button,
  Center,
  FileButton,
  Group,
  Loader,
  Select,
  Stack,
  Table,
  Text,
  Tooltip,
} from "@mantine/core";
import { IconLock } from "@tabler/icons-react";
import { api, ApiError } from "../../api/client";
import { PROJECT_MANAGE, hasProjectScopedPermission } from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import type { ProjectDocument, ProjectDocumentKind } from "../../api/types";
import { ConfirmDeleteModal } from "../../components/ConfirmDeleteModal";

const KIND_OPTIONS: { value: ProjectDocumentKind; label: string }[] = [
  { value: "proposal", label: "Proposal" },
  { value: "contract", label: "Contract" },
  { value: "progress_report", label: "Progress report" },
  { value: "other", label: "Other" },
];

const KIND_LABELS: Record<ProjectDocumentKind, string> = Object.fromEntries(
  KIND_OPTIONS.map((o) => [o.value, o.label]),
) as Record<ProjectDocumentKind, string>;

interface DocumentsTabProps {
  projectId: number;
}

/**
 * Project hub — Documents tab (`docs/tasks/M7-project-grants.md`:
 * "proposal/contract/progress_report/other"). **Reads are gated by
 * project-scoped `expense.view`, NOT `project.view`** (product decision,
 * `apps.projects.permissions._action_permission_key` doc comment: proposals/
 * contracts routinely restate the exact budget figures redacted elsewhere)
 * — a caller without it gets a 403 on the WHOLE sub-resource, rendered here
 * as the same "no access" state the Expenses/Overview financials use, never
 * an empty list (which would incorrectly imply "there just aren't any
 * documents"). Writes (upload/delete) are gated by `project.manage`.
 */
export function DocumentsTab({ projectId }: DocumentsTabProps) {
  const { me } = useAuth();
  const canManage = hasProjectScopedPermission(me, PROJECT_MANAGE, projectId);

  const [items, setItems] = useState<ProjectDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  const [uploadKind, setUploadKind] = useState<ProjectDocumentKind>("other");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const resetFileRef = useRef<() => void>(null);

  const [deleteTarget, setDeleteTarget] = useState<ProjectDocument | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setForbidden(false);
    try {
      const body = await api.listProjectDocuments(projectId, { page_size: 100 });
      setItems(body.results);
    } catch (err) {
      if (err instanceof ApiError && err.isForbidden) {
        setForbidden(true);
        setError("You don't have access to this project's documents.");
      } else {
        setError(
          err instanceof ApiError
            ? err.problem.detail ?? err.problem.title
            : "Unable to reach the server. Please try again.",
        );
      }
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleUpload = async (file: File | null) => {
    if (!file) return;
    setUploadError(null);
    setUploading(true);
    try {
      const doc = await api.uploadProjectDocument(projectId, file, uploadKind, "doc");
      setItems((prev) => [doc, ...prev]);
    } catch (err) {
      setUploadError(
        err instanceof ApiError ? err.problem.detail ?? err.problem.title : "Upload failed. Please try again.",
      );
    } finally {
      setUploading(false);
      resetFileRef.current?.();
    }
  };

  return (
    <Stack gap="sm" data-testid="documents-tab">
      {canManage && (
        <Group align="flex-end">
          <Select
            label="Document type"
            data={KIND_OPTIONS}
            value={uploadKind}
            onChange={(v) => v && setUploadKind(v as ProjectDocumentKind)}
            allowDeselect={false}
            w={200}
          />
          <FileButton resetRef={resetFileRef} onChange={handleUpload} accept="application/pdf,.doc,.docx,image/*">
            {(props) => (
              <Button loading={uploading} data-testid="upload-document-button" {...props}>
                Upload document
              </Button>
            )}
          </FileButton>
        </Group>
      )}

      {uploadError && (
        <Alert color="red" data-testid="document-upload-error">
          {uploadError}
        </Alert>
      )}

      {error && (
        <Alert
          color={forbidden ? "gray" : "red"}
          icon={forbidden ? <IconLock size={16} /> : undefined}
          title={forbidden ? "Not available" : "Couldn't load documents"}
          data-testid="documents-error"
        >
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{error}</Text>
            {!forbidden && (
              <Button size="xs" variant="light" onClick={() => void load()}>
                Retry
              </Button>
            )}
          </Stack>
        </Alert>
      )}

      {loading && !error && (
        <Center p="xl">
          <Loader data-testid="documents-loading" />
        </Center>
      )}

      {!loading && !error && items.length === 0 && (
        <Center p="xl">
          <Text c="dimmed">No documents uploaded yet.</Text>
        </Center>
      )}

      {!loading && !error && items.length > 0 && (
        <Table.ScrollContainer minWidth={480}>
          <Table verticalSpacing="xs" data-testid="documents-table">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>File</Table.Th>
                <Table.Th>Kind</Table.Th>
                <Table.Th>Uploaded</Table.Th>
                {canManage && <Table.Th />}
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {items.map((doc) => (
                <Table.Tr key={doc.id} data-testid={`document-row-${doc.id}`}>
                  <Table.Td>
                    <Anchor href={`/media/${doc.storage_key}`} target="_blank" rel="noopener noreferrer">
                      {doc.filename}
                    </Anchor>
                  </Table.Td>
                  <Table.Td>{KIND_LABELS[doc.kind]}</Table.Td>
                  <Table.Td>{new Date(doc.created_at).toLocaleDateString()}</Table.Td>
                  {canManage && (
                    <Table.Td>
                      <Tooltip label="Delete">
                        <ActionIcon
                          variant="subtle"
                          size="sm"
                          color="red"
                          aria-label={`Delete ${doc.filename}`}
                          onClick={() => setDeleteTarget(doc)}
                        >
                          🗑
                        </ActionIcon>
                      </Tooltip>
                    </Table.Td>
                  )}
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}

      {deleteTarget && (
        <ConfirmDeleteModal
          opened={!!deleteTarget}
          title="Delete document"
          itemLabel={deleteTarget.filename}
          onClose={() => setDeleteTarget(null)}
          onConfirm={async () => {
            await api.deleteProjectDocument(deleteTarget.id);
          }}
          onDeleted={() => void load()}
        />
      )}
    </Stack>
  );
}
