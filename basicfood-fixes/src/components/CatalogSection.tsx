import { useState, useMemo, useEffect, forwardRef } from "react";
import { acos } from "@/lib/acos";
import ProductImpressionTracker from "@/components/ProductImpressionTracker";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useCart } from "@/contexts/CartContext";
import { useCartGuard } from "@/hooks/useCartGuard";
import { ShoppingCart, Check, Plus, Minus, Search, Grid3X3, List, Heart, Eye } from "lucide-react";
import { showAddedToCartToast } from "@/lib/cartToast";
import WishlistButton from "@/components/WishlistButton";
import CompareButton from "@/components/CompareButton";
import QuickViewModal from "@/components/QuickViewModal";
import OptimizedImage from "@/components/OptimizedImage";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import SectionHeading from "@/components/SectionHeading";
import { usePriceMode, parseWeightToGrams, WHOLESALE_MIN_GRAMS_PER_PRODUCT, wholesaleStartQty, wholesaleStepQty } from "@/contexts/PriceModeContext";
import { getLangFromPath, withLangPrefix } from "@/i18n";
import { useExperimentClickTracker } from "@/hooks/useExperimentClickTracker";
import { useWholesalePrices } from "@/hooks/useWholesalePrices";

import productLungs from "@/assets/product-lungs.jpg";
import productHeart from "@/assets/product-heart.jpg";
import productLiver from "@/assets/product-liver.jpg";
import productTripe from "@/assets/product-tripe.jpg";
import productAorta from "@/assets/product-aorta.jpg";
import productTrachea from "@/assets/product-trachea.jpg";
import productEsophagus from "@/assets/product-esophagus.jpg";
import productUdder from "@/assets/product-udder.jpg";
import productBully from "@/assets/product-bully.jpg";
import productMix from "@/assets/product-mix.jpg";

const fallbackImages: Record<string, string> = {
  "легені": productLungs,
  "серце": productHeart,
  "печінка": productLiver,
  "рубець": productTripe,
  "аорта": productAorta,
  "трахея": productTrachea,
  "стравохід": productEsophagus,
  "вим'я": productUdder,
  "пеніс": productBully,
  "набір": productMix,
};

export const getProductImage = (product: { name: string; image_url: string | null }) => {
  if (product.image_url) return product.image_url;
  const key = Object.keys(fallbackImages).find((k) => product.name.toLowerCase().includes(k));
  return key ? fallbackImages[key] : productMix;
};

