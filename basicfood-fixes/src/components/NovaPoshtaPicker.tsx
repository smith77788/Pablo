import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Loader2, MapPin, Package, Search } from "lucide-react";
import { supabase } from "@/integrations/supabase/client";

/**
 * NovaPoshtaPicker — debounced autocomplete для міст і відділень НП.
 *
 * Чому власний компонент, а не Combobox:
 *  - Required-валідація браузерної форми працює тільки на справжньому <input>.
 *  - Користувач має бачити поточне значення у звичайному текстовому полі
 *    (для зручної ручної правки на мобільному).
 *
 * Дзвонок на edge-функцію nova-poshta-search кешується там 5 хв.
 * Локально дебаунс 300ms — щоб не бомбити при кожному натисканні клавіші.
 *
 * Контролери:
 *  - city / setCity / cityRef — зберігаємо як string, ref не критичний (НП
 *    приймає назву міста для warehouses-методу).
 *  - warehouse / setWarehouse — людино-читаний рядок, який летить у
 *    delivery_address замовлення. UI показує "№X — адреса".
 */
type City = { ref: string; name: string; area: string };
type Warehouse = { ref: string; name: string; number: string; address: string };

interface Props {
  city: string;
  warehouse: string;
  deliveryType: "branch" | "parcel_locker";
  onCityChange: (next: string) => void;
  onWarehouseChange: (next: string) => void;
  cityLabel?: string;
  warehouseLabel?: string;
  cityPlaceholder?: string;
  warehousePlaceholder?: string;
  required?: boolean;
}

const useDebouncedValue = <T,>(value: T, delay = 300): T => {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), delay);
    return () => clearTimeout(id);
  }, [value, delay]);
  return v;
};

