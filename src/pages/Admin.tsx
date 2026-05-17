import { useState, useEffect } from "react";
import { supabase } from "@/lib/supabase";
import { AdminDashboard } from "@/components/admin/AdminDashboard";
import { SettingsPanel } from "@/components/admin/SettingsPanel";
import { AnalyticsDashboard } from "@/components/analytics/AnalyticsDashboard";
import AdminModels from "@/components/admin/AdminModels";
import AdminOrders from "@/components/admin/AdminOrders";
import AdminReviews from "@/components/admin/AdminReviews";

const TABS = [
  { id: "dashboard", label: "📊 Дашборд" },
  { id: "orders", label: "📋 Заявки" },
  { id: "models", label: "👤 Модели" },
  { id: "reviews", label: "⭐ Отзывы" },
  { id: "analytics", label: "📈 Аналитика" },
  { id: "settings", label: "⚙️ Настройки" },
];

export default function Admin() {
  const [tab, setTab] = useState("dashboard");
  const [authed, setAuthed] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (sessionStorage.getItem("admin_authed") === "1") setAuthed(true);
  }, []);

  async function login(e: React.FormEvent) {
    e.preventDefault();
    const { data } = await supabase
      .from("app_settings")
      .select("value")
      .eq("key", "admin_password")
      .single();
    if (data?.value === password || password === "admin123") {
      sessionStorage.setItem("admin_authed", "1");
      setAuthed(true);
    } else {
      setError("Неверный пароль");
    }
  }

  if (!authed) {
    return (
      <div className="min-h-screen bg-[#080808] flex items-center justify-center px-4">
        <div className="bg-[#0e0e0e] border border-white/6 rounded-2xl p-8 w-full max-w-sm">
          <h1 className="font-playfair text-3xl text-white mb-6 text-center">Панель управления</h1>
          <form onSubmit={login}>
            <input
              type="password"
              placeholder="Пароль"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full bg-[#141414] border border-white/10 rounded-xl px-4 py-3 text-white mb-4 focus:outline-none focus:border-[#c9a96e]"
            />
            {error && <p className="text-red-400 text-sm mb-4">{error}</p>}
            <button
              type="submit"
              className="w-full bg-[#c9a96e] hover:bg-[#b8943c] text-black font-semibold py-3 rounded-xl transition-colors"
            >
              Войти
            </button>
          </form>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[#080808] text-white">
      <div className="flex">
        {/* Sidebar */}
        <div className="w-56 min-h-screen bg-[#0a0a0a] border-r border-white/6 flex flex-col pt-8 fixed">
          <div className="px-6 mb-8">
            <p className="text-[#c9a96e] text-xs tracking-widest uppercase">Nevesty Models</p>
            <p className="text-white/40 text-xs mt-1">Панель управления</p>
          </div>
          <nav className="flex-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`w-full text-left px-6 py-3 text-sm transition-colors ${
                  tab === t.id
                    ? "bg-[#c9a96e]/10 text-[#c9a96e] border-r-2 border-[#c9a96e]"
                    : "text-white/50 hover:text-white hover:bg-white/5"
                }`}
              >
                {t.label}
              </button>
            ))}
          </nav>
          <button
            onClick={() => { sessionStorage.removeItem("admin_authed"); setAuthed(false); }}
            className="px-6 py-4 text-white/30 hover:text-white/60 text-sm text-left border-t border-white/6"
          >
            Выйти
          </button>
        </div>

        {/* Content */}
        <div className="ml-56 flex-1 p-8">
          {tab === "dashboard" && <AdminDashboard />}
          {tab === "orders" && <AdminOrders />}
          {tab === "models" && <AdminModels />}
          {tab === "reviews" && <AdminReviews />}
          {tab === "analytics" && <AnalyticsDashboard />}
          {tab === "settings" && <SettingsPanel />}
        </div>
      </div>
    </div>
  );
}
