import type { CustomFieldDef } from "../../api/types";

/**
 * Typed display for one `Asset.field_values[def.key]` value against its
 * `CustomFieldDef` (T1.6 acceptance: "detail renders custom fields" —
 * bool/date/enum/unit-suffixed numbers, not a raw JSON dump). Mirrors the
 * types `CustomFieldDataType` supports (`text|int|float|bool|date|enum|json`).
 */
export function formatFieldValue(def: CustomFieldDef, value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";

  switch (def.data_type) {
    case "bool":
      return value ? "Yes" : "No";
    case "date": {
      const parsed = new Date(String(value));
      return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleDateString();
    }
    case "int":
    case "float": {
      const num = Number(value);
      const rendered = Number.isNaN(num) ? String(value) : num.toLocaleString();
      return def.unit ? `${rendered} ${def.unit}` : rendered;
    }
    case "enum":
    case "text":
      return String(value);
    case "json":
    default:
      return typeof value === "string" ? value : JSON.stringify(value);
  }
}

/** Sorts an asset's `field_values` against its category's field defs (by
 * `order`, matching the dynamic-form/admin-editor convention elsewhere),
 * dropping any value whose def isn't found (e.g. the category's fields
 * changed after the value was recorded) rather than rendering a raw,
 * unlabeled key. */
export function orderedFieldEntries(
  fieldDefs: CustomFieldDef[],
  values: Record<string, unknown>,
): { def: CustomFieldDef; value: unknown }[] {
  return [...fieldDefs]
    .sort((a, b) => a.order - b.order || a.id - b.id)
    .filter((def) => Object.prototype.hasOwnProperty.call(values, def.key))
    .map((def) => ({ def, value: values[def.key] }));
}