const NovaPoshtaPicker = ({
  city,
  warehouse,
  deliveryType,
  onCityChange,
  onWarehouseChange,
  cityLabel,
  warehouseLabel,
  cityPlaceholder,
  warehousePlaceholder,
  required,
}: Props) => {
  const { t } = useTranslation();
  const [cityFocus, setCityFocus] = useState(false);
  const [whFocus, setWhFocus] = useState(false);
  const [cities, setCities] = useState<City[]>([]);
  const [warehouses, setWarehouses] = useState<Warehouse[]>([]);
  const [loadingCities, setLoadingCities] = useState(false);
  const [loadingWh, setLoadingWh] = useState(false);
  // Track which city the user *confirmed* (clicked from suggestion list).
  // Without this, typing "Київ" lets us search warehouses immediately, but
  // we lose the ability to say "this isn't a real city" — UX trade-off in
  // favor of speed.
  const confirmedCityRef = useRef<string>(city);

  const dCity = useDebouncedValue(city, 300);
  const dWh = useDebouncedValue(warehouse, 250);

  // Suggestions: cities ──────────────────────────────────────────────────
  useEffect(() => {
    if (!cityFocus) return;
    if (!dCity || dCity.length < 2) {
      setCities([]);
      return;
    }
    let cancelled = false;
    setLoadingCities(true);
    supabase.functions
      .invoke<{ items: City[] }>("nova-poshta-search", {
        body: { action: "cities", query: dCity },
      })
      .then(({ data }) => {
        if (cancelled) return;
        setCities(data?.items ?? []);
      })
      .catch(() => !cancelled && setCities([]))
      .finally(() => !cancelled && setLoadingCities(false));
    return () => {
      cancelled = true;
    };
  }, [dCity, cityFocus]);

  // Suggestions: warehouses ─────────────────────────────────────────────
  useEffect(() => {
    if (!whFocus) return;
    const cityName = confirmedCityRef.current || city;
    if (!cityName || cityName.length < 2) {
      setWarehouses([]);
      return;
    }
    let cancelled = false;
    setLoadingWh(true);
    supabase.functions
      .invoke<{ items: Warehouse[] }>("nova-poshta-search", {
        body: { action: "warehouses", cityName, query: dWh, type: deliveryType },
      })
      .then(({ data }) => {
        if (cancelled) return;
        setWarehouses(data?.items ?? []);
      })
      .catch(() => !cancelled && setWarehouses([]))
      .finally(() => !cancelled && setLoadingWh(false));
    return () => {
      cancelled = true;
    };
  }, [dWh, whFocus, deliveryType, city]);

  const pickCity = (c: City) => {
    onCityChange(c.name);
    confirmedCityRef.current = c.name;
    setCities([]);
    setCityFocus(false);
    // Clear stale warehouse — region change means old warehouse string is wrong.
    if (warehouse) onWarehouseChange("");
  };

  const pickWarehouse = (w: Warehouse) => {
    // Compose human-friendly value: "№3 — вул. Слобожанська, 13"
    const label = `№${w.number} — ${w.address.replace(/^.*?,\s*/, "")}`;
    onWarehouseChange(label);
    setWarehouses([]);
    setWhFocus(false);
  };

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
      {/* CITY ───────────────────────────────────────────── */}
      <div className="space-y-1 relative">
        <Label htmlFor="np-city">{cityLabel || t("checkout.city")} *</Label>
        <div className="relative">
          <MapPin className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
          <Input
            id="np-city"
            value={city}
            onChange={(e) => onCityChange(e.target.value)}
            onFocus={() => setCityFocus(true)}
            onBlur={() => setTimeout(() => setCityFocus(false), 150)}
            placeholder={cityPlaceholder || t("checkout.city_placeholder")}
            autoComplete="address-level2"
            required={required}
            className="pl-9"
          />
          {loadingCities && (
            <Loader2 className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground animate-spin" />
          )}
        </div>
        {cityFocus && cities.length > 0 && (
          <ul className="absolute z-50 left-0 right-0 top-full mt-1 max-h-60 overflow-y-auto bg-popover border border-border rounded-md shadow-lg">
            {cities.map((c) => (
              <li
                key={c.ref}
                onMouseDown={(e) => {
                  e.preventDefault();
                  pickCity(c);
                }}
                className="px-3 py-2 cursor-pointer hover:bg-accent text-sm flex items-center justify-between gap-2"
              >
                <span className="truncate">
                  <MapPin className="inline w-3 h-3 mr-1 text-muted-foreground" />
                  {c.name}
                </span>
                {c.area && (
                  <span className="text-[10px] text-muted-foreground shrink-0">{c.area}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* WAREHOUSE ───────────────────────────────────────── */}
      <div className="space-y-1 relative">
        <Label htmlFor="np-warehouse">
          {warehouseLabel ||
            (deliveryType === "parcel_locker"
              ? t("checkout.delivery_parcel_locker_warehouse")
              : t("checkout.delivery_branch_warehouse"))}{" "}
          *
        </Label>
        <div className="relative">
          {deliveryType === "parcel_locker" ? (
            <Package className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
          ) : (
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
          )}
          <Input
            id="np-warehouse"
            value={warehouse}
            onChange={(e) => onWarehouseChange(e.target.value)}
            onFocus={() => setWhFocus(true)}
            onBlur={() => setTimeout(() => setWhFocus(false), 150)}
            placeholder={
              warehousePlaceholder ||
              (deliveryType === "parcel_locker"
                ? t("checkout.delivery_parcel_locker_placeholder")
                : t("checkout.delivery_branch_placeholder"))
            }
            autoComplete="street-address"
            required={required}
            disabled={!city}
            className="pl-9"
          />
          {loadingWh && (
            <Loader2 className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground animate-spin" />
          )}
        </div>
        {whFocus && warehouses.length > 0 && (
          <ul className="absolute z-30 left-0 right-0 top-full mt-1 max-h-72 overflow-y-auto bg-popover border border-border rounded-md shadow-lg">
            {warehouses.map((w) => (
              <li
                key={w.ref}
                onMouseDown={(e) => {
                  e.preventDefault();
                  pickWarehouse(w);
                }}
                className="px-3 py-2 cursor-pointer hover:bg-accent text-sm border-b border-border/50 last:border-0"
              >
                <div className="flex items-baseline gap-2">
                  <span className="font-semibold text-primary text-xs">№{w.number}</span>
                  <span className="text-xs text-muted-foreground truncate">
                    {w.address.replace(/^.*?,\s*/, "")}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        )}
        {whFocus && !loadingWh && warehouses.length === 0 && city && (
          <p className="absolute top-full mt-1 text-[11px] text-muted-foreground">
            {dWh ? "Нічого не знайдено" : "Почніть вводити номер або адресу"}
          </p>
        )}
      </div>
    </div>
  );
};

export default NovaPoshtaPicker;