const CatalogSection = () => {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const lang = getLangFromPath(pathname);
  const lp = (p: string) => withLangPrefix(p, lang);

  const tabs = [
    { id: "all", label: t("common.all") },
    { id: "dogs", label: t("catalog.tab_dogs") },
    { id: "cats", label: t("catalog.tab_cats") },
    { id: "training", label: t("catalog.tab_training") },
  ];

  const sortOptions = [
    { value: "sort_order", label: t("catalog.sort.default") },
    { value: "price_asc", label: t("catalog.sort.price_asc") },
    { value: "price_desc", label: t("catalog.sort.price_desc") },
    { value: "name_asc", label: t("catalog.sort.name_asc") },
    { value: "newest", label: t("catalog.sort.newest") },
  ];

  const [activeTab, setActiveTab] = useState("all");
  const [addedIds, setAddedIds] = useState<Set<string>>(new Set());
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  const [quickViewProduct, setQuickViewProduct] = useState<any | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [sortBy, setSortBy] = useState("sort_order");
  const [viewMode, setViewMode] = useState<"grid" | "list">("grid");
  const [priceRange, setPriceRange] = useState<[number, number]>([0, 1000]);
  const [showFilters, setShowFilters] = useState(false);
  // Butternut-style allergen exclusion. Asortyment domain: only one source
  // of chicken protein ("Шия куряча") — pet parents with poultry-allergic dogs
  // need a single switch, not a multi-select. Detection is text-based to avoid
  // a schema migration: matches name/description for курк/курин/chicken.
  const [excludeChicken, setExcludeChicken] = useState(false);
  const { addItem, items: cartItems, updateQuantity, removeItem } = useCart();
  const { addSafely } = useCartGuard();
  const { mode } = usePriceMode();
  const trackExperimentClick = useExperimentClickTracker();

  const [searchParams, setSearchParams] = useSearchParams();
  const needParam = searchParams.get("need");
  const segmentParam = searchParams.get("segment");
  const searchUrlParam = searchParams.get("search");

  // Підхопити пошук з URL (?search=печінка) — використовується з блоку
  // "Склад, який ви можете прочитати" на головній.
  useEffect(() => {
    if (searchUrlParam) {
      setSearchQuery(searchUrlParam);
      const el = document.getElementById("catalog");
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchUrlParam]);
  const needToTab: Record<string, string> = {
    training: "training",
    puppies: "dogs",
    sensitive: "dogs",
    dental: "dogs",
    dogs: "dogs",
    cats: "cats",
  };
  useEffect(() => {
    if (needParam && needToTab[needParam]) {
      setActiveTab(needToTab[needParam]);
      const el = document.getElementById("catalog");
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [needParam]);

  // Load product ids for the requested segment (e.g. ?segment=dental).
  // Filter applies on top of the active tab so users can combine "for dogs" + "dental".
  const { data: segmentProductIds } = useQuery({
    queryKey: ["product-segments", segmentParam],
    enabled: !!segmentParam,
    staleTime: 10 * 60_000,
    queryFn: async () => {
      const { data, error } = await supabase
        .from("product_segments")
        .select("product_id")
        .eq("segment", segmentParam as any);
      if (error) throw error;
      return new Set((data ?? []).map((r: any) => r.product_id as string));
    },
  });
  useEffect(() => {
    if (segmentParam) {
      const el = document.getElementById("catalog");
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [segmentParam]);

  const handleTabChange = (tabId: string) => {
    setActiveTab(tabId);
    if (needParam) {
      const next = new URLSearchParams(searchParams);
      next.delete("need");
      setSearchParams(next, { replace: true });
    }
  };

  /**
   * Quantity displayed on the catalog card.
   * - If the product is already in the cart, mirror its cart quantity so the
   *   `−` button actually reduces the line (previously the catalog only had a
   *   local "next add" counter, so users couldn't decrement from the catalog).
   * - Otherwise fall back to the locally pending quantity, defaulting to 1
   *   (or the wholesale start quantity in wholesale mode).
   */
  const getQty = (id: string, unitGrams: number) => {
    const inCart = cartItems.find((i) => i.id === id)?.quantity;
    if (inCart && inCart > 0) return inCart;
    const stored = quantities[id];
    if (stored && stored > 0) return stored;
    return mode === "wholesale" ? wholesaleStartQty(unitGrams) : 1;
  };
  /**
   * When the product is already in the cart, the stepper drives the cart line
   * directly (so the user can both add AND remove from the catalog). When it
   * isn't, we keep a local pending quantity that `handleAdd` will commit.
   */
  const setQty = (id: string, q: number) => {
    const inCart = cartItems.find((i) => i.id === id);
    if (inCart) {
      if (q <= 0) removeItem(id);
      else updateQuantity(id, q);
      return;
    }
    setQuantities((prev) => ({ ...prev, [id]: Math.max(1, q) }));
  };

  const { data: products = [], isLoading: productsLoading } = useQuery({
    queryKey: ["products"],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("products_public")
        .select("*")
        .eq("is_active", true)
        .order("sort_order");
      if (error) throw error;
      return data ?? [];
    },
  });

  const wholesalePrices = useWholesalePrices(products.map((p: any) => p.id));
  const hydratedProducts = useMemo(
    () => products.map((p: any) => ({ ...p, wholesale_price: wholesalePrices[p.id] ?? null })),
    [products, wholesalePrices],
  );

  const handleAdd = (product: any) => {
    const grams = parseWeightToGrams(product.weight);
    const alreadyInCart = cartItems.find((i) => i.id === product.id);
    // If the line exists, the stepper already drives cart quantity directly
    // (see setQty) — the "Add" button just bumps it by one extra unit.
    // Otherwise we commit the locally pending quantity.
    const qty = alreadyInCart ? 1 : getQty(product.id, grams);
    const ok = addSafely(
      {
        id: product.id,
        name: product.name,
        price: product.price,
        wholesale_price: product.wholesale_price ?? null,
        weight: product.weight,
        weight_grams: grams,
        image_url: getProductImage(product),
        categories: product.categories ?? null,
      },
      { qty },
    );
    if (!ok) return;
    acos({
      event_type: "add_to_cart",
      product_id: product.id,
      metadata: { name: product.name, price: product.price, qty, source: "catalog" },
    });
    trackExperimentClick("click");
    setAddedIds((prev) => new Set(prev).add(product.id));
    showAddedToCartToast(product.name, qty);
    setTimeout(() => setAddedIds((prev) => { const n = new Set(prev); n.delete(product.id); return n; }), 1500);
    setQuantities((prev) => {
      const next = { ...prev };
      delete next[product.id];
      return next;
    });
  };

  const maxPrice = useMemo(() => {
    if (hydratedProducts.length === 0) return 1000;
    return Math.max(...hydratedProducts.map((p) => p.price));
  }, [hydratedProducts]);

  // Sync the upper bound of priceRange with the actual max price once products load.
  // Without this, products priced above the initial 1000₴ cap would be hidden.
  useEffect(() => {
    if (maxPrice > 1000) {
      setPriceRange((prev) => prev[1] === 1000 ? [prev[0], maxPrice] : prev);
    }
  }, [maxPrice]);

  const filtered = useMemo(() => {
    let result = activeTab === "all"
      ? hydratedProducts
      : hydratedProducts.filter((p) => p.categories?.includes(activeTab));

    if (segmentParam && segmentProductIds) {
      result = result.filter((p) => segmentProductIds.has(p.id));
    }

    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      result = result.filter((p) =>
        p.name.toLowerCase().includes(q) ||
        p.description?.toLowerCase().includes(q)
      );
    }

    result = result.filter((p) => p.price >= priceRange[0] && p.price <= priceRange[1]);

    if (excludeChicken) {
      const re = /курк|курин|шия|chicken/i;
      result = result.filter((p) => !re.test(`${p.name} ${p.description ?? ""}`));
    }

    switch (sortBy) {
      case "price_asc":
        result = [...result].sort((a, b) => a.price - b.price);
        break;
      case "price_desc":
        result = [...result].sort((a, b) => b.price - a.price);
        break;
      case "name_asc":
        result = [...result].sort((a, b) => a.name.localeCompare(b.name, "uk"));
        break;
      case "newest":
        result = [...result].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
        break;
    }

    return result;
  }, [hydratedProducts, activeTab, searchQuery, sortBy, priceRange, segmentParam, segmentProductIds, excludeChicken]);

  useEffect(() => {
    const q = searchQuery.trim();
    if (q.length < 2) return;
    const timer = setTimeout(() => {
      acos({
        event_type: "search_performed",
        metadata: { query: q.slice(0, 100), results_count: filtered.length },
      });
    }, 800);
    return () => clearTimeout(timer);
  }, [searchQuery, filtered.length]);

  // Autocomplete suggestions
  const suggestions = useMemo(() => {
    if (searchQuery.length < 2) return [];
    const q = searchQuery.toLowerCase();
    return products
      .filter((p) => p.name.toLowerCase().includes(q))
      .slice(0, 5)
      .map((p) => p.name);
  }, [searchQuery, products]);

  // Product badges — пріоритет: ручні маркери з адмінки → авто (новинка за датою / sort_order).
  const getBadges = (product: any) => {
    const badges: { label: string; color: string }[] = [];
    if (product.is_bestseller) {
      badges.push({ label: t("catalog.badge_hit"), color: "bg-primary text-primary-foreground" });
    }
    if (product.is_new) {
      badges.push({ label: t("catalog.badge_new"), color: "bg-green-600 text-green-50" });
    } else {
      // Авто-маркер «Новинка» лише якщо адмін явно не позначав маркери — щоб не дублювати
      const createdAt = new Date(product.created_at);
      const weekAgo = new Date();
      weekAgo.setDate(weekAgo.getDate() - 14);
      if (createdAt > weekAgo) badges.push({ label: t("catalog.badge_new"), color: "bg-green-600 text-green-50" });
    }
    if (product.is_featured) {
      badges.push({ label: "💎", color: "bg-blue-600 text-blue-50" });
    }
    if (product.is_sale) {
      badges.push({ label: "🎯 -%", color: "bg-red-600 text-red-50" });
    }
    // Авто-«Хіт» за sort_order лише якщо немає жодного ручного бестселера в каталозі
    if (!product.is_bestseller && product.sort_order <= 2 && !badges.some(b => b.label === t("catalog.badge_hit"))) {
      badges.push({ label: t("catalog.badge_hit"), color: "bg-primary text-primary-foreground" });
    }
    // Вікова рекомендація — узгоджено з блогом «Коли можна давати ласощі цуценяті»:
    // допомагає власникам цуценят/кошенят одразу побачити, що підходить їх віку.
    if (typeof product.min_age_months === "number" && product.min_age_months > 0) {
      badges.push({
        label: t("catalog.badge_age_from", { months: product.min_age_months }),
        color: "bg-amber-600/90 text-amber-50",
      });
    }
    return badges;
  };

  return (
    <section id="catalog" className="py-12 sm:py-20 surface-glow">
      <div className="container mx-auto px-4">
        <SectionHeading
          eyebrow={t("catalog.eyebrow")}
          title={t("catalog.title")}
          subtitle={t("catalog.subtitle")}
        />

        {/* Wholesale info banner */}
        {mode === "wholesale" && (
          <div className="max-w-2xl mx-auto mb-6 px-4 py-3 rounded-lg border border-primary/30 bg-primary/10 text-sm text-foreground flex items-start gap-2">
            <span className="text-primary text-base leading-none">🏷️</span>
            <span>{t("catalog.wholesale_banner")}</span>
          </div>
        )}

        {/* Search */}
        <div className="max-w-md mx-auto mb-6 relative">
          <Search className="absolute left-3 top-2.5 w-4 h-4 text-muted-foreground" />
          <Input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={t("common.search_products")}
            className="pl-9"
          />
          {suggestions.length > 0 && searchQuery.length >= 2 && (
            <div className="absolute z-10 w-full mt-1 bg-card border border-border rounded-md shadow-lg">
              {suggestions.map((s, i) => (
                <button
                  key={i}
                  className="w-full text-left px-3 py-2 text-sm hover:bg-secondary transition-colors first:rounded-t-md last:rounded-b-md"
                  onClick={() => setSearchQuery(s)}
                >
                  {s}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Tabs */}
        <div className="flex flex-wrap justify-center gap-2 mb-6">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => handleTabChange(tab.id)}
              className={`px-4 py-2 rounded-full text-sm font-medium transition-colors ${
                activeTab === tab.id
                  ? "bg-primary text-primary-foreground"
                  : "bg-secondary text-secondary-foreground hover:bg-secondary/80"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* Sort & filters controls — compact on mobile.
            View-mode toggle (Grid/List) hidden on mobile (List view rarely used on small screens). */}
        <div className="flex items-center justify-between gap-2 sm:gap-3 mb-6">
          <div className="flex items-center gap-2 flex-1 min-w-0">
            <Select value={sortBy} onValueChange={setSortBy}>
              <SelectTrigger className="flex-1 sm:flex-none sm:w-48 h-9 text-sm min-w-0">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {sortOptions.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>{opt.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowFilters(!showFilters)}
              className="text-xs shrink-0 h-9"
            >
              {t("catalog.filters")} {showFilters ? "▲" : "▼"}
            </Button>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            <div className="hidden sm:flex items-center gap-1">
              <button
                onClick={() => setViewMode("grid")}
                className={`p-2 rounded ${viewMode === "grid" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"}`}
                aria-label={t("catalog.view_grid")}
              >
                <Grid3X3 className="w-4 h-4" />
              </button>
              <button
                onClick={() => setViewMode("list")}
                className={`p-2 rounded ${viewMode === "list" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"}`}
                aria-label={t("catalog.view_list")}
              >
                <List className="w-4 h-4" />
              </button>
            </div>
            <span className="text-xs text-muted-foreground ml-1 sm:ml-2 whitespace-nowrap">{filtered.length} {t("common.items_short")}</span>
          </div>
        </div>

        {/* Price filter */}
        {showFilters && (
          <div className="mb-8 p-4 bg-card rounded-lg border border-border space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">{t("catalog.price_label")}</span>
              <span className="text-xs text-muted-foreground">{priceRange[0]} — {priceRange[1]} ₴</span>
            </div>
            <Slider
              min={0}
              max={maxPrice}
              step={10}
              value={priceRange}
              onValueChange={(v) => setPriceRange(v as [number, number])}
              className="w-full"
            />
            {/* Allergen exclusion — Butternut "no chicken" filter */}
            <label className="flex items-center justify-between gap-3 pt-2 border-t border-border cursor-pointer">
              <span className="text-sm">
                {t("catalog_extra.no_chicken_label")}
                <span className="block text-[11px] text-muted-foreground">{t("catalog_extra.no_chicken_hint")}</span>
              </span>
              <input
                type="checkbox"
                checked={excludeChicken}
                onChange={(e) => setExcludeChicken(e.target.checked)}
                className="w-4 h-4 accent-primary cursor-pointer"
              />
            </label>
            {(priceRange[0] !== 0 || priceRange[1] !== maxPrice || activeTab !== "all" || searchQuery || sortBy !== "sort_order" || excludeChicken) && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setPriceRange([0, maxPrice]);
                  setActiveTab("all");
                  setSearchQuery("");
                  setSortBy("sort_order");
                  setExcludeChicken(false);
                }}
                className="w-full text-xs"
              >
                {t("common.reset_filters")}
              </Button>
            )}
          </div>
        )}

        {/* Skeleton placeholders during initial load — eliminates blank flash */}
        {productsLoading && products.length === 0 && (
          <div className="grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-3 gap-3 sm:gap-6">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="card-warm rounded-xl overflow-hidden">
                <div className="aspect-square bg-muted/40 animate-pulse" />
                <div className="p-3 sm:p-5 space-y-2">
                  <div className="h-4 w-3/4 bg-muted/40 rounded animate-pulse" />
                  <div className="h-3 w-1/2 bg-muted/30 rounded animate-pulse" />
                  <div className="flex justify-between items-center pt-1">
                    <div className="h-5 w-16 bg-muted/40 rounded animate-pulse" />
                    <div className="h-7 w-7 bg-primary/30 rounded animate-pulse" />
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Products grid/list */}
        <div className={viewMode === "grid"
          ? "grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-3 gap-3 sm:gap-6"
          : "space-y-4"
        }>
          {filtered.map((product, idx) => {
            const badges = getBadges(product);

            if (viewMode === "list") {
              return (
                <ProductImpressionTracker
                  key={product.id}
                  productId={product.id}
                  productName={product.name}
                  source="catalog_list"
                  position={idx + 1}
                  className="card-warm rounded-xl overflow-hidden flex"
                >
                  <Link
                    to={lp(`/product/${product.id}`)}
                    onClick={() => acos({ event_type: "product_clicked", product_id: product.id, metadata: { source: "catalog_list", name: product.name } })}
                    className="w-24 sm:w-32 h-24 sm:h-32 flex-shrink-0"
                  >
                    <img
                      src={getProductImage(product)}
                      alt={product.name}
                      loading="lazy"
                      decoding="async"
                      width="128"
                      height="128"
                      className="w-full h-full object-cover"
                    />
                  </Link>
                  <div className="p-3 sm:p-4 flex-1 flex flex-col justify-between min-w-0">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 mb-1 flex-wrap">
                        <Link
                          to={lp(`/product/${product.id}`)}
                          onClick={() => acos({ event_type: "product_clicked", product_id: product.id, metadata: { source: "catalog_list_title", name: product.name } })}
                          className="text-base sm:text-lg font-semibold hover:text-primary transition-colors line-clamp-1"
                        >
                          {product.name}
                        </Link>
                        {badges.map((b, i) => (
                          <span key={i} className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium ${b.color}`}>{b.label}</span>
                        ))}
                      </div>
                      <p className="text-xs sm:text-sm text-muted-foreground line-clamp-1">{product.description}</p>
                    </div>
                    <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 mt-2">
                      <PriceTag product={product} mode={mode} />

                      <div className="flex items-center gap-1.5 flex-wrap">
                        <QuantityControl
                          id={product.id}
                          qty={getQty(product.id, parseWeightToGrams(product.weight))}
                          setQty={setQty}
                          step={mode === "wholesale" ? wholesaleStepQty(parseWeightToGrams(product.weight)) : 1}
                          minQty={mode === "wholesale" ? wholesaleStartQty(parseWeightToGrams(product.weight)) : 1}
                          unitGrams={parseWeightToGrams(product.weight)}
                          displayInKg={mode === "wholesale"}
                        />
                        <Button size="sm" onClick={() => handleAdd(product)}>
                          {addedIds.has(product.id) ? <Check className="w-4 h-4" /> : <><ShoppingCart className="w-4 h-4 mr-1" /> <span className="hidden sm:inline">{t("product.in_cart")}</span></>}
                        </Button>
                      </div>
                    </div>
                  </div>
                </ProductImpressionTracker>
              );
            }

            return (
              <ProductImpressionTracker
                key={product.id}
                productId={product.id}
                productName={product.name}
                source="catalog_grid"
                position={idx + 1}
                className="card-warm rounded-xl overflow-hidden group"
              >
                <div className="aspect-square overflow-hidden relative bg-background/40">
                  <Link
                    to={lp(`/product/${product.id}`)}
                    onClick={() => acos({ event_type: "product_clicked", product_id: product.id, metadata: { source: "catalog_grid_image", name: product.name } })}
                    className="block w-full h-full"
                  >
                    {/* OptimizedImage emits Supabase WebP/AVIF srcset → mobile downloads
                        ~70% less than full-res JPEG. First 4 cards use priority=true to
                        skip lazy loading (above the fold on most viewports). */}
                    <OptimizedImage
                      src={getProductImage(product)}
                      alt={product.name}
                      width={640}
                      height={640}
                      priority={idx < 4}
                      sizes="(max-width: 640px) 50vw, (max-width: 1024px) 33vw, 25vw"
                      className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
                    />
                  </Link>
                  {/* Badges */}
                  {badges.length > 0 && (
                    <div className="absolute top-2 left-2 flex flex-col gap-1">
                      {badges.map((b, i) => (
                        <span key={i} className={`text-xs px-2 py-0.5 rounded-full font-medium ${b.color}`}>{b.label}</span>
                      ))}
                    </div>
                  )}
                  {/* Low-stock urgency badge — only shown when 1-5 left, drives FOMO */}
                  {product.stock_quantity > 0 && product.stock_quantity <= 5 && (
                    <div className="absolute bottom-2 left-2">
                      <span className="text-[10px] sm:text-xs px-2 py-0.5 rounded-full font-semibold bg-destructive/90 text-destructive-foreground backdrop-blur-sm animate-pulse">
                        {t("stock.low_stock", { count: product.stock_quantity })}
                      </span>
                    </div>
                  )}
                  {/* Wishlist heart + Compare — top-right floating */}
                  <div className="absolute top-2 right-2 flex flex-col gap-1.5">
                    <WishlistButton productId={product.id} variant="floating" size="sm" />
                    <CompareButton productId={product.id} variant="floating" size="sm" />
                  </div>
                  {/* Quick View — appears on hover (desktop) / always-visible (mobile, bottom-right corner)
                      Lets shoppers preview + add-to-cart without leaving the catalog grid. */}
                  <button
                    type="button"
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      setQuickViewProduct(product);
                      acos({ event_type: "product_clicked", product_id: product.id, metadata: { source: "quickview_open", name: product.name } });
                    }}
                    aria-label={t("catalog_extra.quick_view", { name: product.name }) as string}
                    className="absolute bottom-2 right-2 w-8 h-8 rounded-full bg-card/90 backdrop-blur-sm border border-border text-foreground hover:text-primary hover:border-primary transition-all flex items-center justify-center sm:opacity-0 sm:group-hover:opacity-100"
                  >
                    <Eye className="w-3.5 h-3.5" />
                  </button>
                </div>
                <div className="p-3 sm:p-5">
                  <Link
                    to={lp(`/product/${product.id}`)}
                    onClick={() => acos({ event_type: "product_clicked", product_id: product.id, metadata: { source: "catalog_grid_title", name: product.name } })}
                  >
                    <h3 className="text-sm sm:text-lg font-semibold mb-1 hover:text-primary transition-colors line-clamp-2">{product.name}</h3>
                  </Link>
                  <p className="text-xs sm:text-sm text-muted-foreground mb-2 sm:mb-3 line-clamp-2 hidden sm:block">{product.description}</p>
                  {/* Mobile: stacked layout with full-width add-to-cart button in the
                      thumb-zone. Desktop keeps the inline price ↔ button arrangement. */}
                  <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                    <PriceTag product={product} mode={mode} />

                    <div className="flex items-center gap-1.5 justify-between sm:justify-end w-full sm:w-auto">
                      <QuantityControl
                        id={product.id}
                        qty={getQty(product.id, parseWeightToGrams(product.weight))}
                        setQty={setQty}
                        step={mode === "wholesale" ? wholesaleStepQty(parseWeightToGrams(product.weight)) : 1}
                        minQty={mode === "wholesale" ? wholesaleStartQty(parseWeightToGrams(product.weight)) : 1}
                        unitGrams={parseWeightToGrams(product.weight)}
                        displayInKg={mode === "wholesale"}
                      />
                      <Button
                        size="sm"
                        onClick={() => handleAdd(product)}
                        className="h-9 sm:h-8 px-2.5 sm:px-3 shrink-0 text-xs sm:text-sm"
                        aria-label={t("product.in_cart") as string}
                      >
                        {addedIds.has(product.id) ? (
                          <Check className="w-4 h-4" />
                        ) : (
                          <>
                            <ShoppingCart className="w-3.5 h-3.5 sm:w-4 sm:h-4 sm:mr-1" />
                            <span className="hidden sm:inline">{t("product.in_cart")}</span>
                          </>
                        )}
                      </Button>
                    </div>
                  </div>
                </div>
              </ProductImpressionTracker>
            );
          })}
        </div>

        {filtered.length === 0 && !productsLoading && (
          <div className="text-center py-12 max-w-md mx-auto">
            <Search className="w-10 h-10 mx-auto mb-3 text-muted-foreground/40" />
            <p className="text-muted-foreground mb-4">{t("common.nothing_found")}</p>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setSearchQuery("");
                setActiveTab("all");
                setPriceRange([0, maxPrice]);
              }}
            >
              {t("common.reset_filters", "Скинути фільтри")}
            </Button>
          </div>
        )}
      </div>

      {/* Quick View modal — controlled at section level so it overlays the entire catalog */}
      <QuickViewModal
        product={quickViewProduct}
        open={!!quickViewProduct}
        onOpenChange={(o) => !o && setQuickViewProduct(null)}
      />
    </section>
  );
};

// forwardRef-обгортка щоб уникнути React warning, коли ці компоненти потрапляють
// під `asChild` у Radix-обгортках (Sheet/Tooltip/Dialog) у дереві CatalogSection.
const QuantityControl = forwardRef<HTMLDivElement, {
  id: string;
  qty: number;
  setQty: (id: string, q: number) => void;
  step?: number;
  minQty?: number;
  /** Unit weight in grams — when provided together with `displayInKg`, the
   *  control renders the cumulative weight in kg instead of unit count. */
  unitGrams?: number;
  displayInKg?: boolean;
}>(({ id, qty, setQty, step = 1, minQty = 1, unitGrams = 0, displayInKg = false }, ref) => {
  const { t } = useTranslation();
  const showKg = displayInKg && unitGrams > 0;
  const label = showKg
    ? `${((qty * unitGrams) / 1000).toLocaleString("uk-UA", { maximumFractionDigits: 1 })} кг`
    : qty;
  const handleDecrease = () => {
    const next = qty - step;
    // Going below the minimum signals "remove from cart" — the parent's
    // setQty handler decides whether to actually remove the line (when the
    // product is already in the cart) or clamp back up.
    setQty(id, next < minQty ? 0 : next);
  };
  return (
    <div ref={ref} className="flex items-center border border-border rounded-md">
      <button
        onClick={handleDecrease}
        className="px-2 py-1 text-muted-foreground hover:text-foreground transition-colors"
        aria-label={t("product.qty_decrease")}
      >
        <Minus className="w-3.5 h-3.5" />
      </button>
      <span className={`px-2 text-sm font-medium text-center ${showKg ? "min-w-[44px]" : "min-w-[24px]"}`}>{label}</span>
      <button
        onClick={() => setQty(id, qty + step)}
        className="px-2 py-1 text-muted-foreground hover:text-foreground transition-colors"
        aria-label={t("product.qty_increase")}
      >
        <Plus className="w-3.5 h-3.5" />
      </button>
    </div>
  );
});
QuantityControl.displayName = "QuantityControl";

const PriceTag = forwardRef<HTMLDivElement, {
  product: { price: number; wholesale_price?: number | null; weight: string };
  mode: "retail" | "wholesale";
}>(({ product, mode }, ref) => {
  const { t } = useTranslation();
  const hasWholesale = !!product.wholesale_price && product.wholesale_price > 0;
  const showWholesale = mode === "wholesale" && hasWholesale;
  const grams = parseWeightToGrams(product.weight);
  // In wholesale mode, prices are quoted per 1 kg (the unit customers buy in).
  const displayed = showWholesale && grams > 0
    ? Math.round((product.wholesale_price as number) * 1000 / grams)
    : product.price;
  const unit = showWholesale ? "1 кг" : product.weight;
  const retailPerKg = grams > 0 ? Math.round((product.price * 1000) / grams) : product.price;

  return (
    <div ref={ref} className="flex flex-col">
      <div className="flex items-baseline gap-2">
        <span className="text-xl font-bold text-primary">{displayed} ₴</span>
        <span className="text-sm text-muted-foreground">/ {unit}</span>
      </div>
      {showWholesale && (
        <div className="flex items-center gap-1.5 mt-0.5">
          <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-primary/15 text-primary border border-primary/30 font-semibold">
            {t("catalog.wholesale_short")}
          </span>
          <span className="text-[10px] text-muted-foreground line-through">{retailPerKg} ₴ / 1 кг</span>
        </div>
      )}
      {!showWholesale && hasWholesale && mode === "retail" && grams > 0 && (
        <span className="text-[10px] text-muted-foreground mt-0.5">
          {t("product.wholesale_from_kg_short", { price: Math.round((product.wholesale_price as number) * 1000 / grams) })}
        </span>
      )}
    </div>
  );
});
PriceTag.displayName = "PriceTag";

export default CatalogSection;
