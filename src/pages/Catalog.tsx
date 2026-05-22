import { useState, useEffect } from "react";
import { supabase } from "@/integrations/supabase/client";
import { ModelCard } from "@/components/catalog/ModelCard";
import type { Model } from "@/types";

const CATEGORIES = [
  { value: "", label: "Все" },
  { value: "fashion", label: "Fashion" },
  { value: "commercial", label: "Коммерческая" },
  { value: "events", label: "Мероприятия" },
];

const CITIES = ["", "Москва", "Санкт-Петербург", "Краснодар", "Екатеринбург"];

const PER_PAGE = 12;

export default function Catalog() {
  const [models, setModels] = useState<Model[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [category, setCategory] = useState("");
  const [city, setCity] = useState("");
  const [onlyAvailable, setOnlyAvailable] = useState(false);
  const [onlyFeatured, setOnlyFeatured] = useState(false);
  const [search, setSearch] = useState("");

  useEffect(() => {
    setPage(0);
  }, [category, city, onlyAvailable, onlyFeatured, search]);

  useEffect(() => {
    // Sync filters to URL
    const params = new URLSearchParams();
    if (category) params.set("category", category);
    if (city) params.set("city", city);
    if (onlyAvailable) params.set("available", "1");
    if (onlyFeatured) params.set("featured", "1");
    if (search) params.set("q", search);
    if (page) params.set("page", String(page));
    window.history.replaceState(null, "", "?" + params.toString());
  }, [category, city, onlyAvailable, onlyFeatured, search, page]);

  useEffect(() => {
    load();
  }, [category, city, onlyAvailable, onlyFeatured, search, page]);

  async function load() {
    setLoading(true);
    let q = supabase
      .from("models")
      .select("*", { count: "exact" })
      .eq("is_active", true)
      .order("featured", { ascending: false })
      .order("name")
      .range(page * PER_PAGE, page * PER_PAGE + PER_PAGE - 1);

    if (category) q = q.eq("category", category);
    if (city) q = q.eq("city", city);
    if (onlyAvailable) q = q.eq("is_available", true);
    if (onlyFeatured) q = q.eq("featured", true);
    if (search) q = q.ilike("name", `%${search}%`);

    const { data, count } = await q;
    setModels((data as Model[]) ?? []);
    setTotal(count ?? 0);
    setLoading(false);
  }

  const totalPages = Math.ceil(total / PER_PAGE);

  return (
    <div className="min-h-screen bg-[#080808] text-white pt-24 pb-16">
      <div className="max-w-7xl mx-auto px-4">
        {/* Header */}
        <div className="mb-10">
          <p className="text-[#c9a96e] text-xs tracking-[0.3em] uppercase mb-2">Агентство</p>
          <h1 className="font-playfair text-5xl mb-2">Каталог моделей</h1>
          <p className="text-white/40">{total} моделей</p>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap gap-3 mb-8">
          <input
            type="text"
            placeholder="Поиск по имени..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="bg-[#141414] border border-white/10 rounded-xl px-4 py-2 text-sm text-white placeholder-white/30 focus:outline-none focus:border-[#c9a96e] w-48"
          />
          <div className="flex gap-1">
            {CATEGORIES.map((c) => (
              <button
                key={c.value}
                onClick={() => setCategory(c.value)}
                className={`px-4 py-2 rounded-xl text-sm transition-colors ${
                  category === c.value
                    ? "bg-[#c9a96e] text-black font-semibold"
                    : "bg-[#141414] border border-white/10 text-white/60 hover:border-[#c9a96e]/40"
                }`}
              >
                {c.label}
              </button>
            ))}
          </div>
          <select
            value={city}
            onChange={(e) => setCity(e.target.value)}
            className="bg-[#141414] border border-white/10 rounded-xl px-4 py-2 text-sm text-white/70 focus:outline-none focus:border-[#c9a96e]"
          >
            {CITIES.map((c) => (
              <option key={c} value={c}>{c || "Все города"}</option>
            ))}
          </select>
          <button
            onClick={() => setOnlyAvailable((v) => !v)}
            className={`px-4 py-2 rounded-xl text-sm border transition-colors ${
              onlyAvailable ? "bg-emerald-600 border-emerald-600 text-white" : "border-white/10 text-white/60 hover:border-[#c9a96e]/40"
            }`}
          >
            Свободные
          </button>
          <button
            onClick={() => setOnlyFeatured((v) => !v)}
            className={`px-4 py-2 rounded-xl text-sm border transition-colors ${
              onlyFeatured ? "bg-[#c9a96e] border-[#c9a96e] text-black font-semibold" : "border-white/10 text-white/60 hover:border-[#c9a96e]/40"
            }`}
          >
            ⭐ Топ
          </button>
        </div>

        {/* Grid */}
        {loading ? (
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="aspect-[3/4] bg-[#141414] rounded-2xl animate-pulse" />
            ))}
          </div>
        ) : models.length === 0 ? (
          <div className="text-center py-24 text-white/30">
            <div className="text-5xl mb-4">🔍</div>
            <p>Ничего не найдено. Попробуйте изменить фильтры.</p>
          </div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
            {models.map((model) => (
              <ModelCard key={model.id} model={model} />
            ))}
          </div>
        )}

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex justify-center gap-2 mt-12">
            <button
              disabled={page === 0}
              onClick={() => setPage((p) => p - 1)}
              className="px-4 py-2 border border-white/10 rounded-xl text-white/60 disabled:opacity-30 hover:border-[#c9a96e]"
            >
              ←
            </button>
            {Array.from({ length: totalPages }).map((_, i) => (
              <button
                key={i}
                onClick={() => setPage(i)}
                className={`w-10 h-10 rounded-xl text-sm ${
                  page === i ? "bg-[#c9a96e] text-black font-semibold" : "border border-white/10 text-white/60 hover:border-[#c9a96e]"
                }`}
              >
                {i + 1}
              </button>
            ))}
            <button
              disabled={page === totalPages - 1}
              onClick={() => setPage((p) => p + 1)}
              className="px-4 py-2 border border-white/10 rounded-xl text-white/60 disabled:opacity-30 hover:border-[#c9a96e]"
            >
              →
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
