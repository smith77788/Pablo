import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { supabase } from "@/integrations/supabase/client";
import { ModelCard } from "@/components/catalog/ModelCard";
import type { Model } from "@/types";

export default function Index() {
  const [featuredModels, setFeaturedModels] = useState<Model[]>([]);
  const [reviews, setReviews] = useState<{ author: string; text: string; rating: number }[]>([]);
  const [stats, setStats] = useState({ models: 0, projects: 0, years: 5, satisfaction: 98 });
  const [quickForm, setQuickForm] = useState({ name: "", phone: "" });
  const [submitted, setSubmitted] = useState(false);
  const statsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    supabase
      .from("models")
      .select("*")
      .eq("is_active", true)
      .eq("featured", true)
      .limit(6)
      .then(({ data }) => data && setFeaturedModels(data as Model[]));

    supabase
      .from("reviews")
      .select("client_name, text, rating")
      .eq("approved", true)
      .order("created_at", { ascending: false })
      .limit(3)
      .then(({ data }) =>
        data && setReviews(data.map((r) => ({ author: r.client_name, text: r.text, rating: r.rating })))
      );

    supabase
      .from("models")
      .select("id", { count: "exact", head: true })
      .eq("is_active", true)
      .then(({ count }) => count && setStats((s) => ({ ...s, models: count })));
  }, []);

  async function handleQuickSubmit(e: React.FormEvent) {
    e.preventDefault();
    await supabase.from("bookings").insert({
      client_name: quickForm.name,
      client_phone: quickForm.phone,
      event_type: "other",
      status: "new",
    });
    setSubmitted(true);
  }

  return (
    <div className="min-h-screen bg-[#080808] text-white">
      {/* HERO */}
      <section className="relative min-h-screen flex items-center justify-center overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-b from-black/60 via-black/40 to-[#080808]" />
        <div
          className="absolute inset-0 bg-cover bg-center"
          style={{ backgroundImage: "url('/hero-bg.jpg')" }}
        />
        <div className="relative z-10 text-center px-4 max-w-4xl mx-auto">
          <p className="text-[#c9a96e] tracking-[0.3em] text-sm uppercase mb-6">Модельное агентство</p>
          <h1 className="font-playfair text-6xl md:text-8xl font-bold mb-4">
            NEVESTY{" "}
            <span className="text-[#c9a96e] italic">Models</span>
          </h1>
          <p className="text-white/70 text-xl mb-12">Элегантность. Стиль. Совершенство.</p>

          {/* Quick form */}
          {submitted ? (
            <div className="bg-[#c9a96e]/10 border border-[#c9a96e]/30 rounded-2xl p-6 max-w-md mx-auto">
              <p className="text-[#c9a96e] text-lg font-medium">Спасибо! Мы свяжемся с вами.</p>
            </div>
          ) : (
            <form
              onSubmit={handleQuickSubmit}
              className="flex flex-col sm:flex-row gap-3 max-w-md mx-auto mb-8"
            >
              <input
                required
                placeholder="Ваше имя"
                value={quickForm.name}
                onChange={(e) => setQuickForm((f) => ({ ...f, name: e.target.value }))}
                className="flex-1 bg-white/10 border border-white/20 rounded-xl px-4 py-3 text-white placeholder-white/50 focus:outline-none focus:border-[#c9a96e]"
              />
              <input
                required
                placeholder="Телефон"
                value={quickForm.phone}
                onChange={(e) => setQuickForm((f) => ({ ...f, phone: e.target.value }))}
                className="flex-1 bg-white/10 border border-white/20 rounded-xl px-4 py-3 text-white placeholder-white/50 focus:outline-none focus:border-[#c9a96e]"
              />
              <button
                type="submit"
                className="bg-[#c9a96e] hover:bg-[#b8943c] text-black font-semibold px-6 py-3 rounded-xl transition-colors whitespace-nowrap"
              >
                Оставить заявку
              </button>
            </form>
          )}

          <div className="flex gap-4 justify-center">
            <Link
              to="/catalog"
              className="border border-[#c9a96e] text-[#c9a96e] hover:bg-[#c9a96e] hover:text-black px-8 py-3 rounded-xl transition-colors font-medium"
            >
              Смотреть каталог
            </Link>
            <Link
              to="/prices"
              className="border border-white/20 text-white/70 hover:border-white hover:text-white px-8 py-3 rounded-xl transition-colors font-medium"
            >
              Узнать цены
            </Link>
          </div>
        </div>
      </section>

      {/* STATS */}
      <section ref={statsRef} className="py-16 border-y border-white/6">
        <div className="max-w-4xl mx-auto grid grid-cols-2 md:grid-cols-4 gap-8 px-4 text-center">
          {[
            { val: stats.models || 200, suffix: "+", label: "Моделей" },
            { val: 1000, suffix: "+", label: "Проектов" },
            { val: stats.years, suffix: "", label: "Лет на рынке" },
            { val: stats.satisfaction, suffix: "%", label: "Довольных клиентов" },
          ].map(({ val, suffix, label }) => (
            <div key={label}>
              <div className="text-4xl font-bold text-[#c9a96e]">{val}{suffix}</div>
              <div className="text-white/50 mt-1 text-sm">{label}</div>
            </div>
          ))}
        </div>
      </section>

      {/* FEATURED MODELS */}
      <section className="py-20 px-4 max-w-7xl mx-auto">
        <div className="text-center mb-12">
          <span className="text-[#c9a96e] text-xs tracking-[0.3em] uppercase">Наши модели</span>
          <h2 className="font-playfair text-4xl md:text-5xl mt-2">Звёзды агентства</h2>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
          {featuredModels.map((model) => (
            <ModelCard key={model.id} model={model} />
          ))}
        </div>
        <div className="text-center mt-12">
          <Link
            to="/catalog"
            className="inline-block border border-[#c9a96e] text-[#c9a96e] hover:bg-[#c9a96e] hover:text-black px-10 py-3 rounded-xl transition-colors font-medium"
          >
            Смотреть весь каталог
          </Link>
        </div>
      </section>

      {/* SERVICES */}
      <section className="py-20 bg-[#0e0e0e]">
        <div className="max-w-6xl mx-auto px-4">
          <div className="text-center mb-12">
            <h2 className="font-playfair text-4xl">Наши услуги</h2>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
            {[
              { icon: "👗", title: "Показы мод", desc: "Подиум, презентации брендов, модные показы" },
              { icon: "📸", title: "Фотосъёмки", desc: "Каталоги, лукбуки, рекламные кампании" },
              { icon: "🎉", title: "Мероприятия", desc: "Промо-акции, выставки, корпоративы" },
              { icon: "🎬", title: "Видеосъёмки", desc: "Рекламные ролики, клипы, кино" },
            ].map(({ icon, title, desc }) => (
              <div
                key={title}
                className="bg-[#141414] border border-white/6 rounded-2xl p-6 hover:border-[#c9a96e]/30 transition-colors"
              >
                <div className="text-4xl mb-4">{icon}</div>
                <h3 className="text-lg font-semibold mb-2">{title}</h3>
                <p className="text-white/50 text-sm">{desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* REVIEWS */}
      {reviews.length > 0 && (
        <section className="py-20 px-4 max-w-5xl mx-auto">
          <div className="text-center mb-12">
            <h2 className="font-playfair text-4xl">Отзывы клиентов</h2>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
            {reviews.map((r, i) => (
              <div key={i} className="bg-[#0e0e0e] border border-white/6 rounded-2xl p-6">
                <div className="flex mb-3">
                  {Array.from({ length: r.rating }).map((_, j) => (
                    <span key={j} className="text-[#c9a96e]">★</span>
                  ))}
                </div>
                <p className="text-white/70 text-sm mb-4 italic">"{r.text}"</p>
                <p className="text-[#c9a96e] font-medium text-sm">{r.author}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* CTA */}
      <section className="py-20 bg-[#0e0e0e] text-center px-4">
        <h2 className="font-playfair text-4xl mb-4">Готовы начать?</h2>
        <p className="text-white/50 mb-8">Оставьте заявку и мы подберём идеальную модель для вашего проекта</p>
        <Link
          to="/booking"
          className="inline-block bg-[#c9a96e] hover:bg-[#b8943c] text-black font-semibold px-12 py-4 rounded-xl transition-colors text-lg"
        >
          Оставить заявку
        </Link>
      </section>
    </div>
  );
}
