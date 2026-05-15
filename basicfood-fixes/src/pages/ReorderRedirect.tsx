import { useEffect, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useReorder } from "@/hooks/useReorder";
import { useAuth } from "@/contexts/AuthContext";
import { Loader2 } from "lucide-react";

/**
 * /reorder/:orderId — universal entry point for 1-click reorder.
 *
 * Used by:
 *   - Push-notification deep-links (Android FCM: `basicfood://reorder/<id>`)
 *   - Email "Order again" CTAs
 *   - Telegram bot inline buttons
 *   - QR codes on packaging
 *
 * Single source of truth: delegates to useReorder() — same engine that
 * powers the in-app ReorderButton, ensuring identical pricing/wholesale
 * behaviour across channels.
 */
const ReorderRedirect = () => {
  const { orderId } = useParams<{ orderId: string }>();
  const { reorder, isReordering } = useReorder();
  const { isLoading } = useAuth();
  const navigate = useNavigate();
  const hasRunRef = useRef(false);

  useEffect(() => {
    if (!orderId || hasRunRef.current || isLoading) return;
    hasRunRef.current = true;
    void reorder(orderId, { source: "deep_link", redirect: true })
      .then((ok) => { if (!ok) navigate("/profile"); })
      .catch(() => { navigate("/profile"); });
  }, [orderId, reorder, isLoading, navigate]);

  return (
    <div className="min-h-screen flex flex-col items-center justify-center gap-4 px-6 text-center">
      <Loader2 className="w-10 h-10 text-primary animate-spin" />
      <p className="text-muted-foreground text-sm max-w-xs">
        {isReordering || isLoading
          ? "Готуємо ваше попереднє замовлення..."
          : "Перенаправляємо до оформлення..."}
      </p>
    </div>
  );
};

export default ReorderRedirect;
