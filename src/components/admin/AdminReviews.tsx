import { useEffect, useState } from "react";
import { supabase } from "@/lib/supabase";
import type { Review } from "@/types";

export default function AdminReviews() {
  const [reviews, setReviews] = useState<Review[]>([]);
  const [filter, setFilter] = useState<"pending" | "approved" | "all">("pending");
  const [reply, setReply] = useState<{ id: number; text: string } | null>(null);

  useEffect(() => { load(); }, [filter]);

  async function load() {
    let q = supabase.from("reviews").select("*, models(name)").order("created_at", { ascending: false });
    if (filter === "pending") q = q.eq("approved", false);
    if (filter === "approved") q = q.eq("approved", true);
    const { data } = await q;
    setReviews((data as Review[]) ?? []);
  }

  async function approve(id: number) {
    await supabase.from("reviews").update({ approved: true }).eq("id", id);
    load();
  }

  async function reject(id: number) {
    await supabase.from("reviews").delete().eq("id", id);
    load();
  }

  async function saveReply() {
    if (!reply) return;
    await supabase.from("reviews").update({ admin_reply: reply.text }).eq("id", reply.id);
    setReply(null);
    load();
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="font-playfair text-3xl text-white">Отзывы</h2>
        <div className="flex gap-2">
          {(["pending", "approved", "all"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-4 py-1.5 rounded-lg text-sm ${filter === f ? "bg-[#c9a96e] text-black font-semibold" : "bg-[#141414] text-white/50 hover:text-white"}`}
            >
              {{ pending: "Ожидают", approved: "Одобрены", all: "Все" }[f]}
            </button>
          ))}
        </div>
      </div>

      <div className="space-y-3">
        {reviews.map((r) => (
          <div key={r.id} className="bg-[#0e0e0e] border border-white/6 rounded-xl p-5">
            <div className="flex items-start justify-between gap-4">
              <div className="flex-1">
                <div className="flex items-center gap-3 mb-1">
                  <span className="text-white font-medium">{r.client_name}</span>
                  <span className="text-[#c9a96e]">{"★".repeat(r.rating)}</span>
                  <span className={`px-2 py-0.5 rounded text-xs ${r.approved ? "bg-emerald-500/20 text-emerald-400" : "bg-yellow-500/20 text-yellow-400"}`}>
                    {r.approved ? "Одобрен" : "На модерации"}
                  </span>
                </div>
                <p className="text-white/60 text-sm">{r.text}</p>
                {r.admin_reply && (
                  <div className="mt-2 pl-3 border-l border-[#c9a96e]/30 text-white/40 text-sm italic">
                    Ответ: {r.admin_reply}
                  </div>
                )}
              </div>
              <div className="flex gap-2 flex-shrink-0">
                {!r.approved && (
                  <button onClick={() => approve(r.id)} className="px-3 py-1 bg-emerald-500/20 text-emerald-400 rounded-lg text-xs hover:bg-emerald-500/30">
                    Одобрить
                  </button>
                )}
                <button onClick={() => setReply({ id: r.id, text: r.admin_reply ?? "" })} className="px-3 py-1 bg-[#c9a96e]/20 text-[#c9a96e] rounded-lg text-xs">
                  Ответить
                </button>
                <button onClick={() => reject(r.id)} className="px-3 py-1 bg-red-500/20 text-red-400 rounded-lg text-xs">
                  Удалить
                </button>
              </div>
            </div>
          </div>
        ))}
        {reviews.length === 0 && (
          <div className="text-center py-12 text-white/30">Отзывов нет</div>
        )}
      </div>

      {reply && (
        <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" onClick={() => setReply(null)}>
          <div className="bg-[#0e0e0e] border border-white/10 rounded-2xl p-6 w-full max-w-md" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-white text-lg font-semibold mb-4">Ответ на отзыв</h3>
            <textarea
              value={reply.text}
              onChange={(e) => setReply((r) => r && { ...r, text: e.target.value })}
              rows={4}
              placeholder="Напишите ответ..."
              className="w-full bg-[#141414] border border-white/10 rounded-xl px-4 py-3 text-white text-sm focus:outline-none focus:border-[#c9a96e] resize-none mb-4"
            />
            <div className="flex gap-3">
              <button onClick={saveReply} className="flex-1 bg-[#c9a96e] hover:bg-[#b8943c] text-black font-semibold py-3 rounded-xl">Сохранить</button>
              <button onClick={() => setReply(null)} className="px-6 border border-white/10 text-white/50 rounded-xl">Отмена</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
