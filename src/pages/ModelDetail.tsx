import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { supabase } from "@/integrations/supabase/client";
import { BookingForm } from "@/components/booking/BookingForm";
import type { Model, Review } from "@/types";

export default function ModelDetail() {
  const { id } = useParams<{ id: string }>();
  const [model, setModel] = useState<Model | null>(null);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [activePhoto, setActivePhoto] = useState(0);
  const [showBooking, setShowBooking] = useState(false);
  const [wishlisted, setWishlisted] = useState(false);

  useEffect(() => {
    if (!id) return;
    supabase
      .from("models")
      .select("*")
      .eq("id", Number(id))
      .single()
      .then(({ data }) => data && setModel(data as Model));

    supabase
      .from("reviews")
      .select("*")
      .eq("model_id", Number(id))
      .eq("approved", true)
      .order("created_at", { ascending: false })
      .then(({ data }) => data && setReviews(data as Review[]));

    const saved = JSON.parse(localStorage.getItem("wishlist") || "[]");
    setWishlisted(saved.includes(Number(id)));
  }, [id]);

  function toggleWishlist() {
    const saved: number[] = JSON.parse(localStorage.getItem("wishlist") || "[]");
    const next = wishlisted ? saved.filter((x) => x !== Number(id)) : [...saved, Number(id)];
    localStorage.setItem("wishlist", JSON.stringify(next));
    setWishlisted(!wishlisted);
  }

  if (!model) {
    return (
      <div className="min-h-screen bg-[#080808] flex items-center justify-center">
        <div className="w-8 h-8 border-2 border-[#c9a96e] border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  const photos: string[] = model.photos || (model.photo_url ? [model.photo_url] : []);
  const avgRating = reviews.length
    ? (reviews.reduce((s, r) => s + r.rating, 0) / reviews.length).toFixed(1)
    : null;

  return (
    <div className="min-h-screen bg-[#080808] text-white pt-20">
      <div className="max-w-6xl mx-auto px-4 py-10">
        {/* Breadcrumb */}
        <div className="flex items-center gap-2 text-white/40 text-sm mb-8">
          <Link to="/" className="hover:text-[#c9a96e]">Главная</Link>
          <span>/</span>
          <Link to="/catalog" className="hover:text-[#c9a96e]">Каталог</Link>
          <span>/</span>
          <span className="text-white/70">{model.name}</span>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-12">
          {/* Photos */}
          <div>
            <div className="aspect-[3/4] rounded-2xl overflow-hidden bg-[#141414] mb-3">
              {photos[activePhoto] ? (
                <img
                  src={photos[activePhoto]}
                  alt={model.name}
                  className="w-full h-full object-cover"
                />
              ) : (
                <div className="w-full h-full flex items-center justify-center text-white/20 text-6xl">
                  👤
                </div>
              )}
            </div>
            {photos.length > 1 && (
              <div className="flex gap-2 overflow-x-auto pb-1">
                {photos.map((p, i) => (
                  <button
                    key={i}
                    onClick={() => setActivePhoto(i)}
                    className={`w-16 h-20 rounded-lg overflow-hidden flex-shrink-0 border-2 transition-colors ${
                      activePhoto === i ? "border-[#c9a96e]" : "border-transparent"
                    }`}
                  >
                    <img src={p} alt="" className="w-full h-full object-cover" />
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Info */}
          <div>
            <div className="flex items-start justify-between mb-2">
              <div>
                {model.featured && (
                  <span className="text-[#c9a96e] text-xs tracking-widest uppercase">⭐ Топ модель</span>
                )}
                <h1 className="font-playfair text-4xl mt-1">{model.name}</h1>
              </div>
              <button
                onClick={toggleWishlist}
                className={`text-2xl transition-colors ${wishlisted ? "text-red-500" : "text-white/30 hover:text-red-400"}`}
              >
                {wishlisted ? "❤️" : "🤍"}
              </button>
            </div>

            {avgRating && (
              <div className="flex items-center gap-2 mb-4">
                <span className="text-[#c9a96e]">{"★".repeat(Math.round(Number(avgRating)))}</span>
                <span className="text-white/50 text-sm">{avgRating} ({reviews.length} отзывов)</span>
              </div>
            )}

            {/* Parameters */}
            <div className="grid grid-cols-2 gap-3 mb-6">
              {[
                { label: "Город", value: model.city },
                { label: "Категория", value: model.category },
                { label: "Возраст", value: model.age ? `${model.age} лет` : null },
                { label: "Рост", value: model.height ? `${model.height} см` : null },
                { label: "Вес", value: model.weight ? `${model.weight} кг` : null },
                { label: "Размер одежды", value: model.clothing_size },
                { label: "Размер обуви", value: model.shoe_size ? `${model.shoe_size}` : null },
                { label: "Цвет волос", value: model.hair_color },
              ]
                .filter((p) => p.value)
                .map(({ label, value }) => (
                  <div key={label} className="bg-[#141414] rounded-xl px-4 py-3">
                    <div className="text-white/40 text-xs mb-1">{label}</div>
                    <div className="text-white font-medium text-sm">{value}</div>
                  </div>
                ))}
            </div>

            {model.bio && (
              <div className="mb-6">
                <h3 className="text-white/50 text-xs uppercase tracking-wider mb-2">О модели</h3>
                <p className="text-white/70 text-sm leading-relaxed">{model.bio}</p>
              </div>
            )}

            <div className="flex gap-3">
              <button
                onClick={() => setShowBooking(true)}
                className="flex-1 bg-[#c9a96e] hover:bg-[#b8943c] text-black font-semibold py-4 rounded-xl transition-colors text-lg"
              >
                Забронировать
              </button>
              <Link
                to="/catalog"
                className="border border-white/20 hover:border-white/40 text-white/60 hover:text-white px-6 py-4 rounded-xl transition-colors"
              >
                ← Каталог
              </Link>
            </div>
          </div>
        </div>

        {/* Reviews */}
        {reviews.length > 0 && (
          <div className="mt-16">
            <h2 className="font-playfair text-3xl mb-6">Отзывы</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {reviews.map((r) => (
                <div key={r.id} className="bg-[#0e0e0e] border border-white/6 rounded-2xl p-5">
                  <div className="flex items-center justify-between mb-3">
                    <span className="font-medium">{r.client_name}</span>
                    <span className="text-[#c9a96e]">{"★".repeat(r.rating)}</span>
                  </div>
                  <p className="text-white/60 text-sm">{r.text}</p>
                  {r.admin_reply && (
                    <div className="mt-3 pl-3 border-l border-[#c9a96e]/30 text-white/40 text-sm italic">
                      {r.admin_reply}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Booking modal */}
      {showBooking && (
        <div className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4">
          <div className="bg-[#0e0e0e] rounded-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto relative">
            <button
              onClick={() => setShowBooking(false)}
              className="absolute top-4 right-4 text-white/40 hover:text-white text-xl z-10"
            >
              ✕
            </button>
            <BookingForm modelId={model.id} modelName={model.name} onSuccess={() => setShowBooking(false)} />
          </div>
        </div>
      )}
    </div>
  );
}
