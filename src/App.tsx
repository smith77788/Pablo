import { BrowserRouter, Routes, Route, Link, useLocation } from "react-router-dom";
import Index from "@/pages/Index";
import Catalog from "@/pages/Catalog";
import ModelDetail from "@/pages/ModelDetail";
import Booking from "@/pages/Booking";
import Admin from "@/pages/Admin";

function Navbar() {
  const { pathname } = useLocation();
  const isAdmin = pathname.startsWith("/admin");
  if (isAdmin) return null;

  return (
    <header className="fixed top-0 left-0 right-0 z-40 bg-[#080808]/90 backdrop-blur border-b border-white/6">
      <div className="max-w-7xl mx-auto px-4 h-16 flex items-center justify-between">
        <Link to="/" className="font-playfair text-xl text-white">
          NEVESTY <span className="text-[#c9a96e] italic">Models</span>
        </Link>
        <nav className="hidden md:flex items-center gap-8">
          <Link to="/catalog" className="text-white/60 hover:text-[#c9a96e] text-sm transition-colors">Каталог</Link>
          <Link to="/booking" className="text-white/60 hover:text-[#c9a96e] text-sm transition-colors">Заявка</Link>
        </nav>
        <Link
          to="/booking"
          className="bg-[#c9a96e] hover:bg-[#b8943c] text-black text-sm font-semibold px-5 py-2 rounded-xl transition-colors"
        >
          Оставить заявку
        </Link>
      </div>
    </header>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Navbar />
      <Routes>
        <Route path="/" element={<Index />} />
        <Route path="/catalog" element={<Catalog />} />
        <Route path="/model/:id" element={<ModelDetail />} />
        <Route path="/booking" element={<Booking />} />
        <Route path="/admin/*" element={<Admin />} />
      </Routes>
    </BrowserRouter>
  );
}
