import { useEffect, useState } from "react";
import { supabase } from "@/integrations/supabase/client";
import type { Booking } from "@/types";

const STATUS_LABELS: Record<string, { label: string; color: string }> = {
  new: { label: "Новая", color: "bg-blue-500/20 text-blue-400" },
  confirmed: { label: "Подтверждена", color: "bg-emerald-500/20 text-emerald-400" },
  completed: { label: "Завершена", color: "bg-gray-500/20 text-gray-400" },
  cancelled: { label: "Отменена", color: "bg-red-500/20 text-red-400" },
};

const STATUSES = ["", "new", "confirmed", "completed", "cancelled"];

export default function AdminOrders() {
  const [orders, setOrders] = useState<Booking[]>([]);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<Booking | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => { load(); }, [filter]);

  async function load() {
    setLoading(true);
    let q = supabase
      .from("bookings")
      .select("*, models(name)")
      .order("created_at", { ascending: false })
      .limit(50);
    if (filter) q = q.eq("status", filter);
    const { data } = await q;
    setOrders((data as Booking[]) ?? []);
    setLoading(false);
  }

  async function updateStatus(id: number, status: string) {
    await supabase.from("bookings").update({ status }).eq("id", id);
    load();
    if (selected?.id === id) setSelected((s) => s && { ...s, status });
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="font-playfair text-3xl text-white">Заявки</h2>
        <div className="flex gap-2">
          {STATUSES.map((s) => (
            <button
              key={s}
              onClick={() => setFilter(s)}
              className={`px-4 py-1.5 rounded-lg text-sm transition-colors ${
                filter === s ? "bg-[#c9a96e] text-black font-semibold" : "bg-[#141414] text-white/50 hover:text-white"
              }`}
            >
              {s ? STATUS_LABELS[s]?.label : "Все"}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-16 bg-[#141414] rounded-xl animate-pulse" />
          ))}
        </div>
      ) : (
        <div className="space-y-2">
          {orders.map((order) => (
            <div
              key={order.id}
              onClick={() => setSelected(order)}
              className="bg-[#0e0e0e] border border-white/6 rounded-xl px-5 py-4 flex items-center justify-between cursor-pointer hover:border-[#c9a96e]/30 transition-colors"
            >
              <div>
                <div className="flex items-center gap-3">
                  <span className="text-white font-medium">#{order.id} — {order.client_name}</span>
                  <span className={`px-2 py-0.5 rounded text-xs ${STATUS_LABELS[order.status]?.color}`}>
                    {STATUS_LABELS[order.status]?.label}
                  </span>
                </div>
                <div className="text-white/40 text-sm mt-0.5">
                  {order.client_phone} · {order.event_type} · {order.event_date || "дата не указана"}
                </div>
              </div>
              <div className="text-white/30 text-sm">
                {new Date(order.created_at).toLocaleDateString("ru")}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Detail modal */}
      {selected && (
        <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" onClick={() => setSelected(null)}>
          <div className="bg-[#0e0e0e] border border-white/10 rounded-2xl p-6 w-full max-w-md" onClick={(e) => e.stopPropagation()}>
            <div className="flex justify-between items-start mb-4">
              <h3 className="text-white text-xl font-semibold">Заявка #{selected.id}</h3>
              <button onClick={() => setSelected(null)} className="text-white/30 hover:text-white">✕</button>
            </div>
            <div className="space-y-3 mb-6">
              {[
                ["Клиент", selected.client_name],
                ["Телефон", selected.client_phone],
                ["Email", selected.client_email],
                ["Тип события", selected.event_type],
                ["Дата", selected.event_date],
                ["Бюджет", selected.budget ? `${selected.budget} ₽` : null],
                ["Комментарий", selected.comment],
              ].filter(([, v]) => v).map(([k, v]) => (
                <div key={String(k)}>
                  <div className="text-white/40 text-xs">{k}</div>
                  <div className="text-white text-sm">{v}</div>
                </div>
              ))}
            </div>
            <div>
              <div className="text-white/40 text-xs mb-2">Изменить статус</div>
              <div className="flex flex-wrap gap-2">
                {Object.entries(STATUS_LABELS).map(([s, { label, color }]) => (
                  <button
                    key={s}
                    onClick={() => updateStatus(selected.id, s)}
                    className={`px-3 py-1.5 rounded-lg text-xs font-medium ${selected.status === s ? color + " ring-1 ring-current" : "bg-[#141414] text-white/50 hover:text-white"}`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
