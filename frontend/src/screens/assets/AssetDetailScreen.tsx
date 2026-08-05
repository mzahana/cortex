import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Center,
  Group,
  Loader,
  Modal,
  SegmentedControl,
  SimpleGrid,
  Stack,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import {
  ASSET_ATTACH,
  ASSET_EDIT,
  ASSET_RETIRE,
  CHECKOUT_MANAGE,
  LABEL_GENERATE,
  hasAssetPermission,
  RESERVATION_CREATE,
  STOCK_ADJUST,
} from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import { AppLayout } from "../../layout/AppLayout";
import { PrintLabelButton } from "./PrintLabelButton";
import type {
  Asset,
  Attachment,
  Category,
  Checkout,
  CustomFieldDef,
  Location,
  Me,
  Project,
  Reservation,
  StockItem,
} from "../../api/types";
import { orderedFieldEntries, formatFieldValue } from "./assetFieldFormat";
import { STATUS_COLORS, STATUS_LABELS } from "./assetConstants";
import { CreateReservationModal } from "../reservations/CreateReservationModal";
import { ReservationListItem } from "../reservations/ReservationListItem";
import { useReservationList } from "../reservations/useReservationList";
import { AssetReservationMonthCalendar } from "./AssetReservationMonthCalendar";
import { CheckoutModal } from "./CheckoutModal";
import { PhotoCapture } from "./PhotoCapture";

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
 * edit/retire/attach/reserve/check-out/check-in are wired; label/report-issue
 * still belong to later milestones and render as disabled stubs.
 *
 * Reserve reuses T3.4's `CreateReservationModal` pre-filled with this asset
 * (`initialAsset`). Check-out/check-in call the T3.3 checkout endpoints
 * directly (`POST /checkouts`, `POST /checkouts/{id}/checkin`) — `GET
 * /checkouts?asset=<id>&open=true` (post-MVP gap fill) resolves "is there an
 * open checkout for this asset" server-side; this screen still narrows to
 * `user === me.id` client-side since there's no combined `&user=` filter.
 *
 * The "Reservations" section lists every reservation for this asset (`GET
 * /reservations?asset=<id>`, post-MVP gap fill — this section didn't exist
 * before), reusing the same `ReservationListItem` row (with its own
 * check-out/check-in actions) as the Calendar/Approvals screens. It defaults
 * to a Google-Calendar-style month grid (`AssetReservationMonthCalendar`,
 * colored bars spanning each booking's days) with a "List" toggle back to
 * the original flat list for fast exact-time scanning/actions — the month
 * grid is view-only (no approve/reject/cancel from a bar; use List for
 * that), matching the "simple popover, doesn't need a full modal" scope of
 * that feature.
 */
export function AssetDetailScreen() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const routerLocation = useLocation();
  const { me } = useAuth();

  const [asset, setAsset] = useState<Asset | null>(null);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [fieldDefs, setFieldDefs] = useState<CustomFieldDef[]>([]);
  const [category, setCategory] = useState<Category | null>(null);
  const [location, setLocation] = useState<Location | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [retireModalOpen, setRetireModalOpen] = useState(false);
  const [retiring, setRetiring] = useState(false);
  const [retireError, setRetireError] = useState<string | null>(null);

  const [reserveOpen, setReserveOpen] = useState(false);
  const [checkoutOpen, setCheckoutOpen] = useState(false);
  const [myOpenCheckout, setMyOpenCheckout] = useState<Checkout | null>(null);
  const [checkinBusy, setCheckinBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // Consumable assets get a `StockItem` (Feature C, `GET /stock?asset=<id>`)
  // — resolved here so the detail screen can surface "how many do we have"
  // without a trip to the separate Stock screen (the gap this fill closes).
  // `undefined` = not yet resolved (still loading), `null` = resolved but no
  // `StockItem` exists yet (asset predates stock setup, or setup failed).
  const [stockItem, setStockItem] = useState<StockItem | null | undefined>(undefined);
  // Seeded from `AssetFormScreen`'s post-create navigation state (Feature C:
  // "asset created but stock setup failed" — the asset save itself already
  // succeeded, so this surfaces as a banner here rather than losing the
  // success by staying on the form). `location.state` doesn't survive a
  // refresh, which is fine — it's a one-time handoff, not persisted state.
  const [banner, setBanner] = useState<string | null>(
    () => (routerLocation.state as { banner?: string } | null)?.banner ?? null,
  );
  // Seeded alongside `banner` when the form's stock setup created the
  // `StockItem` successfully but the follow-up "receive" txn (initial
  // quantity) failed — lets this screen retry just that one call against the
  // already-known `stockItemId`, instead of the dead end of re-running stock
  // setup (which would now 400 "already has a StockItem").
  const [stockRetry, setStockRetry] = useState<{ stockItemId: number; initialQty: number } | null>(
    () =>
      (routerLocation.state as { stockRetry?: { stockItemId: number; initialQty: number } | null } | null)
        ?.stockRetry ?? null,
  );
  const [stockRetryBusy, setStockRetryBusy] = useState(false);

  const handleRetryStockReceive = async () => {
    if (!stockRetry) return;
    setStockRetryBusy(true);
    try {
      await api.postStockTxn(stockRetry.stockItemId, {
        reason: "receive",
        delta: stockRetry.initialQty,
        ref: "Initial stock (asset setup, retry)",
      });
      setStockRetry(null);
      setBanner("Initial stock quantity set.");
    } catch (err) {
      setBanner(
        err instanceof ApiError
          ? `Setting the initial quantity failed again: ${
              err.problem.detail ?? err.problem.title
            }. You can add stock from the Stock screen.`
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setStockRetryBusy(false);
    }
  };

  const load = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const assetId = Number(id);
      const fetchedAsset = await api.getAsset(assetId);
      setAsset(fetchedAsset);
      setAttachments(fetchedAsset.attachments);

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

      // Resolve "do I currently hold this durable asset checked out" via the
      // `?asset=&open=true` filter (post-MVP gap fill, see module doc
      // comment) — still narrowed to `user === me.id` client-side since
      // there's no combined `&user=` filter.
      if (!fetchedAsset.is_consumable) {
        setStockItem(null);
        try {
          const openCheckouts = await api.listCheckouts({
            asset: fetchedAsset.id,
            open: true,
            page_size: 100,
          });
          const mine = openCheckouts.results.find((c) => c.user === me?.id) ?? null;
          setMyOpenCheckout(mine);
        } catch {
          setMyOpenCheckout(null);
        }
      } else {
        setMyOpenCheckout(null);
        try {
          const stock = await api.listStock({ asset: fetchedAsset.id, page_size: 1 });
          setStockItem(stock.results[0] ?? null);
        } catch {
          // Treated the same as "not tracked yet" — the "Set up stock
          // tracking" prompt is a safe fallback either way.
          setStockItem(null);
        }
      }
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
  }, [id, me?.id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <AppLayout title="Asset" backTo="/assets">
        <Center h="60vh">
          <Loader data-testid="asset-detail-loading" />
        </Center>
      </AppLayout>
    );
  }

  if (error || !asset) {
    return (
      <AppLayout title="Asset" backTo="/assets">
        <Center h="60vh" p="md">
          <Stack align="center" gap="sm" maw={420}>
            <Alert color="red" title="Couldn't load this asset" data-testid="asset-detail-error" w="100%">
              {error ?? "Not found."}
            </Alert>
            <Button onClick={() => navigate("/assets")}>Back to Assets</Button>
          </Stack>
        </Center>
      </AppLayout>
    );
  }

  const canEdit = hasAssetPermission(me, ASSET_EDIT, asset.project);
  const canRetire = hasAssetPermission(me, ASSET_RETIRE, asset.project);
  const canAttach = hasAssetPermission(me, ASSET_ATTACH, asset.project);
  const canReserve = hasAssetPermission(me, RESERVATION_CREATE, asset.project);
  const canCheckout = hasAssetPermission(me, CHECKOUT_MANAGE, asset.project);
  const canPrintLabel = hasAssetPermission(me, LABEL_GENERATE, asset.project);
  const isRetired = asset.status === "retired";
  const isCheckoutEligible = !asset.is_consumable && ["available", "reserved"].includes(asset.status);
  const isCheckedOutByMe = !!myOpenCheckout;

  const handleReservationCreated = (reservation: Reservation) => {
    void reservation;
    setBanner("Reservation requested.");
    void load();
  };

  const handleCheckedOut = (checkout: Checkout) => {
    setMyOpenCheckout(checkout);
    setBanner("Checked out.");
    void load();
  };

  const handleCheckIn = async () => {
    if (!myOpenCheckout) return;
    setCheckinBusy(true);
    setActionError(null);
    try {
      const updated = await api.checkinCheckout(myOpenCheckout.id);
      setMyOpenCheckout(null);
      setBanner("Checked in.");
      void updated;
      void load();
    } catch (err) {
      // A server 403/409 here is a normal, handled outcome (CLAUDE.md) — the
      // client gate above can drift from the server's own scoped/holder check.
      setActionError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setCheckinBusy(false);
    }
  };

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

  const specs = orderedFieldEntries(fieldDefs, asset.field_values);

  return (
    <AppLayout
      title={asset.name}
      backTo="/assets"
      actions={
        <Badge color={STATUS_COLORS[asset.status]} variant="light" style={{ flexShrink: 0 }}>
          {STATUS_LABELS[asset.status]}
        </Badge>
      }
    >
        <Stack gap="md" pb="xl">
          {banner && (
            <Alert color={stockRetry ? "yellow" : "teal"} withCloseButton onClose={() => setBanner(null)}>
              <Stack gap="xs">
                <Text size="sm">{banner}</Text>
                {stockRetry && (
                  <Group justify="flex-end">
                    <Button
                      size="xs"
                      variant="light"
                      loading={stockRetryBusy}
                      onClick={() => void handleRetryStockReceive()}
                      data-testid="asset-detail-stock-retry"
                    >
                      Retry setting initial quantity
                    </Button>
                  </Group>
                )}
              </Stack>
            </Alert>
          )}

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
              {asset.url && (
                <Anchor
                  href={asset.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  size="sm"
                  mt="xs"
                  data-testid="asset-url-link"
                >
                  {asset.url}
                </Anchor>
              )}
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
                {specs.map(({ def, value }) =>
                  def.data_type === "url" && typeof value === "string" && value ? (
                    <Stack key={def.key} gap={0}>
                      <Text size="xs" c="dimmed">
                        {def.label}
                      </Text>
                      <Anchor href={value} target="_blank" rel="noopener noreferrer" size="sm" truncate>
                        {value}
                      </Anchor>
                    </Stack>
                  ) : (
                    <DetailField key={def.key} label={def.label} value={formatFieldValue(def, value)} />
                  ),
                )}
              </SimpleGrid>
            )}
          </Card>

          {asset.is_consumable && (
            <AssetStockCard asset={asset} stockItem={stockItem} me={me} onNavigateToEdit={() => navigate(`/assets/${asset.id}/edit`)} />
          )}

          <Card withBorder>
            <PhotoCapture
              assetId={asset.id}
              attachments={attachments}
              canAttach={canAttach}
              onUploaded={(attachment) => setAttachments((prev) => [attachment, ...prev])}
              onDeleted={(attachmentId) =>
                setAttachments((prev) => prev.filter((a) => a.id !== attachmentId))
              }
            />
          </Card>

          {me && <AssetReservationsCard assetId={asset.id} asset={asset} me={me} />}

          <Card withBorder>
            <Title order={6} mb="xs">
              History
            </Title>
            <Text size="sm" c="dimmed" data-testid="history-placeholder">
              Checkout/maintenance history lands in a later milestone — this
              section is a placeholder per T1.6 (reservation history now has
              its own section above).
            </Text>
          </Card>

          <Card withBorder>
            <Title order={6} mb="xs">
              Actions
            </Title>
            {actionError && (
              <Alert color="red" mb="xs" data-testid="asset-action-error">
                {actionError}
              </Alert>
            )}
            <Group gap="xs" wrap="wrap">
              <PrintLabelButton assetId={asset.id} disabled={!canPrintLabel} />

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

              {!asset.is_consumable && canReserve ? (
                <Button size="sm" variant="default" onClick={() => setReserveOpen(true)} data-testid="reserve-action">
                  Reserve
                </Button>
              ) : (
                <Tooltip
                  label={
                    asset.is_consumable
                      ? "Consumable assets can't be reserved"
                      : "You don't have permission to reserve this asset"
                  }
                >
                  <Button size="sm" variant="default" disabled>
                    Reserve
                  </Button>
                </Tooltip>
              )}

              {isCheckedOutByMe ? (
                canCheckout ? (
                  <Button
                    size="sm"
                    variant="filled"
                    color="teal"
                    loading={checkinBusy}
                    onClick={() => void handleCheckIn()}
                    data-testid="checkin-action"
                  >
                    Check in
                  </Button>
                ) : (
                  <Tooltip label="You don't have permission to check in this asset">
                    <Button size="sm" variant="filled" color="teal" disabled>
                      Check in
                    </Button>
                  </Tooltip>
                )
              ) : canCheckout && isCheckoutEligible ? (
                <Button size="sm" variant="default" onClick={() => setCheckoutOpen(true)} data-testid="checkout-action">
                  Check out
                </Button>
              ) : (
                <Tooltip
                  label={
                    asset.is_consumable
                      ? "Consumable assets can't be checked out"
                      : !isCheckoutEligible
                        ? `Asset is '${asset.status}' and can't be checked out right now`
                        : "You don't have permission to check out this asset"
                  }
                >
                  <Button size="sm" variant="default" disabled>
                    Check out
                  </Button>
                </Tooltip>
              )}

              <StubAction label="Generate label" />
              <StubAction label="Report issue" />
            </Group>
          </Card>
        </Stack>
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

      <CreateReservationModal
        opened={reserveOpen}
        onClose={() => setReserveOpen(false)}
        onCreated={handleReservationCreated}
        initialAsset={asset}
      />

      <CheckoutModal
        opened={checkoutOpen}
        onClose={() => setCheckoutOpen(false)}
        onCheckedOut={handleCheckedOut}
        asset={asset}
      />
    </AppLayout>
  );
}

