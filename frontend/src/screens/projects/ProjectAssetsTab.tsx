import { useCallback } from "react";
import { api } from "../../api/client";
import type { AssetListParams } from "../../api/types";
import { AssetListView } from "../assets/AssetListView";

interface ProjectAssetsTabProps {
  projectId: number;
}

/**
 * Project hub — Assets tab (`docs/tasks/M7-project-grants.md`: "the Assets
 * tab must reuse the existing asset list UI/component filtered to the
 * project (`GET /projects/{id}/assets`), not a reimplementation"). This is a
 * thin wrapper around `AssetListView` (the SAME component `AssetListScreen`
 * uses for the full `/assets` list) with its fetch bound to `GET
 * /projects/{id}/assets` instead — identical filter bar, virtualization,
 * card/table rendering, "New asset" / "Export CSV" gating, nothing
 * reimplemented.
 */
export function ProjectAssetsTab({ projectId }: ProjectAssetsTabProps) {
  const fetchAssets = useCallback(
    (params: AssetListParams) => api.listProjectAssets(projectId, params),
    [projectId],
  );

  return (
    <AssetListView
      fetchAssets={fetchAssets}
      showProjectFilter={false}
      fixedProjectId={projectId}
      containerHeight="calc(100vh - 260px)"
    />
  );
}
