import { useState } from "react";
import { supabase } from "@/lib/supabase";

const EVENT_TYPES = [
  { value: "photo", label: "📷 Фотосессия" },
  { value: "video", label: "🎬 Видеосъёмка" },
  { value: "event", label: "🎉 Мероприятие" },
  { value: "promo", label: "📢 Промоакция" },
  { value: "fashion", label: "👗 Показ мод" },
  { value: "commercial", label: "📺 Реклама" },
  { value: "other", label: "📋 Другое" },
];

const STEPS = ["Тип события", "Дата и детали", "Контакты", "Подтверждение"];

interface Props {
  modelId?: number;
  modelName?: string;
  onSuccess?: () => void;
}

export function BookingForm({ modelId, modelName, onSuccess }: Props) {
  const [step, setStep] = useState(0);
  const [done, setDone] = useState(false);
  const [loading, setLoading] = useState(false);
  const [form, setForm] = useState({
    event_type: "",
    event_date: "",
    event_location: "",
    budget: "",
    client_name: "",
    client_phone: "",
    client_email: "",
    comment: "",
    promo_code: "",
  });

  function set(k: string, v: string) {
    setForm((f) => ({ ...f, [k]: v }));
  }

  async function submit() {
    setLoading(true);
    await supabase.from("bookings").insert({
      model_id: modelId ?? null,
      event_type: form.event_type || "other",
      event_date: form.event_date || null,
      event_location: form.event_location || null,
      budget: form.budget ? Number(form.budget) : null,
      client_name: form.client_name,
      client_phone: form.client_phone,
      client_email: form.client_email || null,
      comment: form.comment || null,
      status: "new",
    });
    setLoading(false);
    setDone(true);
    onSuccess?.();
  }

  if (done) {
    return (
      <div className="p-8 text-center">
        <div className="text-5xl mb-4">✅</div>
        <h3 className="font-playfair text-2xl text-white mb-2">Заявка отправлена!</h3>
        <p className="text-white/50">Мы свяжемся с вами в ближайшее время.</p>
      </div>
    );
  }

  return (
    <div className="p-6">
      {/* Progress */}
      <div className="flex items-center gap-2 mb-8">
        {STEPS.map((s, i) => (
          <div key={s} className="flex items-center gap-2 flex-1">
            <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-semibold flex-shrink-0 ${i < step ? "bg-[#c9a96e] text-black" : i === step ? "bg-[#c9a96e] text-black" : "bg-white/10 text-white/30"}`}>
              {i < step ? "✓" : i + 1}
            </div>
            <span className={`text-xs hidden sm:block ${i === step ? "text-[#c9a96e]" : "text-white/30"}`}>{s}</span>
            {i < STEPS.length - 1 && <div className={`flex-1 h-px ${i < step ? "bg-[#c9a96e]" : "bg-white/10"}`} />}
          </div>
        ))}
      </div>

      {modelName && (
        <div className="bg-[#c9a96e]/10 border border-[#c9a96e]/20 rounded-xl px-4 py-2 text-[#c9a96e] text-sm mb-6">
          Модель: <strong>{modelName}</strong>
        </div>
      )}

      {/* Step 0 — Event type */}
      {step === 0 && (
        <div>
          <h3 className="text-white text-lg font-semibold mb-4">Тип события</h3>
          <div className="grid grid-cols-2 gap-3">
            {EVENT_TYPES.map((t) => (
              <button
                key={t.value}
                onClick={() => { set("event_type", t.value); setStep(1); }}
                className={`py-3 px-4 rounded-xl border text-sm text-left transition-colors ${form.event_type === t.value ? "border-[#c9a96e] bg-[#c9a96e]/10 text-[#c9a96e]" : "border-white/10 text-white/60 hover:border-[#c9a96e]/40"}`}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Step 1 — Date & details */}
      {step === 1 && (
        <div className="space-y-4">
          <h3 className="text-white text-lg font-semibold mb-4">Дата и детали</h3>
          <div>
            <label className="text-white/40 text-xs mb-1 block">Дата события</label>
            <input
              type="date"
              value={form.event_date}
              onChange={(e) => set("event_date", e.target.value)}
              className="w-full bg-[#141414] border border-white/10 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-[#c9a96e]"
            />
          </div>
          <div>
            <label className="text-white/40 text-xs mb-1 block">Место проведения</label>
            <input
              type="text"
              placeholder="Город, адрес или онлайн"
              value={form.event_location}
              onChange={(e) => set("event_location", e.target.value)}
              className="w-full bg-[#141414] border border-white/10 rounded-xl px-4 py-3 text-white placeholder-white/30 focus:outline-none focus:border-[#c9a96e]"
            />
          </div>
          <div>
            <label className="text-white/40 text-xs mb-1 block">Бюджет (₽)</label>
            <input
              type="number"
              placeholder="Укажите ваш бюджет"
              value={form.budget}
              onChange={(e) => set("budget", e.target.value)}
              className="w-full bg-[#141414] border border-white/10 rounded-xl px-4 py-3 text-white placeholder-white/30 focus:outline-none focus:border-[#c9a96e]"
            />
          </div>
          <div>
            <label className="text-white/40 text-xs mb-1 block">Комментарий</label>
            <textarea
              placeholder="Детали, пожелания, ТЗ..."
              value={form.comment}
              onChange={(e) => set("comment", e.target.value)}
              rows={3}
              className="w-full bg-[#141414] border border-white/10 rounded-xl px-4 py-3 text-white placeholder-white/30 focus:outline-none focus:border-[#c9a96e] resize-none"
            />
          </div>
          <div className="flex gap-3">
            <button onClick={() => setStep(0)} className="border border-white/10 text-white/50 px-6 py-3 rounded-xl hover:text-white transition-colors">← Назад</button>
            <button onClick={() => setStep(2)} className="flex-1 bg-[#c9a96e] hover:bg-[#b8943c] text-black font-semibold py-3 rounded-xl transition-colors">Далее →</button>
          </div>
        </div>
      )}

      {/* Step 2 — Contacts */}
      {step === 2 && (
        <div className="space-y-4">
          <h3 className="text-white text-lg font-semibold mb-4">Контактные данные</h3>
          <div>
            <label className="text-white/40 text-xs mb-1 block">Имя *</label>
            <input
              type="text"
              placeholder="Ваше имя"
              value={form.client_name}
              onChange={(e) => set("client_name", e.target.value)}
              className="w-full bg-[#141414] border border-white/10 rounded-xl px-4 py-3 text-white placeholder-white/30 focus:outline-none focus:border-[#c9a96e]"
            />
          </div>
          <div>
            <label className="text-white/40 text-xs mb-1 block">Телефон *</label>
            <input
              type="tel"
              placeholder="+7 (___) ___-__-__"
              value={form.client_phone}
              onChange={(e) => set("client_phone", e.target.value)}
              className="w-full bg-[#141414] border border-white/10 rounded-xl px-4 py-3 text-white placeholder-white/30 focus:outline-none focus:border-[#c9a96e]"
            />
          </div>
          <div>
            <label className="text-white/40 text-xs mb-1 block">Email</label>
            <input
              type="email"
              placeholder="email@example.com"
              value={form.client_email}
              onChange={(e) => set("client_email", e.target.value)}
              className="w-full bg-[#141414] border border-white/10 rounded-xl px-4 py-3 text-white placeholder-white/30 focus:outline-none focus:border-[#c9a96e]"
            />
          </div>
          <div className="flex gap-3">
            <button onClick={() => setStep(1)} className="border border-white/10 text-white/50 px-6 py-3 rounded-xl hover:text-white transition-colors">← Назад</button>
            <button
              disabled={!form.client_name || !form.client_phone}
              onClick={() => setStep(3)}
              className="flex-1 bg-[#c9a96e] hover:bg-[#b8943c] disabled:opacity-40 text-black font-semibold py-3 rounded-xl transition-colors"
            >
              Далее →
            </button>
          </div>
        </div>
      )}

      {/* Step 3 — Confirm */}
      {step === 3 && (
        <div>
          <h3 className="text-white text-lg font-semibold mb-4">Подтверждение</h3>
          <div className="bg-[#141414] rounded-xl p-4 space-y-2 mb-6 text-sm">
            {[
              ["Тип события", EVENT_TYPES.find(t => t.value === form.event_type)?.label],
              ["Дата", form.event_date],
              ["Место", form.event_location],
              ["Бюджет", form.budget ? `${form.budget} ₽` : null],
              ["Имя", form.client_name],
              ["Телефон", form.client_phone],
              ["Email", form.client_email],
            ].filter(([, v]) => v).map(([k, v]) => (
              <div key={String(k)} className="flex justify-between">
                <span className="text-white/40">{k}</span>
                <span className="text-white">{v}</span>
              </div>
            ))}
          </div>
          <div className="flex gap-3">
            <button onClick={() => setStep(2)} className="border border-white/10 text-white/50 px-6 py-3 rounded-xl hover:text-white transition-colors">← Назад</button>
            <button
              onClick={submit}
              disabled={loading}
              className="flex-1 bg-[#c9a96e] hover:bg-[#b8943c] disabled:opacity-40 text-black font-semibold py-4 rounded-xl transition-colors text-lg"
            >
              {loading ? "Отправляю..." : "Отправить заявку ✓"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