/**
 * Asset Detail's "Reservations" section (Feature B, post-MVP gap fill — this
 * screen previously had no reservation history/list at all). `GET
 * /reservations?asset=<id>` ordered newest-start-first so upcoming/active
 * bookings surface above past ones; reuses `ReservationListItem` (with its
 * own approve/reject/cancel/check-out/check-in actions) so behavior stays in
 * lockstep with the Calendar/Approvals screens rather than a second
 * implementation.
 */
function AssetReservationsCard({ assetId, asset, me }: { assetId: number; asset: Asset; me: Me }) {
  const [view, setView] = useState<"calendar" | "list">("calendar");
  const filters = useMemo(() => ({ asset: assetId, ordering: "-start_at" as const }), [assetId]);
  const { items, totalCount, loading, error, reload } = useReservationList({
    filters,
    // The month grid does its own fetching (scoped to its own visible-month
    // window) — skip this flat-list fetch entirely while it's hidden rather
    // than running two overlapping `GET /reservations` calls for the same
    // card.
    enabled: view === "list",
  });

  return (
    <Card withBorder>
      <Group justify="space-between" mb="xs">
        <Title order={6}>Reservations</Title>
        <SegmentedControl
          size="xs"
          value={view}
          onChange={(v) => setView(v as "calendar" | "list")}
          data={[
            { label: "Calendar", value: "calendar" },
            { label: "List", value: "list" },
          ]}
        />
      </Group>

      {view === "calendar" && <AssetReservationMonthCalendar assetId={assetId} asset={asset} me={me} />}

      {view === "list" && (
        <>
          {error && (
            <Alert color="red" mb="xs">
              <Group justify="space-between">
                <Text size="sm">{error}</Text>
                <Button size="xs" variant="light" onClick={reload}>
                  Retry
                </Button>
              </Group>
            </Alert>
          )}
          {loading && !error && (
            <Center p="md">
              <Loader size="sm" data-testid="asset-reservations-loading" />
            </Center>
          )}
          {!loading && !error && items.length === 0 && (
            <Text size="sm" c="dimmed">
              No reservations for this asset yet.
            </Text>
          )}
          {!loading && !error && items.length > 0 && (
            <Stack gap="xs">
              {items.map((r) => (
                <ReservationListItem key={r.id} reservation={r} asset={asset} me={me} onChanged={() => reload()} />
              ))}
            </Stack>
          )}
          {totalCount !== null && totalCount > items.length && (
            <Text size="xs" c="dimmed" mt="xs">
              Showing {items.length} of {totalCount}.
            </Text>
          )}
        </>
      )}
    </Card>
  );
}

