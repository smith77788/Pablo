import { useState } from "react";
import { Link } from "react-router-dom";
import type { Model } from "@/types";

interface Props {
  model: Model;
}

export function ModelCard({ model }: Props) {
  const [wishlisted, setWishlisted] = useState(() => {
    const saved: number[] = JSON.parse(localStorage.getItem("wishlist") || "[]");
    return saved.includes(model.id);
  });

  function toggleWishlist(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    const saved: number[] = JSON.parse(localStorage.getItem("wishlist") || "[]");
    const next = wishlisted ? saved.filter((x) => x !== model.id) : [...saved, model.id];
    localStorage.setItem("wishlist", JSON.stringify(next));
    setWishlisted(!wishlisted);
  }

  return (
    <Link
      to={`/model/${model.id}`}
      className="group relative block bg-[#0e0e0e] border border-white/6 rounded-2xl overflow-hidden hover:border-[#c9a96e]/30 transition-all duration-200 hover:-translate-y-1"
    >
      <div className="aspect-[3/4] overflow-hidden bg-[#141414]">
        {model.photo_url ? (
          <img
            src={model.photo_url}
            alt={model.name}
            className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
            loading="lazy"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-white/10 text-6xl">👤</div>
        )}
      </div>

      <div className="absolute top-3 left-3 flex flex-col gap-1">
        {model.featured && (
          <span className="bg-[#c9a96e] text-black text-xs font-semibold px-2 py-0.5 rounded-full">⭐ Топ</span>
        )}
        {model.is_available === false && (
          <span className="bg-black/70 text-white/60 text-xs px-2 py-0.5 rounded-full">Занята</span>
        )}
      </div>

      <button
        onClick={toggleWishlist}
        className="absolute top-3 right-3 w-8 h-8 bg-black/50 rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
      >
        <span className={wishlisted ? "text-red-400" : "text-white/60"}>{wishlisted ? "❤️" : "🤍"}</span>
      </button>

      <div className="p-4">
        <h3 className="text-white font-semibold truncate">{model.name}</h3>
        <div className="flex items-center justify-between mt-1">
          <span className="text-white/40 text-sm">{model.city}</span>
          <span className="text-white/30 text-xs capitalize">{model.category}</span>
        </div>
        {(model.height || model.age) && (
          <div className="flex gap-3 mt-2 text-xs text-white/30">
            {model.height && <span>{model.height} см</span>}
            {model.age && <span>{model.age} лет</span>}
          </div>
        )}
      </div>
    </Link>
  );
}
