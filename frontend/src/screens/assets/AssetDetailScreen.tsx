import { useCallback, useEffect, useRef, useState } from "react";
import {
  ActionIcon,
  Alert,
  AppShell,
  Badge,
  Button,
  Card,
  Center,
  FileButton,
  Group,
  Image,
  Loader,
  Modal,
  SimpleGrid,
  Stack,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import {
  ASSET_ATTACH,
  ASSET_EDIT,
  ASSET_RETIRE,
  hasAssetPermission,
} from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import type { Asset, Category, CustomFieldDef, Location, Project } from "../../api/types";
import { orderedFieldEntries, formatFieldValue } from "./assetFieldFormat";
import { STATUS_COLORS, STATUS_LABELS } from "./assetConstants";

/**
 * Asset Detail (T1.6, docs/api-and-ui.md "Asset Detail": "Specs (custom
 * fields), photos, status, location, history; actions: reserve, check-out/
 * in, edit, attach photo, generate label, report issue").
 *
 * Custom-field specs are rendered against the category's live
 * `CustomFieldDef` list (`GET /categories/{id}/fields`) — `Asset.field_values`
 * alone only carries already-typed raw values keyed by field `key`, not the
 * label/unit/order metadata needed to display them properly.
 *
 * Action buttons are gated by `hasAssetPermission` (presentation-only,
 * CLAUDE.md/rbac.md §1 — a server 403 is still a normal, handled outcome):
 * edit/retire/attach are THIS milestone's actions; reserve/checkout/label/
 * report-issue belong to later milestones and render as disabled stubs.
 */
export function AssetDetailScreen() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { me } = useAuth();

  const [asset, setAsset] = useState<Asset | null>(null);
  const [fieldDefs, setFieldDefs] = useState<CustomFieldDef[]>([]);
  const [category, setCategory] = useState<Category | null>(null);
  const [location, setLocation] = useState<Location | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [retireModalOpen, setRetireModalOpen] = useState(false);
  const [retiring, setRetiring] = useState(false);
  const [retireError, setRetireError] = useState<string | null>(null);

  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const resetFileRef = useRef<() => void>(null);

  const load = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const assetId = Number(id);
      const fetchedAsset = await api.getAsset(assetId);
      setAsset(fetchedAsset);

      const [fetchedCategory, defs, fetchedLocation, fetchedProject] = await Promise.all([
        api.getCategory(fetchedAsset.category).catch(() => null),
        api.listCategoryFields(fetchedAsset.category).catch(() => []),
        fetchedAsset.location ? api.getLocation(fetchedAsset.location).catch(() => null) : Promise.resolve(null),
        fetchedAsset.project ? api.getProject(fetchedAsset.project).catch(() => null) : Promise.resolve(null),
      ]);
      setCategory(fetchedCategory);
      setFieldDefs(defs);
      setLocation(fetchedLocation);
      setProject(fetchedProject);
    } catch (err) {
      setAsset(null);
      setError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <Center h="100vh">
        <Loader data-testid="asset-detail-loading" />
      </Center>
    );
  }

  if (error || !asset) {
    return (
      <Center h="100vh" p="md">
        <Stack align="center" gap="sm" maw={420}>
          <Alert color="red" title="Couldn't load this asset" data-testid="asset-detail-error" w="100%">
            {error ?? "Not found."}
          </Alert>
          <Button onClick={() => navigate("/assets")}>Back to Assets</Button>
        </Stack>
      </Center>
    );
  }

  const canEdit = hasAssetPermission(me, ASSET_EDIT, asset.project);
  const canRetire = hasAssetPermission(me, ASSET_RETIRE, asset.project);
  const canAttach = hasAssetPermission(me, ASSET_ATTACH, asset.project);
  const isRetired = asset.status === "retired";

  const handleRetire = async () => {
    setRetiring(true);
    setRetireError(null);
    try {
      const updated = await api.retireAsset(asset.id);
      setAsset(updated);
      setRetireModalOpen(false);
    } catch (err) {
      // A server 403 here is a normal, handled outcome (CLAUDE.md) — the
      // client gate above can drift from the server's own scoped check.
      setRetireError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setRetiring(false);
    }
  };

  const handleUpload = async (file: File | null) => {
    if (!file) return;
    setUploading(true);
    setUploadError(null);
    try {
      await api.uploadAssetAttachment(asset.id, file, "photo");
      await load();
    } catch (err) {
      setUploadError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Upload failed. Please try again.",
      );
    } finally {
      setUploading(false);
      resetFileRef.current?.();
    }
  };

  const specs = orderedFieldEntries(fieldDefs, asset.field_values);

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
            <ActionIcon variant="subtle" aria-label="Back" onClick={() => navigate("/assets")}>
              &#8592;
            </ActionIcon>
            <Title order={4} lineClamp={1}>
              {asset.name}
            </Title>
          </Group>
          <Badge color={STATUS_COLORS[asset.status]} variant="light" style={{ flexShrink: 0 }}>
            {STATUS_LABELS[asset.status]}
          </Badge>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Stack gap="md" pb="xl">
          <Card withBorder>
            <Stack gap={4}>
              <Text size="xs" c="dimmed">
                {category?.name ?? `Category #${asset.category}`}
                {asset.is_consumable ? " · Consumable" : " · Durable"}
              </Text>
              {asset.description && <Text size="sm">{asset.description}</Text>}
              <SimpleGrid cols={{ base: 2, sm: 3 }} spacing="xs" mt="xs">
                <DetailField label="Serial #" value={asset.serial_number || "—"} />
                <DetailField label="Manufacturer" value={asset.manufacturer || "—"} />
                <DetailField label="Model" value={asset.model || "—"} />
                <DetailField label="Location" value={location?.name ?? (asset.location ? `#${asset.location}` : "—")} />
                <DetailField label="Project" value={project?.name ?? (asset.project ? `#${asset.project}` : "General pool")} />
                <DetailField
                  label="Workload holder"
                  value={asset.current_workload_user ? `User #${asset.current_workload_user}` : "—"}
                />
                <DetailField label="Purchase date" value={asset.purchase_date ?? "—"} />
                <DetailField
                  label="Purchase cost"
                  value={
                    asset.purchase_cost
                      ? `${asset.currency || ""} ${asset.purchase_cost}`.trim()
                      : "—"
                  }
                />
                <DetailField label="Warranty expiry" value={asset.warranty_expiry ?? "—"} />
              </SimpleGrid>
              {asset.condition && (
                <Text size="xs" c="dimmed" mt="xs">
                  Condition notes: {asset.condition}
                </Text>
              )}
              {asset.tags.length > 0 && (
                <Group gap={4} mt="xs" wrap="wrap">
                  {asset.tags.map((tag) => (
                    <Badge key={tag} size="xs" variant="dot" color="grape">
                      {tag}
                    </Badge>
                  ))}
                </Group>
              )}
            </Stack>
          </Card>

          <Card withBorder>
            <Title order={6} mb="xs">
              Specs
            </Title>
            {specs.length === 0 ? (
              <Text size="sm" c="dimmed">
                No custom-field values recorded for this asset.
              </Text>
            ) : (
              <SimpleGrid cols={{ base: 2, sm: 3 }} spacing="xs">
                {specs.map(({ def, value }) => (
                  <DetailField key={def.key} label={def.label} value={formatFieldValue(def, value)} />
                ))}
              </SimpleGrid>
            )}
          </Card>

          <Card withBorder>
            <Group justify="space-between" mb="xs">
              <Title order={6}>Photos &amp; attachments</Title>
              {canAttach ? (
                <FileButton
                  resetRef={resetFileRef}
                  onChange={(file) => void handleUpload(file)}
                  accept="image/png,image/jpeg,image/webp,application/pdf"
                >
                  {(props) => (
                    <Button size="xs" variant="light" loading={uploading} {...props}>
                      Add photo
                    </Button>
                  )}
                </FileButton>
              ) : (
                <Tooltip label="You don't have permission to attach files to this asset">
                  <Button size="xs" variant="light" disabled>
                    Add photo
                  </Button>
                </Tooltip>
              )}
            </Group>
            {uploadError && (
              <Alert color="red" mb="xs" data-testid="attachment-upload-error">
                {uploadError}
              </Alert>
            )}
            {asset.attachments.length === 0 ? (
              <Text size="sm" c="dimmed">
                No photos or documents yet.
              </Text>
            ) : (
              <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="xs">
                {asset.attachments.map((att) =>
                  att.kind === "photo" ? (
                    <Image
                      key={att.id}
                      src={`/media/${att.storage_key}`}
                      alt={att.filename}
                      radius="sm"
                      fit="cover"
                      h={100}
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
          </Card>

          <Card withBorder>
            <Title order={6} mb="xs">
              History
            </Title>
            <Text size="sm" c="dimmed" data-testid="history-placeholder">
              Checkout/reservation/maintenance history lands in later
              milestones (M2–M3) — this section is a placeholder per T1.6.
            </Text>
          </Card>

          <Card withBorder>
            <Title order={6} mb="xs">
              Actions
            </Title>
            <Group gap="xs" wrap="wrap">
              {canEdit ? (
                <Button size="sm" variant="default" onClick={() => navigate(`/assets/${asset.id}/edit`)}>
                  Edit
                </Button>
              ) : (
                <Tooltip label="You don't have permission to edit this asset">
                  <Button size="sm" variant="default" disabled>
                    Edit
                  </Button>
                </Tooltip>
              )}

              {canRetire && !isRetired ? (
                <Button size="sm" color="red" variant="light" onClick={() => setRetireModalOpen(true)}>
                  Retire / mark lost
                </Button>
              ) : (
                <Tooltip
                  label={
                    isRetired
                      ? "Already retired"
                      : "You don't have permission to retire this asset"
                  }
                >
                  <Button size="sm" color="red" variant="light" disabled>
                    Retire / mark lost
                  </Button>
                </Tooltip>
              )}

              <StubAction label="Reserve" />
              <StubAction label="Check out" />
              <StubAction label="Generate label" />
              <StubAction label="Report issue" />
            </Group>
          </Card>
        </Stack>
      </AppShell.Main>

      <Modal opened={retireModalOpen} onClose={() => setRetireModalOpen(false)} title="Retire asset" centered>
        {retireError && (
          <Alert color="red" mb="sm">
            {retireError}
          </Alert>
        )}
        <Text size="sm" mb="md">
          Retire <strong>{asset.name}</strong>? It will be hidden from the default asset list but its record is
          retained.
        </Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={() => setRetireModalOpen(false)}>
            Cancel
          </Button>
          <Button color="red" loading={retiring} onClick={() => void handleRetire()}>
            Retire
          </Button>
        </Group>
      </Modal>
    </AppShell>
  );
}

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <Stack gap={0}>
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <Text size="sm">{value}</Text>
    </Stack>
  );
}

/** Later-milestone action (reserve/checkout/label/issue) — present but
 * always disabled, with a "coming soon" tooltip (T1.6 requirement),
 * regardless of the viewer's permissions (there is nothing to gate yet). */
function StubAction({ label }: { label: string }) {
  return (
    <Tooltip label="Coming in a later milestone">
      <Button size="sm" variant="default" disabled>
        {label}
      </Button>
    </Tooltip>
  );
}