/**
 * Asset Detail's "Stock" section (Feature C follow-up, post-MVP gap fill —
 * `AssetForm` already lets you set up a consumable's `StockItem`, but this
 * screen never showed the resulting quantity anywhere). Mirrors the
 * low-stock visual treatment from `screens/stock/StockRows.tsx`'s `StockRow`
 * (`quantity_on_hand <= reorder_threshold` — same comparison the server's
 * `?low_stock=true` filter uses) so a consumable looks the same here as it
 * does on the dedicated Stock screen. When no `StockItem` exists yet, this
 * renders a "not tracked yet" prompt linking to the edit screen's existing
 * "Set up stock tracking" flow (`AssetForm`) rather than duplicating that
 * flow here.
 */
function AssetStockCard({
  asset,
  stockItem,
  me,
  onNavigateToEdit,
}: {
  asset: Asset;
  stockItem: StockItem | null | undefined;
  me: Me | null;
  onNavigateToEdit: () => void;
}) {
  const navigate = useNavigate();
  const canManageStock = hasAssetPermission(me, STOCK_ADJUST, asset.project);
  const isLoading = stockItem === undefined;
  const isLowStock = stockItem != null && stockItem.quantity_on_hand <= stockItem.reorder_threshold;

  return (
    <Card withBorder data-testid="asset-stock-card">
      <Group justify="space-between" mb="xs">
        <Title order={6}>Stock</Title>
        {isLowStock && (
          <Badge color="red" data-testid="asset-stock-low-badge">
            Low stock
          </Badge>
        )}
      </Group>

      {isLoading && (
        <Center p="md">
          <Loader size="sm" data-testid="asset-stock-loading" />
        </Center>
      )}

      {!isLoading && stockItem === null && (
        <Stack gap="xs" align="flex-start">
          <Text size="sm" c="dimmed" data-testid="asset-stock-not-tracked">
            Not tracked yet — this consumable has no stock record, so quantity on hand isn't known.
          </Text>
          {canManageStock ? (
            <Button size="xs" variant="light" onClick={onNavigateToEdit} data-testid="asset-stock-setup-link">
              Set up stock tracking
            </Button>
          ) : (
            <Text size="xs" c="dimmed">
              You don&apos;t have permission to set up stock tracking for this asset.
            </Text>
          )}
        </Stack>
      )}

      {!isLoading && stockItem != null && (
        <Stack gap={6}>
          <Group gap="lg" wrap="wrap">
            <Text size="sm">
              On hand: <strong>{stockItem.quantity_on_hand}</strong> {stockItem.unit_of_measure}
            </Text>
            <Text size="sm" c="dimmed">
              Reorder at {stockItem.reorder_threshold}, target {stockItem.reorder_target}
            </Text>
          </Group>
          <Button
            size="xs"
            variant="subtle"
            onClick={() => navigate("/stock")}
            style={{ alignSelf: "flex-start" }}
            data-testid="asset-stock-manage-link"
          >
            Manage stock (receive / consume / adjust)
          </Button>
        </Stack>
      )}
    </Card>
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

