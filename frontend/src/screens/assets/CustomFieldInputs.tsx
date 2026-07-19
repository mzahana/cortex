import { NumberInput, Select, Stack, Switch, Textarea, TextInput } from "@mantine/core";
import type { CustomFieldDef } from "../../api/types";

/**
 * Renders one input per `CustomFieldDef`, mapped by `data_type`
 * (T1.7's category-driven dynamic form, docs/data-model.md's
 * `text|int|float|bool|date|enum|json`):
 *
 * - `text`   -> `TextInput`
 * - `int`    -> `NumberInput` (integer step, `unit` shown as a suffix)
 * - `float`  -> `NumberInput` (decimal, `unit` shown as a suffix)
 * - `bool`   -> `Switch`
 * - `date`   -> `TextInput type="date"` (no `@mantine/dates` dependency in
 *   this package yet — a native date input is fully accessible and needs no
 *   new dependency; ISO `YYYY-MM-DD` matches exactly what the server expects,
 *   `apps.assets.services._coerce_value`'s `date.fromisoformat` check)
 * - `enum`   -> `Select` from `enum_options`
 * - `json`   -> `Textarea`, edited as raw text (`jsonDrafts`/`jsonErrors`
 *   below) since a JSON value itself isn't a single form-control primitive;
 *   parsed on every keystroke so a submit can never race a stale parse.
 *
 * Ordered by `def.order` (then `id`), matching the same convention used to
 * render read-only specs on Asset Detail (`assetFieldFormat.orderedFieldEntries`).
 */
export interface CustomFieldInputsProps {
  fieldDefs: CustomFieldDef[];
  values: Record<string, unknown>;
  onChange: (key: string, value: unknown) => void;
  /** Raw textarea text for `json`-type fields, keyed by `def.key` — kept
   * separate from `values` (which holds the last successfully-parsed JSON)
   * so an in-progress, currently-invalid edit doesn't clobber the last good
   * value the form would otherwise submit. */
  jsonDrafts: Record<string, string>;
  onJsonDraftChange: (key: string, raw: string) => void;
  /** Parse error per `json`-type field key (blocks submit while present, see
   * `AssetForm`'s `hasBlockingJsonErrors`). */
  jsonErrors: Record<string, string>;
  /** Server (or client pre-validation) error message per field-def `key`,
   * from `ApiError.problem.errors.custom_field_values` (T1.7's inline
   * server-error surfacing requirement) or the client's own required/type
   * pre-check. */
  errors: Record<string, string>;
  disabled?: boolean;
}

export function CustomFieldInputs({
  fieldDefs,
  values,
  onChange,
  jsonDrafts,
  onJsonDraftChange,
  jsonErrors,
  errors,
  disabled,
}: CustomFieldInputsProps) {
  const sorted = [...fieldDefs].sort((a, b) => a.order - b.order || a.id - b.id);

  return (
    <Stack gap="sm">
      {sorted.map((def) => {
        const value = values[def.key];
        const error = errors[def.key];
        const label = def.required ? `${def.label} *` : def.label;

        switch (def.data_type) {
          case "text":
            return (
              <TextInput
                key={def.key}
                label={label}
                value={typeof value === "string" ? value : ""}
                onChange={(e) => onChange(def.key, e.currentTarget.value)}
                error={error}
                disabled={disabled}
                data-testid={`custom-field-${def.key}`}
              />
            );
          case "int":
            return (
              <NumberInput
                key={def.key}
                label={label}
                suffix={def.unit ? ` ${def.unit}` : undefined}
                allowDecimal={false}
                value={typeof value === "number" ? value : undefined}
                onChange={(v) => onChange(def.key, v === "" ? null : Number(v))}
                error={error}
                disabled={disabled}
                data-testid={`custom-field-${def.key}`}
              />
            );
          case "float":
            return (
              <NumberInput
                key={def.key}
                label={label}
                suffix={def.unit ? ` ${def.unit}` : undefined}
                allowDecimal
                value={typeof value === "number" ? value : undefined}
                onChange={(v) => onChange(def.key, v === "" ? null : Number(v))}
                error={error}
                disabled={disabled}
                data-testid={`custom-field-${def.key}`}
              />
            );
          case "bool":
            return (
              <Switch
                key={def.key}
                label={label}
                checked={value === true}
                onChange={(e) => onChange(def.key, e.currentTarget.checked)}
                error={error}
                disabled={disabled}
                data-testid={`custom-field-${def.key}`}
              />
            );
          case "date":
            return (
              <TextInput
                key={def.key}
                type="date"
                label={label}
                value={typeof value === "string" ? value : ""}
                onChange={(e) => onChange(def.key, e.currentTarget.value || null)}
                error={error}
                disabled={disabled}
                data-testid={`custom-field-${def.key}`}
              />
            );
          case "enum":
            return (
              <Select
                key={def.key}
                label={label}
                data={def.enum_options}
                value={typeof value === "string" ? value : null}
                onChange={(v) => onChange(def.key, v)}
                clearable
                error={error}
                disabled={disabled}
                data-testid={`custom-field-${def.key}`}
              />
            );
          case "json":
            return (
              <Textarea
                key={def.key}
                label={label}
                description="Raw JSON"
                autosize
                minRows={2}
                value={jsonDrafts[def.key] ?? ""}
                onChange={(e) => onJsonDraftChange(def.key, e.currentTarget.value)}
                error={jsonErrors[def.key] || error}
                disabled={disabled}
                data-testid={`custom-field-${def.key}`}
              />
            );
          default:
            return null;
        }
      })}
    </Stack>
  );
}
