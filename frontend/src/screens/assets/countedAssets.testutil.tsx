import type { Asset } from "../../api/types";

export function makeAsset(overrides: Partial<Asset> = {}): Asset {
  return {
    id: 10,
    uuid: "uuid-10",
    qr_token: "qr-10",
    category: 1,
    name: "Screwdriver",
    description: "",
    is_consumable: false,
    tracking: "individual",
    quantity: 1,
    project: null,
    serial_number: "",
    manufacturer: "",
    model: "",
    location: null,
    purchase_date: null,
    purchase_cost: null,
    currency: "",
    warranty_expiry: null,
    supplier: "",
    url: "",
    status: "available",
    condition: "",
    retired_at: null,
    current_workload_user: null,
    tags: [],
    field_values: {},
    attachments: [],
    created_at: "2026-07-01T10:00:00Z",
    updated_at: "2026-07-01T10:00:00Z",
    ...overrides,
  };
}

export const ME = {
  id: 1,
  email: "a@b.com",
  name: "A",
  tenant: { id: 1, name: "T", slug: "t" },
  memberships: [],
  permissions: ["asset.create", "asset.edit", "reservation.create", "checkout.manage"],
  project_permissions: {},
};
