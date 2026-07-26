import { api } from "../../api/client";
import { AppLayout } from "../../layout/AppLayout";
import { AssetListView } from "./AssetListView";

/**
 * Asset List (T1.6, docs/api-and-ui.md "Asset List": "Server-side search +
 * filters + tags; virtualized list; card + table views").
 *
 * A thin `AppLayout` wrapper around `AssetListView` (M7 extraction,
 * `docs/tasks/M7-project-grants.md` — the project hub's Assets tab embeds
 * the SAME `AssetListView` against `GET /projects/{id}/assets` instead of
 * this screen's `GET /assets`, so this file no longer owns the filter bar/
 * virtualization logic itself). The "New asset"/"Export CSV" actions that
 * used to live in `AppLayout`'s `actions` slot now render inside
 * `AssetListView`'s own header row instead, since the project hub's Assets
 * tab needs those same actions without its own `AppLayout`.
 */
export function AssetListScreen() {
  return (
    <AppLayout title="Assets">
      <AssetListView fetchAssets={api.listAssets} showProjectFilter />
    </AppLayout>
  );
}
