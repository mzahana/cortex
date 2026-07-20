import { useEffect, useRef, useState } from "react";
import { Select, type SelectProps } from "@mantine/core";
import { api } from "../../api/client";
import type { Asset } from "../../api/types";

interface AssetPickerSelectProps
  extends Omit<SelectProps, "data" | "value" | "onChange" | "searchValue" | "onSearchChange"> {
  value: Asset | null;
  onChange: (asset: Asset | null) => void;
}

/**
 * Debounced server-side asset search (`GET /api/v1/assets?search=`) for
 * picking the asset to reserve — reservations are durable-asset-only
 * (`ReservationSerializer.validate_asset`), so this only ever searches
 * `is_consumable=false`. Never loads "all assets" (CLAUDE.md): each keystroke
 * (debounced) fetches one bounded page of matches.
 */
export function AssetPickerSelect({ value, onChange, ...rest }: AssetPickerSelectProps) {
  const [searchValue, setSearchValue] = useState(value?.name ?? "");
  const [options, setOptions] = useState<Asset[]>(value ? [value] : []);
  const [loading, setLoading] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setSearchValue(value?.name ?? "");
  }, [value]);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    // Don't re-search when the field just shows the selected asset's own name.
    if (value && searchValue === value.name) return;

    debounceRef.current = setTimeout(() => {
      setLoading(true);
      api
        .listAssets({ search: searchValue || undefined, is_consumable: false, page_size: 15 })
        .then((body) => setOptions(body.results))
        .catch(() => setOptions([]))
        .finally(() => setLoading(false));
    }, 300);

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchValue]);

  // Fetch an initial page once on mount so the dropdown isn't empty before typing.
  useEffect(() => {
    setLoading(true);
    api
      .listAssets({ is_consumable: false, page_size: 15 })
      .then((body) => setOptions((prev) => (prev.length ? prev : body.results)))
      .catch(() => undefined)
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const data = options.map((a) => ({ value: String(a.id), label: a.name }));

  return (
    <Select
      {...rest}
      searchable
      clearable
      data={data}
      value={value ? String(value.id) : null}
      searchValue={searchValue}
      onSearchChange={setSearchValue}
      rightSectionPointerEvents={loading ? "none" : "auto"}
      nothingFoundMessage={loading ? "Searching…" : "No assets found"}
      onChange={(id) => {
        if (!id) {
          onChange(null);
          return;
        }
        const found = options.find((a) => String(a.id) === id) ?? null;
        onChange(found);
      }}
    />
  );
}
