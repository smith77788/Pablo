import { useEffect, useState } from "react";
import { supabase } from "@/integrations/supabase/client";
import type { Model } from "@/types";

const EMPTY: Partial<Model> = { name: "", city: "", category: "fashion", is_active: true, featured: false };

export default function AdminModels() {
  const [models, setModels] = useState<Model[]>([]);
  const [editing, setEditing] = useState<Partial<Model> | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => { load(); }, []);

  async function load() {
    setLoading(true);
    const { data } = await supabase.from("models").select("*").order("name");
    setModels((data as Model[]) ?? []);
    setLoading(false);
  }

  async function save() {
    if (!editing) return;
    setSaving(true);
    if (editing.id) {
      await supabase.from("models").update(editing).eq("id", editing.id);
    } else {
      await supabase.from("models").insert(editing);
    }
    setSaving(false);
    setEditing(null);
    load();
  }

  async function toggleActive(id: number, val: boolean) {
    await supabase.from("models").update({ is_active: val }).eq("id", id);
    load();
  }

  async function toggleFeatured(id: number, val: boolean) {
    await supabase.from("models").update({ featured: val }).eq("id", id);
    load();
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="font-playfair text-3xl text-white">Модели</h2>
        <button
          onClick={() => setEditing({ ...EMPTY })}
          className="bg-[#c9a96e] hover:bg-[#b8943c] text-black font-semibold px-5 py-2 rounded-xl text-sm transition-colors"
        >
          + Добавить модель
        </button>
      </div>

      {loading ? (
        <div className="space-y-3">{Array.from({ length: 4 }).map((_, i) => <div key={i} className="h-16 bg-[#141414] rounded-xl animate-pulse" />)}</div>
      ) : (
        <div className="space-y-2">
          {models.map((m) => (
            <div key={m.id} className="bg-[#0e0e0e] border border-white/6 rounded-xl px-5 py-4 flex items-center gap-4">
              <div className="w-10 h-12 rounded-lg overflow-hidden bg-[#141414] flex-shrink-0">
                {m.photo_url ? (
                  <img src={m.photo_url} alt={m.name} className="w-full h-full object-cover" />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-white/20">👤</div>
                )}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-white font-medium">{m.name}</span>
                  {m.featured && <span className="text-[#c9a96e] text-xs">⭐ Топ</span>}
                  {!m.is_active && <span className="text-white/30 text-xs">скрыта</span>}
                </div>
                <div className="text-white/40 text-sm">{m.city} · {m.category} {m.age ? `· ${m.age} лет` : ""} {m.height ? `· ${m.height} см` : ""}</div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => toggleFeatured(m.id, !m.featured)}
                  className={`px-3 py-1 rounded-lg text-xs ${m.featured ? "bg-[#c9a96e]/20 text-[#c9a96e]" : "bg-[#141414] text-white/40"}`}
                >
                  ⭐ Топ
                </button>
                <button
                  onClick={() => toggleActive(m.id, !m.is_active)}
                  className={`px-3 py-1 rounded-lg text-xs ${m.is_active ? "bg-emerald-500/20 text-emerald-400" : "bg-[#141414] text-white/40"}`}
                >
                  {m.is_active ? "Активна" : "Скрыта"}
                </button>
                <button
                  onClick={() => setEditing({ ...m })}
                  className="px-3 py-1 rounded-lg text-xs bg-[#141414] text-white/50 hover:text-white"
                >
                  Изменить
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Edit modal */}
      {editing && (
        <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" onClick={() => setEditing(null)}>
          <div className="bg-[#0e0e0e] border border-white/10 rounded-2xl p-6 w-full max-w-lg max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            <div className="flex justify-between items-center mb-5">
              <h3 className="text-white text-xl font-semibold">{editing.id ? "Редактировать" : "Новая модель"}</h3>
              <button onClick={() => setEditing(null)} className="text-white/30 hover:text-white">✕</button>
            </div>
            <div className="grid grid-cols-2 gap-3">
              {[
                ["name", "Имя *", "text"],
                ["city", "Город", "text"],
                ["age", "Возраст", "number"],
                ["height", "Рост (см)", "number"],
                ["weight", "Вес (кг)", "number"],
                ["clothing_size", "Размер одежды", "text"],
                ["shoe_size", "Размер обуви", "number"],
                ["hair_color", "Цвет волос", "text"],
                ["photo_url", "Фото URL", "text"],
              ].map(([key, label, type]) => (
                <div key={String(key)} className={key === "photo_url" || key === "name" ? "col-span-2" : ""}>
                  <label className="text-white/40 text-xs block mb-1">{label}</label>
                  <input
                    type={String(type)}
                    value={(editing as Record<string, unknown>)[String(key)] as string ?? ""}
                    onChange={(e) => setEditing((ed) => ({ ...ed, [String(key)]: e.target.value }))}
                    className="w-full bg-[#141414] border border-white/10 rounded-xl px-3 py-2 text-white text-sm focus:outline-none focus:border-[#c9a96e]"
                  />
                </div>
              ))}
              <div className="col-span-2">
                <label className="text-white/40 text-xs block mb-1">Категория</label>
                <select
                  value={editing.category ?? "fashion"}
                  onChange={(e) => setEditing((ed) => ({ ...ed, category: e.target.value as Model["category"] }))}
                  className="w-full bg-[#141414] border border-white/10 rounded-xl px-3 py-2 text-white text-sm focus:outline-none focus:border-[#c9a96e]"
                >
                  <option value="fashion">Fashion</option>
                  <option value="commercial">Коммерческая</option>
                  <option value="events">Мероприятия</option>
                </select>
              </div>
              <div className="col-span-2">
                <label className="text-white/40 text-xs block mb-1">О модели</label>
                <textarea
                  value={editing.bio ?? ""}
                  onChange={(e) => setEditing((ed) => ({ ...ed, bio: e.target.value }))}
                  rows={3}
                  className="w-full bg-[#141414] border border-white/10 rounded-xl px-3 py-2 text-white text-sm focus:outline-none focus:border-[#c9a96e] resize-none"
                />
              </div>
              <div className="flex items-center gap-4">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={editing.is_active ?? true}
                    onChange={(e) => setEditing((ed) => ({ ...ed, is_active: e.target.checked }))}
                    className="accent-[#c9a96e]"
                  />
                  <span className="text-white/60 text-sm">Активна</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={editing.featured ?? false}
                    onChange={(e) => setEditing((ed) => ({ ...ed, featured: e.target.checked }))}
                    className="accent-[#c9a96e]"
                  />
                  <span className="text-white/60 text-sm">⭐ Топ</span>
                </label>
              </div>
            </div>
            <div className="flex gap-3 mt-6">
              <button
                onClick={save}
                disabled={saving}
                className="flex-1 bg-[#c9a96e] hover:bg-[#b8943c] disabled:opacity-50 text-black font-semibold py-3 rounded-xl transition-colors"
              >
                {saving ? "Сохраняю..." : "Сохранить"}
              </button>
              <button onClick={() => setEditing(null)} className="px-6 border border-white/10 text-white/50 hover:text-white rounded-xl transition-colors">
                Отмена
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
