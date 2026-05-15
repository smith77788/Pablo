import { useState, useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useCart } from "@/contexts/CartContext";
import { useAuth } from "@/contexts/AuthContext";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import { useToast } from "@/hooks/use-toast";
import { showAddedToCartToast } from "@/lib/cartToast";
import { useNavigate, Link, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Tag, Plus, Gift, Package } from "lucide-react";
import CheckoutTrustBadges from "@/components/CheckoutTrustBadges";
import FreeShippingProgress from "@/components/FreeShippingProgress";
import SmartCheckoutUpsell from "@/components/SmartCheckoutUpsell";
import PhoneInputUA from "@/components/PhoneInputUA";
import NovaPoshtaPicker from "@/components/NovaPoshtaPicker";
import PreferredDeliveryDatePicker from "@/components/PreferredDeliveryDatePicker";
import { trackBeginCheckout, trackPurchase } from "@/lib/analytics";
import { acos } from "@/lib/acos";
import { getProductImage } from "@/components/CatalogSection";
import { usePricedCart } from "@/hooks/usePricedCart";
import { parseWeightToGrams } from "@/contexts/PriceModeContext";
import PriceModeToggle from "@/components/PriceModeToggle";
import { getLangFromPath, withLangPrefix } from "@/i18n";
import Seo from "@/components/Seo";
import {
  FREE_SHIPPING_THRESHOLD as FREE_DELIVERY_THRESHOLD,
  NP_BRANCH_FEE as DELIVERY_BASE_BRANCH,
  NP_PARCEL_LOCKER_FEE as DELIVERY_BASE_PARCEL_LOCKER,
  NP_COMMISSION_RATE,
  freeShippingThresholdFor,
} from "@/lib/shipping";
import {
  evaluateWholesaleEligibility,
  wholesaleShortfallHint,
} from "@/lib/wholesaleEligibility";

const Checkout = () => {
  const { items, clearCart, addItem } = useCart();
  const { lines, total: subtotalWithMode, mode } = usePricedCart();
  const totalPrice = subtotalWithMode;
  const { user } = useAuth();
  const { toast } = useToast();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const lang = getLangFromPath(pathname);
  const lp = (p: string) => withLangPrefix(p, lang);
  const [loading, setLoading] = useState(false);
  const [submitStatus, setSubmitStatus] = useState<string | null>(null);
  const [redirecting, setRedirecting] = useState(false);
  const [successOrder, setSuccessOrder] = useState<{ id: string; total: number } | null>(null);
  const activeReorderPlanId = typeof window !== "undefined" ? sessionStorage.getItem("active_reorder_plan_id") : null;
  const subscriptionDiscount = activeReorderPlanId ? Math.round(totalPrice * 0.05) : 0;

  // Persisted draft — щоб користувач не втратив дані якщо випадково закрив вкладку
  // або сторінка перезавантажилась (поширений сценарій з Apple Pay).
  // 24 години TTL — після цього вважаємо що користувач не зацікавлений.
  const DRAFT_KEY = "checkout_draft_v2";
  const DRAFT_TTL_MS = 24 * 60 * 60 * 1000;

  const loadDraft = () => {
    try {
      const raw = localStorage.getItem(DRAFT_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed?.savedAt || Date.now() - parsed.savedAt > DRAFT_TTL_MS) {
        localStorage.removeItem(DRAFT_KEY);
        return null;
      }
      return parsed.form;
    } catch {
      return null;
    }
  };

  const [form, setForm] = useState(() => {
    const saved = loadDraft();
    return saved || {
      customer_name: "",
      customer_surname: "",
      customer_phone: "",
      customer_email: "",
      city: "",
      warehouse: "",
      message: "",
      payment_method: "cash_on_delivery",
      delivery_type: "branch" as "branch" | "parcel_locker",
      promo_code: "",
      is_gift: false,
      gift_message: "",
      preferred_delivery_date: null as string | null,
    };
  });

  const [agreed, setAgreed] = useState(false);
  const [promoApplied, setPromoApplied] = useState<{ id: string; discount_type: string; discount_value: number } | null>(null);
  const [promoError, setPromoError] = useState("");
  const [promoLoading, setPromoLoading] = useState(false);

  // Active reorder plan? Auto-suggest PLAN5. Cheap query, runs only when logged in.
  const { data: hasActivePlan } = useQuery({
    queryKey: ["has-active-reorder-plan", user?.id],
    enabled: !!user?.id,
    staleTime: 60_000,
    queryFn: async () => {
      const { count } = await supabase
        .from("reorder_plans")
        .select("id", { count: "exact", head: true })
        .eq("user_id", user!.id)
        .eq("is_active", true);
      return (count ?? 0) > 0;
    },
  });

  // Auto-save draft on every form change (debounced via natural React batching).
  // Cleared after successful order submit.
  useEffect(() => {
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify({ savedAt: Date.now(), form }));
    } catch {
      // localStorage may be full or disabled — silently skip
    }
  }, [form]);

  // Auto-apply pending promo from /cart?promo=CODE deep-link.
  // Reads from sessionStorage (set by Cart) or directly from URL (?promo=) for
  // direct /checkout?promo=… links. Validates server-side via validate_promo_code,
  // re-runs once cart total stabilises (min_order_amount check needs subtotal).
  // Failure mode is intentionally quiet — see plan: don't toast on auto-apply.
  const autoPromoTriedRef = useRef<string | null>(null);
  useEffect(() => {
    if (promoApplied || form.promo_code) return;
    if (totalPrice <= 0) return;
    let code: string | null = null;
    try { code = sessionStorage.getItem("pending_promo"); } catch { /* disabled */ }
    if (!code) {
      const fromUrl = new URLSearchParams(window.location.search).get("promo");
      if (fromUrl) code = fromUrl.trim().toUpperCase();
    }
    if (!code || !/^[A-Z0-9_-]{3,32}$/.test(code)) return;
    if (autoPromoTriedRef.current === code) return;
    autoPromoTriedRef.current = code;
    (async () => {
      const { data, error } = await supabase.rpc("validate_promo_code", {
        p_code: code,
        p_order_amount: totalPrice,
      });
      try { sessionStorage.removeItem("pending_promo"); } catch { /* ignore */ }
      if (error || !data || data.length === 0) return;
      const promo = data[0];
      if (!promo.is_valid || !promo.id) {
        // Below-min / expired / inactive — leave the code visible in the input
        // so the user can see what was attempted, but don't show an error toast
        // (it was an automatic action, not a user-initiated submit).
        setForm((f) => ({ ...f, promo_code: code as string }));
        return;
      }
      setForm((f) => ({ ...f, promo_code: code as string }));
      setPromoApplied({ id: promo.id, discount_type: promo.discount_type, discount_value: promo.discount_value });
      const isPct = ["percent","percentage"].includes(promo.discount_type);
      toast({
        title: t("checkout.promo_applied_toast_title", { code }),
        description: isPct
          ? t("checkout.promo_applied_toast_pct", { value: promo.discount_value })
          : t("checkout.promo_applied_toast_amount", { value: promo.discount_value }),
      });
    })();
  }, [totalPrice, promoApplied, form.promo_code, toast, t]);


  const { data: paymentSettings } = useQuery({
    queryKey: ["site-settings", "payment_methods"],
    queryFn: async () => {
      const { data } = await supabase.from("site_settings").select("value").eq("key", "payment_methods").single();
      return data?.value as Record<string, { enabled: boolean; label: string; details?: string }> | null;
    },
  });

  // Upsell — products not in cart. Read from `products_public` view (public surface,
  // never exposes wholesale_price to non-wholesale users — see security migration).
  const { data: upsellProducts = [] } = useQuery({
    queryKey: ["upsell-products"],
    queryFn: async () => {
      const { data } = await supabase
        .from("products_public" as never)
        .select("*")
        .eq("is_active", true)
        .order("sort_order")
        .limit(8);
      return (data as never[]) || [];
    },
  });

  // Pre-fill from default address
  const { data: defaultAddress } = useQuery({
    queryKey: ["default-address", user?.id],
    queryFn: async () => {
      const { data } = await supabase.from("user_addresses").select("*").eq("user_id", user!.id).eq("is_default", true).maybeSingle();
      return data;
    },
    enabled: !!user,
  });

  useEffect(() => {
    if (user) {
      setForm((f) => ({
        ...f,
        customer_email: user.email || f.customer_email,
        customer_name: user.user_metadata?.display_name || f.customer_name,
      }));
    }
  }, [user]);

  // ACOS: track checkout page view (real begin_checkout — fires when user *reaches* the page,
  // not when they submit). This unblocks accurate funnel measurement (was conflating reach + submit).
  useEffect(() => {
    if (items.length === 0) return;
    acos({
      event_type: "checkout_viewed",
      metadata: { item_count: items.length, cart_value: totalPrice, has_user: !!user },
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ACOS: track abandoned checkout — if user leaves the page after starting to fill the form
  // but before submit, emit a recoverable signal for cart-recovery / winback engines.
  useEffect(() => {
    const startedFilling = !!(form.customer_name || form.customer_phone || form.city);
    if (!startedFilling || items.length === 0) return;
    const handleLeave = () => {
      const payload = JSON.stringify({
        event_type: "checkout_abandoned",
        session_id: sessionStorage.getItem("acos_sid") || null,
        source: "site",
        metadata: {
          cart_value: totalPrice,
          item_count: items.length,
          filled_name: !!form.customer_name,
          filled_phone: !!form.customer_phone,
          filled_address: !!form.city && !!form.warehouse,
          payment_method: form.payment_method,
        },
      });
      // Supabase REST requires apikey/Authorization headers, which sendBeacon
      // CANNOT set (it only allows Content-Type via Blob). Without these the
      // request is rejected with 401 → no abandonment events ever recorded.
      // Use fetch({ keepalive: true }) which DOES allow custom headers and
      // survives page unload on all modern browsers (incl. iOS Safari ≥13).
      try {
        const apikey = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY as string;
        void fetch(`${import.meta.env.VITE_SUPABASE_URL}/rest/v1/events`, {
          method: "POST",
          keepalive: true,
          headers: {
            "Content-Type": "application/json",
            apikey,
            Authorization: `Bearer ${apikey}`,
          },
          body: payload,
        }).catch(() => { /* best-effort */ });
      } catch {
        // best-effort only — never break UX
      }
    };
    window.addEventListener("pagehide", handleLeave);
    return () => window.removeEventListener("pagehide", handleLeave);
  }, [form.customer_name, form.customer_phone, form.city, form.warehouse, form.payment_method, items.length, totalPrice]);

  // Prefill from default address — runs ONCE per session per user.
  // Without the ref guard, useQuery refetches (window focus, network reconnect, etc.)
  // would re-trigger this effect and overwrite anything the user has typed in city/warehouse.
  // That manifested as "city resets while typing" on mobile keyboards (focus events).
  const prefilledRef = useRef<string | null>(null);
  useEffect(() => {
    if (!defaultAddress) return;
    if (prefilledRef.current === defaultAddress.id) return;
    prefilledRef.current = defaultAddress.id;
    setForm((f) => ({
      ...f,
      city: f.city || defaultAddress.city || "",
      warehouse: f.warehouse || defaultAddress.address || "",
      customer_phone: f.customer_phone || defaultAddress.phone || "",
    }));
  }, [defaultAddress]);

  const defaultPayments: Array<[string, { enabled: boolean; label: string; details?: string }]> = [
    ["cash_on_delivery", { enabled: true, label: t("checkout.payment_cod_label"), details: t("checkout.payment_cod_details") }],
    ["card_transfer", { enabled: true, label: t("checkout.payment_card_label"), details: t("checkout.payment_card_details") }],
  ];
  // Monobank Acquiring (card_online) тимчасово вимкнено — фільтруємо незалежно від site_settings.
  // "Карткою онлайн" доступно через card_transfer (адмін задає реквізити в налаштуваннях).
  const enabledPayments = (paymentSettings
    ? Object.entries(paymentSettings).filter(([, v]) => v.enabled)
    : defaultPayments
  ).filter(([k]) => k !== "card_online");
  if (enabledPayments.length === 0) {
    enabledPayments.push(defaultPayments[0]);
  }

  // Defensive: clamp discount to [0, totalPrice]. Even if validate_promo_code
  // returns a malformed discount_value (regression, malicious payload, future
  // schema change), the order total can never go negative and we never quote
  // a bigger discount than the cart actually contains.
  const rawDiscount = promoApplied
    ? ["percent", "percentage"].includes(promoApplied.discount_type)
      ? Math.round(totalPrice * Math.max(0, promoApplied.discount_value) / 100)
      : Math.max(0, promoApplied.discount_value)
    : 0;
  const discountAmount = Math.min(rawDiscount, totalPrice);

  const subtotalAfterDiscount = Math.max(0, totalPrice - discountAmount);
  // ACOS decision: shipping is no longer billed to the customer at checkout.
  // Customer pays for goods only — delivery cost is absorbed into product margin
  // and handled separately by ops. Keep variables (`deliveryCost`, `npCommission`)
  // as 0 to minimise downstream churn (OrderSuccess, notify-telegram, sessionStorage).
  const deliveryCost = 0;
  const npCommission = 0;
  const deliveryFree = true;
  const finalTotal = subtotalAfterDiscount;

  // Bundle preview — UI hint only. Authoritative value is computed server-side
  // inside create_order_with_items RPC and returned in its result for reconcile.
  const [bundleDiscount, setBundleDiscount] = useState(0);
  useEffect(() => {
    if (!items || items.length < 2) { setBundleDiscount(0); return; }
    const payload = items.map((i: any) => ({
      product_id: i.id,
      product_price: i.price,
      quantity: i.quantity ?? 1,
    }));
    let cancelled = false;
    (async () => {
      const { data, error } = await supabase.rpc("find_best_bundle" as any, { p_items: payload as any });
      if (cancelled) return;
      if (error || !data || (Array.isArray(data) && data.length === 0)) { setBundleDiscount(0); return; }
      const row: any = Array.isArray(data) ? data[0] : data;
      setBundleDiscount(Math.max(0, Number(row?.discount_amount ?? 0)));
    })();
    return () => { cancelled = true; };
  }, [items]);

  const finalTotalWithBundle = Math.max(0, finalTotal - bundleDiscount - subscriptionDiscount);

  const cartIds = useMemo(() => new Set(items.map((i) => i.id)), [items]);
  const upsellFiltered = upsellProducts.filter((p: any) => !cartIds.has(p.id)).slice(0, 3);

  const applyPromo = async () => {
    setPromoError("");
    if (!form.promo_code.trim() || promoLoading) return;
    setPromoLoading(true);
    try {
      const { data, error } = await supabase
        .rpc("validate_promo_code", {
          p_code: form.promo_code.trim(),
          p_order_amount: totalPrice,
        });

      if (error || !data || data.length === 0) {
        setPromoError(t("checkout.promo_not_found_short"));
        setPromoApplied(null);
        return;
      }

      const promo = data[0];
      if (!promo.is_valid) {
        setPromoError(promo.error_message || t("checkout.promo_invalid_short"));
        setPromoApplied(null);
        return;
      }

      setPromoApplied({ id: promo.id, discount_type: promo.discount_type, discount_value: promo.discount_value });
      toast({ title: t("checkout.promo_applied_simple") });
    } finally {
      setPromoLoading(false);
    }
  };

  // Helper: scroll to first invalid field and focus it. Better than just toast —
  // user immediately sees what to fix without scanning the form.
  const focusField = (id: string) => {
    const el = document.getElementById(id);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      setTimeout(() => (el as HTMLInputElement).focus({ preventScroll: true }), 250);
    }
  };

  // Sensor neuron: emit a granular dropoff signal so the FunnelDropoffAnalyzer
  // can spot which exact field is killing conversions. Without this, agents see
  // only "checkout_viewed → 0 purchases" and have no idea where users die.
  const trackValidationFail = (field: string, reason: string) => {
    acos({
      event_type: "checkout_validation_failed",
      metadata: {
        field,
        reason,
        cart_value: totalPrice,
        item_count: items.length,
        payment_method: form.payment_method,
        filled_name: !!form.customer_name,
        filled_surname: !!form.customer_surname,
        filled_phone: !!form.customer_phone,
        filled_email: !!form.customer_email,
        filled_city: !!form.city,
        filled_warehouse: !!form.warehouse,
        agreed,
      },
    });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (items.length === 0) return;
    // Sensor: user actually tried to submit. This was the missing signal —
    // ratio (submit_attempted / checkout_viewed) tells us how many users even
    // reach the bottom of the form vs bounce on page load.
    acos({
      event_type: "checkout_submit_attempted",
      metadata: { item_count: items.length, cart_value: totalPrice, payment_method: form.payment_method },
    });
    if (!agreed) {
      trackValidationFail("agreed", "consent_unchecked");
      toast({ title: t("checkout.consent_required"), variant: "destructive" });
      const consentEl = document.getElementById("checkout-consent");
      consentEl?.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    if (!form.customer_name.trim() || form.customer_name.trim().length < 2) {
      trackValidationFail("customer_name", "too_short_or_empty");
      toast({ title: t("checkout.name_required"), variant: "destructive" });
      focusField("checkout-name");
      return;
    }
    if (!form.customer_surname.trim()) {
      trackValidationFail("customer_surname", "empty");
      toast({ title: t("checkout.surname_required"), variant: "destructive" });
      focusField("checkout-surname");
      return;
    }
    // Strict UA phone validation — gate against bot/spam orders.
    const phoneClean = (form.customer_phone || "").replace(/\s/g, "");
    if (!/^\+380\d{9}$/.test(phoneClean)) {
      trackValidationFail("customer_phone", phoneClean ? "wrong_format" : "empty");
      toast({
        title: t("checkout.phone_invalid_title"),
        description: t("checkout.phone_invalid_desc"),
        variant: "destructive",
      });
      return;
    }
    if (form.customer_email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.customer_email.trim())) {
      trackValidationFail("customer_email", "wrong_format");
      toast({ title: t("checkout.email_invalid_title"), description: t("checkout.email_invalid_desc"), variant: "destructive" });
      focusField("checkout-email");
      return;
    }
    if (!form.city.trim()) {
      trackValidationFail("city", "empty");
      toast({ title: t("checkout.city_required"), variant: "destructive" });
      focusField("np-city");
      return;
    }
    if (!form.warehouse.trim()) {
      trackValidationFail("warehouse", "empty");
      toast({ title: t("checkout.warehouse_required"), variant: "destructive" });
      focusField("np-warehouse");
      return;
    }

    // Wholesale-order minimum (₴ або кг). Per-SKU 1 кг already enforced by
    // usePricedCart (lines under 1 kg silently fall back to retail), so we
    // only need to check the aggregate threshold here.
    const wsEligibility = evaluateWholesaleEligibility(lines);
    if (mode === "wholesale" && !wsEligibility.noWholesaleLines && !wsEligibility.meetsThreshold) {
      trackValidationFail("wholesale_minimum", "below_threshold");
      toast({
        title: "Замало для оптового замовлення",
        description: wholesaleShortfallHint(wsEligibility),
        variant: "destructive",
      });
      return;
    }

    setLoading(true);
    setSubmitStatus(t("checkout.submit_status_initial"));
    trackBeginCheckout(finalTotal, items.length);
    acos({ event_type: "begin_checkout", metadata: { total: finalTotal, item_count: items.length, payment_method: form.payment_method } });
    try {
      const fullName = `${form.customer_surname} ${form.customer_name}`.trim();
      const deliveryAddr = `${form.city}, ${form.delivery_type === "parcel_locker" ? t("checkout.warehouse_label_locker") : t("checkout.warehouse_label_branch")}: ${form.warehouse}`;

      // Atomic order + items creation via RPC. Prevents orphan orders
      // (order persisted but items insert failed) which used to leave
      // unfulfillable rows in the admin queue.
      const orderItemsPayload = lines.map((item) => ({
        product_id: item.id,
        product_name: item.name + (item.isWholesale ? ` ${t("checkout.wholesale_marker")}` : ""),
        product_price: item.effectivePrice,
        quantity: item.quantity,
      }));

      const rpcArgs = {
        // SECURITY: do NOT send user_id / status from the client — the RPC
        // forces user_id := auth.uid() and status := 'new' for non-admins.
        // Also do NOT send subtotal / discount_amount / total — RPC ignores
        // them and recomputes server-side. See mem://constraints/checkout-server-side-attribution.
        p_order: {
          customer_name: fullName,
          customer_phone: form.customer_phone || null,
          customer_email: form.customer_email || null,
          delivery_address: deliveryAddr,
          // Field is `notes` (matches DB column + Telegram bot + public-api).
          // Previously sent as `message` and was silently dropped by the RPC.
          notes: (() => {
            const parts: string[] = [];
            if (form.is_gift) {
              parts.push(t("checkout.gift_marker"));
              if (form.gift_message?.trim()) parts.push(`${t("checkout.gift_letter_prefix")} ${form.gift_message.trim()}`);
              parts.push(t("checkout.gift_packing_note"));
            }
            if (form.message?.trim()) parts.push(form.message.trim());
            return parts.length ? parts.join("\n") : null;
          })(),
          payment_method: form.payment_method,
          promo_code_id: promoApplied?.id || null,
          source: "site",
          preferred_delivery_date: form.preferred_delivery_date || null,
          reorder_plan_id: (typeof window !== "undefined"
            ? sessionStorage.getItem("active_reorder_plan_id")
            : null) || null,
          session_id: (typeof window !== "undefined"
            ? localStorage.getItem("price_lab_session_id")
            : null) || null,
        },
        p_items: orderItemsPayload,
      };

      // Retry only transient failures (network/timeout/5xx). Validation errors
      // (22023, 23xxx) and ambiguous-column / function bugs are NOT retryable —
      // retrying them just spams the user with the same crash. Up to 2 retries
      // with 400ms / 1200ms backoff so a flaky 4G connection still completes.
      const isTransient = (err: any): boolean => {
        const msg = String(err?.message || "").toLowerCase();
        const code = String(err?.code || "");
        if (code.startsWith("PGRST")) return false;
        if (/^2[23]\d{3}$/.test(code)) return false; // 22xxx data, 23xxx integrity
        return /network|fetch|timeout|temporar|503|502|504|connection|econn/.test(msg);
      };

      let rpcRows: any = null;
      let orderError: any = null;
      let retryCount = 0;
      for (let attempt = 0; attempt < 3; attempt++) {
        if (attempt === 0) {
          setSubmitStatus(t("checkout.submit_status_initial"));
        } else {
          setSubmitStatus(t("checkout.submit_status_retry", { attempt: attempt + 1 }));
        }
        const res = await (supabase as any).rpc("create_order_with_items", rpcArgs);
        rpcRows = res.data;
        orderError = res.error;
        if (!orderError) break;
        if (!isTransient(orderError)) break;
        retryCount++;
        await new Promise((r) => setTimeout(r, 400 * Math.pow(3, attempt)));
      }
      if (retryCount > 0) {
        acos({
          event_type: "checkout_failed",
          metadata: { stage: "rpc_retry", attempts: retryCount, succeeded: !orderError, last_error: orderError?.message },
        });
      }

      if (orderError) throw orderError;
      const order = Array.isArray(rpcRows) ? rpcRows[0] : rpcRows;
      if (!order?.id) throw new Error(t("checkout.err_create_failed"));

      // ─── Side-effect RPCs (fire-and-forget) ────────────────────────────
      // These MUST NOT block / fail the checkout — order is already in DB.
      // Errors here used to bubble up and show "Помилка оформлення" to a
      // user whose order had actually been created → duplicates + support load.
      if (promoApplied) {
        (supabase as any).rpc("apply_promo_code", {
          p_promo_code_id: promoApplied.id,
          p_order_id: order.id,
          p_user_id: user?.id || null,
          p_access_token: order.access_token ?? null,
        }).then(({ error }: any) => {
          if (error) console.warn("[checkout] apply_promo_code failed:", error.message);
        });
      }

      // Apply referral code (silent best-effort) — only credits referrer
      // if this is the new user's first order. Server enforces all rules.
      (async () => {
        try {
          const refCode = (typeof window !== "undefined")
            ? (await import("@/lib/referral")).getStoredReferralCode()
            : null;
          if (refCode && user?.id) {
            const { data: refRes } = await (supabase as any).rpc("apply_referral_first_order", {
              p_referral_code: refCode,
              p_order_id: order.id,
            });
            if ((refRes as any)?.ok) {
              (await import("@/lib/referral")).clearStoredReferralCode();
            }
          }
        } catch (e) {
          console.warn("[checkout] referral apply failed:", e);
        }
      })();

      supabase.functions.invoke("notify-telegram", {
        body: {
          order_id: order.id,
          customer_name: fullName,
          customer_phone: form.customer_phone,
          customer_email: form.customer_email,
          delivery_address: deliveryAddr,
          payment_method: form.payment_method,
          total: finalTotal,
          items_count: items.length,
          items_detail: items.map(i => ({ name: i.name, qty: i.quantity, price: i.price })),
          message: form.message,
        },
      }).catch(() => {});

      trackPurchase(order.id, finalTotal, items.length);
      // ─── MARQ-ready aggregate event ─────────────────────────────────
      // One header event with full items[] + total_cents so the Revenue
      // Orchestrator can create an order row without backfill. Per-line
      // events still fire below for product_stats analytics.
      acos({
        event_type: "purchase_completed",
        order_id: order.id,
        metadata: {
          total: finalTotal,
          total_cents: Math.round(finalTotal * 100),
          currency: "UAH",
          item_count: items.length,
          payment_method: form.payment_method,
          has_promo: !!promoApplied,
          email: form.customer_email || undefined,
          name: fullName || undefined,
          phone: form.customer_phone || undefined,
          items: items.map((i) => ({
            product_id: i.id,
            name: i.name,
            quantity: i.quantity,
            price: i.price,
            price_cents: Math.round(i.price * 100),
          })),
        },
      });
      // Per-line events so product_stats.purchases_total populates per-product.
      // NOTE: distinct event_type to avoid double-counting orders in analytics
      // (header `purchase_completed` fires once above; lines fire as `purchase_line`).
      items.forEach((it) => {
        acos({
          event_type: "purchase_line",
          order_id: order.id,
          product_id: it.id,
          metadata: {
            total: finalTotal,
            total_cents: Math.round(finalTotal * 100),
            currency: "UAH",
            item_count: items.length,
            line_quantity: it.quantity,
            line_price: it.price,
            line_price_cents: Math.round(it.price * 100),
            payment_method: form.payment_method,
            has_promo: !!promoApplied,
          },
        });
      });

      // Pass items to success page before clearing
      const summary = {
        items: items.map(i => ({ name: i.name, qty: i.quantity, price: i.price })),
        total: finalTotal,
        delivery: deliveryCost,
        preferred_delivery_date: form.preferred_delivery_date || null,
      };
      sessionStorage.setItem(`order_summary_${order.id}`, JSON.stringify(summary));
      // Store the secret access token so OrderSuccess (and any return visit
      // within the same browser session) can still fetch the order details
      // even though anonymous SELECT on `orders` is no longer allowed.
      if (order.access_token) {
        try {
          sessionStorage.setItem(`order_token_${order.id}`, String(order.access_token));
        } catch {}
      }

      // Онлайн-оплата (Monobank) тимчасово вимкнена. Усі замовлення йдуть як накладений платіж.

      try { localStorage.removeItem(DRAFT_KEY); } catch { /* noop */ }
      try { sessionStorage.removeItem("active_reorder_plan_id"); } catch { /* noop */ }
      clearCart();
      // Show inline success banner with order number + actions. Auto-redirects
      // to the full order-success page after 6s so users who are reading the
      // confirmation aren't yanked away mid-read, but we still get them to the
      // detailed page eventually.
      setSuccessOrder({ id: order.id, total: finalTotal });
      window.scrollTo({ top: 0, behavior: "smooth" });
      setTimeout(() => navigate(lp("/order-success/" + order.id)), 6000);
    } catch (err: any) {
      const raw = err?.message || "unknown";
      acos({
        event_type: "checkout_failed",
        metadata: {
          stage: "submit",
          reason: raw,
          cart_value: totalPrice,
          item_count: items.length,
          payment_method: form.payment_method,
        },
      });
      // Map raw DB / network errors to friendly Ukrainian text. The user
      // should never see things like "infinite recursion in policy" or
      // "duplicate key value violates" — those are diagnostics for us.
      const friendly = (() => {
        const m = raw.toLowerCase();
        const code = String((err as any)?.code || "");
        if (m.includes("ambiguous") || m.includes("does not exist") || m.includes("undefined function") || code === "42703" || code === "42883" || code === "42702")
          return "Тимчасова технічна помилка на сервері. Ми вже отримали сповіщення — спробуйте через хвилину або напишіть нам у чат.";
        if (m.includes("recursion") || m.includes("policy") || m.includes("rls") || code === "42P17")
          return "Сталася технічна помилка при оформленні. Спробуйте ще раз або напишіть нам у чат — ми оформимо вручну.";
        if (m.includes("duplicate") || m.includes("conflict") || code === "23505")
          return "Це замовлення вже було створено. Перевірте свою пошту або профіль.";
        if (m.includes("network") || m.includes("fetch") || m.includes("timeout") || m.includes("econn"))
          return "Немає зв'язку з сервером. Перевірте інтернет і спробуйте ще раз — ми вже автоматично робили кілька спроб.";
        if (m.includes("unavailable") || m.includes("inactive") || m.includes("out of stock") || m.includes("stock"))
          return "Один із товарів у кошику став недоступним або закінчився. Оновіть сторінку та перевірте кошик.";
        if (m.includes("invalid quantity") || m.includes("quantity"))
          return "Перевірте кількість товарів у кошику — вона має бути більшою за 0.";
        if (m.includes("invalid item") || m.includes("item shape"))
          return "Дані одного з товарів некоректні. Видаліть його з кошика та додайте знову.";
        if (m.includes("promo"))
          return "Промокод більше не дійсний. Зніміть його і спробуйте ще раз.";
        if (code === "42501" || m.includes("permission") || m.includes("not authorized"))
          return "Сесія закінчилася. Оновіть сторінку та спробуйте ще раз.";
        return `Не вдалося оформити замовлення. Спробуйте ще раз або напишіть нам у Telegram. ${code ? `(код: ${code})` : ""}`.trim();
      })();
      toast({ title: "Помилка оформлення", description: friendly, variant: "destructive" });
    } finally {
      setLoading(false);
      setSubmitStatus(null);
    }
  };

  if (redirecting) {
    return (
      <div className="min-h-screen bg-background flex flex-col items-center justify-center px-4 text-center">
        <div className="w-12 h-12 rounded-full border-2 border-primary border-t-transparent animate-spin mb-4" />
        <h1 className="text-xl font-bold mb-2">{t("checkout.redirecting_title")}</h1>
        <p className="text-sm text-muted-foreground max-w-sm">
          {t("checkout.redirecting_text")}
        </p>
      </div>
    );
  }

  if (successOrder) {
    const shortId = successOrder.id.slice(0, 8).toUpperCase();
    return (
      <div className="min-h-screen bg-background flex items-center justify-center px-4 py-12">
        <div className="w-full max-w-md bg-card border border-primary/30 rounded-2xl p-6 sm:p-8 shadow-xl text-center">
          <div className="w-14 h-14 rounded-full bg-primary/15 text-primary flex items-center justify-center mx-auto mb-4 text-3xl" aria-hidden>
            ✓
          </div>
          <h1 className="text-2xl font-bold mb-2">Замовлення оформлено!</h1>
          <p className="text-sm text-muted-foreground mb-5">
            Дякуємо за замовлення. Ми надіслали підтвердження та зв'яжемося, якщо знадобиться уточнення.
          </p>
          <div className="rounded-lg border border-border bg-background/50 p-4 mb-5 text-left space-y-1.5">
            <div className="flex justify-between text-sm">
              <span className="text-muted-foreground">Номер замовлення:</span>
              <span className="font-mono font-semibold">#{shortId}</span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-muted-foreground">Сума:</span>
              <span className="font-semibold text-primary">{successOrder.total} ₴</span>
            </div>
          </div>
          <div className="flex flex-col gap-2">
            <Link to={lp("/order-success/" + successOrder.id)}>
              <Button className="w-full bg-primary text-primary-foreground">
                Деталі замовлення
              </Button>
            </Link>
            {user ? (
              <Link to={`${lp("/profile")}?tab=orders`}>
                <Button variant="outline" className="w-full">
                  <Package className="w-4 h-4 mr-2" /> Мої замовлення
                </Button>
              </Link>
            ) : (
              <Link to={lp("/catalog")}>
                <Button variant="outline" className="w-full">Продовжити покупки</Button>
              </Link>
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-4">
            Через кілька секунд відкриємо повну сторінку замовлення…
          </p>
        </div>
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="min-h-screen bg-background flex flex-col items-center justify-center px-4">
        <h1 className="text-2xl font-bold mb-4">{t("checkout.empty_cart_title")}</h1>
        <Link to={lp("/catalog")}>
          <Button className="bg-primary text-primary-foreground">{t("checkout.go_to_catalog")}</Button>
        </Link>
      </div>
    );
  }

  return (
    <div className="min-h-screen pb-20 md:pb-0">
      <Seo title="Оформлення замовлення — BASIC.FOOD" description="Оформіть замовлення BASIC.FOOD з доставкою по Україні" canonicalPath="/checkout" noindex />
      <div className="container mx-auto px-4 py-8 max-w-2xl">
        <Link to={lp("/")} className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground mb-6">
          <ArrowLeft size={16} /> {t("checkout.back")}
        </Link>

        <h1 className="text-2xl font-bold mb-3">{t("checkout.title")}</h1>

        {/* "30-sec, no callback" reassurance banner — mirrors Hero promise so
            the message stays consistent through the funnel and removes any
            "will someone phone me?" objection right before form fill. */}
        <div className="flex items-center gap-2 mb-4 px-3 py-2 rounded-lg bg-primary/10 border border-primary/25 text-xs sm:text-sm">
          <span className="text-primary text-base leading-none shrink-0">⚡</span>
          <span className="text-foreground/90">
            {lang === "en"
              ? "Self-service checkout in ~30 seconds. Pay online — we don't call you back."
              : "Самостійне оформлення за ~30 секунд. Оплата онлайн — ми не передзвонюємо."}
          </span>
        </div>

        {/* Urgency: same-day shipping cutoff (Kyiv tz). Drives commitment via loss aversion. */}
        {(() => {
          const now = new Date();
          const kyivHour = (now.getUTCHours() + 2) % 24;
          const beforeCutoff = kyivHour < 16;
          return (
            <div
              className={`inline-flex items-center gap-2 mb-5 px-3 py-1.5 rounded-full text-xs font-medium border ${
                beforeCutoff
                  ? "bg-[hsl(var(--success)/0.12)] border-[hsl(var(--success)/0.35)] text-[hsl(var(--success))]"
                  : "bg-primary/10 border-primary/30 text-primary"
              }`}
              role="status"
            >
              <span className="relative flex h-2 w-2">
                <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-70 ${beforeCutoff ? "bg-[hsl(var(--success))]" : "bg-primary"}`} />
                <span className={`relative inline-flex h-2 w-2 rounded-full ${beforeCutoff ? "bg-[hsl(var(--success))]" : "bg-primary"}`} />
              </span>
              {beforeCutoff
                ? t("checkout.urgency_today", { defaultValue: lang === "en" ? "Order before 16:00 — ships today" : "Замов до 16:00 — відправимо сьогодні" })
                : t("checkout.urgency_tomorrow", { defaultValue: lang === "en" ? "Will ship tomorrow morning" : "Відправимо завтра вранці" })}
            </div>
          );
        })()}

        {/* Order summary — collapsible on mobile to keep form above the fold. */}
        <details className="md:!open bg-card rounded-lg border border-border p-4 mb-6 group" open>
          <summary className="flex items-center justify-between gap-2 cursor-pointer list-none mb-3 md:cursor-default">
            <h2 className="font-semibold text-sm sm:text-base flex items-baseline gap-1.5 min-w-0">
              <span className="truncate">{t("checkout.summary_title")}</span>
              <span className="md:hidden text-xs text-muted-foreground font-normal whitespace-nowrap">
                · {lines.length} {lang === "en" ? "items" : "поз."}
              </span>
            </h2>
            <div className="flex items-center gap-2 shrink-0">
              <span className="md:hidden text-base font-bold text-primary whitespace-nowrap">{finalTotal} ₴</span>
              <PriceModeToggle variant="compact" />
              <svg className="md:hidden w-4 h-4 text-muted-foreground transition-transform group-open:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
            </div>
          </summary>
          {mode === "wholesale" && (
            <p className="text-[11px] text-primary mb-2">
              {t("checkout.wholesale_hint")}
            </p>
          )}
          {lines.map((item) => (
            <div key={item.id} className="flex justify-between text-sm py-1">
              <span>
                {item.name} × {item.quantity}
                {item.isWholesale && <span className="ml-1 text-[10px] text-primary font-semibold">{t("catalog.wholesale_short")}</span>}
              </span>
              <span>{item.lineTotal} ₴</span>
            </div>
          ))}
          <div className="border-t border-border mt-2 pt-2 space-y-1">
            <div className="flex justify-between text-sm">
              <span>{t("checkout.subtotal")}</span>
              <span>{totalPrice} ₴</span>
            </div>
            {discountAmount > 0 && (
              <div className="flex justify-between text-sm text-[hsl(var(--success,142_71%_45%))]">
                <span>{t("checkout.discount")}</span>
                <span>-{discountAmount} ₴</span>
              </div>
            )}
            <div className="flex justify-between text-sm text-muted-foreground">
              <span>{t("checkout.delivery_with_type", { type: form.delivery_type === "parcel_locker" ? t("checkout.delivery_parcel_locker") : t("checkout.delivery_branch") })}</span>
              <span className="text-[hsl(var(--success,142_71%_45%))] font-medium">{lang === "en" ? "Per Nova Poshta tariff" : "За тарифом Нової Пошти"}</span>
            </div>
            {bundleDiscount > 0 && (
              <div className="flex justify-between text-sm text-[hsl(var(--success,142_71%_45%))]">
                <span>🎁 Знижка за комбо <span className="text-xs text-muted-foreground">(≈ застосується автоматично)</span></span>
                <span>-{bundleDiscount} ₴</span>
              </div>
            )}
            {subscriptionDiscount > 0 && (
              <div className="flex justify-between text-sm text-[hsl(var(--success,142_71%_45%))]">
                <span>🔁 Знижка підписника <span className="text-xs text-muted-foreground">(−5%)</span></span>
                <span>-{subscriptionDiscount} ₴</span>
              </div>
            )}
            <div className="flex justify-between font-bold text-lg mt-1 pt-1 border-t border-border">
              <span>{t("checkout.to_pay")}</span>
              <span className="text-primary">{finalTotalWithBundle} ₴</span>
            </div>
          </div>
          <div className="mt-3">
            <FreeShippingProgress current={subtotalAfterDiscount} city={form.city} />
            {(() => {
              // Context-aware hint. Recomputes on every render so it always reflects
              // the LIVE state: promo applied / changed / removed, city changed, cart
              // edited. No stale values, no useMemo cache to invalidate.
              if (discountAmount <= 0) return null;
              const threshold = freeShippingThresholdFor(form.city);
              const reachedBefore = totalPrice >= threshold;
              const reachedAfter = subtotalAfterDiscount >= threshold;
              const lostByPromo = reachedBefore && !reachedAfter;
              const stillNeed = Math.max(0, threshold - subtotalAfterDiscount);
              return (
                <p
                  key={`promo-hint-${promoApplied?.id ?? "none"}-${discountAmount}-${form.city || "no-city"}`}
                  className="mt-1.5 text-[11px] leading-snug text-muted-foreground"
                >
                  {reachedAfter ? (
                    <>✅ Безкоштовна доставка вже застосована — навіть з урахуванням промокоду −{discountAmount} ₴ (поріг {threshold} ₴ від суми після знижки).</>
                  ) : lostByPromo ? (
                    <>⚠️ Промокод −{discountAmount} ₴ опустив суму нижче порогу безкоштовної доставки ({threshold} ₴). Додайте товарів ще на {stillNeed} ₴, щоб повернути її.</>
                  ) : (
                    <>ℹ️ Поріг безкоштовної доставки рахується від суми <span className="font-medium">після знижки</span> ({subtotalAfterDiscount} ₴ із {threshold} ₴). Промокод −{discountAmount} ₴ зменшує прогрес — лишилось додати ще {stillNeed} ₴.</>
                  )}
                </p>
              );
            })()}
          </div>
        </details>

        <form
          onSubmit={handleSubmit}
          className="space-y-6"
          onKeyDown={(e) => {
            // Prevent implicit form submit / first-button-trigger when user presses Enter
            // inside a text input. This was causing the "Enter on city → adds upsell to cart" bug
            // (first non-explicit-type Button inside the form was getting clicked).
            // Allow Enter inside <textarea> (multiline) and on the submit button itself.
            const target = e.target as HTMLElement;
            if (
              e.key === "Enter" &&
              target.tagName === "INPUT" &&
              (target as HTMLInputElement).type !== "submit" &&
              (target as HTMLInputElement).type !== "button"
            ) {
              e.preventDefault();
            }
          }}
        >
          {/* Contact info */}
          <div className="space-y-4">
            <h2 className="font-semibold">{t("checkout.contacts_title")}</h2>
            {!user && (
              <p className="text-xs text-muted-foreground">
                {t("checkout.guest_hint", { defaultValue: lang === "en" ? "Checkout as guest — no account needed. Optionally " : "Оформлюйте як гість — без реєстрації. За бажанням " })}
                <Link to={lp("/customer-login")} className="text-primary hover:underline">{t("checkout.auth_hint_login")}</Link>
                {t("checkout.guest_hint_tail", { defaultValue: lang === "en" ? " to track your order." : ", щоб відстежувати замовлення." })}
              </p>
            )}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label htmlFor="checkout-name">{t("checkout.name")} *</Label>
                <Input
                  id="checkout-name"
                  value={form.customer_name}
                  onChange={(e) => setForm({ ...form, customer_name: e.target.value })}
                  autoComplete="given-name"
                  inputMode="text"
                  required
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="checkout-surname">{t("checkout.surname")} *</Label>
                <Input
                  id="checkout-surname"
                  value={form.customer_surname}
                  onChange={(e) => setForm({ ...form, customer_surname: e.target.value })}
                  autoComplete="family-name"
                  inputMode="text"
                  required
                />
              </div>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label>{t("checkout.phone")} *</Label>
                <PhoneInputUA
                  value={form.customer_phone}
                  onChange={(next) => setForm({ ...form, customer_phone: next })}
                  required
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="checkout-email">{t("checkout.email")}</Label>
                <Input
                  id="checkout-email"
                  type="email"
                  value={form.customer_email}
                  onChange={(e) => setForm({ ...form, customer_email: e.target.value })}
                  autoComplete="email"
                  inputMode="email"
                />
              </div>
            </div>

            <div className="space-y-2">
              <Label>{t("checkout.delivery_type_label")} *</Label>
              <div className="grid grid-cols-2 gap-2">
                {[
                  { value: "branch", label: t("checkout.delivery_branch"), price: DELIVERY_BASE_BRANCH },
                  { value: "parcel_locker", label: t("checkout.delivery_parcel_locker"), price: DELIVERY_BASE_PARCEL_LOCKER },
                ].map((opt) => (
                  <label
                    key={opt.value}
                    className={`flex flex-col gap-0.5 p-3 rounded-lg border cursor-pointer transition-colors ${
                      form.delivery_type === opt.value ? "border-primary bg-primary/5" : "border-border"
                    }`}
                  >
                    <input
                      type="radio"
                      name="delivery_type"
                      value={opt.value}
                      checked={form.delivery_type === opt.value}
                      onChange={() => setForm((p) => ({ ...p, delivery_type: opt.value as "branch" | "parcel_locker" }))}
                      className="sr-only"
                    />
                    <span className="text-sm font-medium">{opt.label}</span>
                    <span className="text-xs text-muted-foreground">{opt.price} ₴ {t("checkout.np_fee_short", { defaultValue: lang === "en" ? "+ 1% NP fee" : "+ 1% НП" })}</span>
                  </label>
                ))}
              </div>
            </div>

            <NovaPoshtaPicker
              city={form.city}
              warehouse={form.warehouse}
              deliveryType={form.delivery_type}
              onCityChange={(next) => setForm((prev) => ({ ...prev, city: next }))}
              onWarehouseChange={(next) => setForm((prev) => ({ ...prev, warehouse: next }))}
              required
            />
            <p className="text-xs text-muted-foreground">
              {t("checkout.delivery_np_note")}
            </p>
          </div>

          <PreferredDeliveryDatePicker
            value={form.preferred_delivery_date}
            onChange={(next) => setForm((prev) => ({ ...prev, preferred_delivery_date: next }))}
          />

          {/* Payment method */}
          <div className="space-y-3">
            <h2 className="font-semibold">{t("checkout.payment_title")}</h2>
            {enabledPayments.map(([key, pm]) => (
              <label key={key} className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${
                form.payment_method === key ? "border-primary bg-primary/5" : "border-border"
              }`}>
                <input
                  type="radio"
                  name="payment"
                  value={key}
                  checked={form.payment_method === key}
                  onChange={() => setForm({ ...form, payment_method: key })}
                  className="mt-1 accent-[hsl(var(--primary))]"
                />
                <div>
                  <div className="text-sm font-medium">{(pm as any).label}</div>
                  {(pm as any).details && form.payment_method === key && (
                    <div className="text-xs text-muted-foreground mt-1">{(pm as any).details}</div>
                  )}
                </div>
              </label>
            ))}
          </div>

          {/* Promo code */}
          <div className="space-y-2">
            <h2 className="font-semibold">{t("checkout.promo_title")}</h2>
            <div className="flex gap-2">
              <div className="relative flex-1">
                <Tag className="absolute left-3 top-2.5 w-4 h-4 text-muted-foreground" />
                <Input
                  value={form.promo_code}
                  onChange={(e) => { setForm({ ...form, promo_code: e.target.value.toUpperCase() }); setPromoError(""); }}
                  onKeyDown={(e) => {
                    // Allow Enter inside promo input to apply the code instead
                    // of being swallowed by the form's global Enter-prevent guard.
                    if (e.key === "Enter") {
                      e.preventDefault();
                      e.stopPropagation();
                      applyPromo();
                    }
                  }}
                  placeholder={t("checkout.promo_placeholder")}
                  className="pl-9"
                />
              </div>
              <Button type="button" variant="outline" onClick={applyPromo} disabled={promoLoading}>{promoLoading ? "..." : t("checkout.promo_apply")}</Button>
            </div>
            {promoError && <p className="text-xs text-destructive">{promoError}</p>}
            {promoApplied && <p className="text-xs text-success">{["percent", "percentage"].includes(promoApplied.discount_type) ? t("checkout.promo_discount_pct", { value: promoApplied.discount_value }) : t("checkout.promo_discount_amount", { value: promoApplied.discount_value })}</p>}
            {hasActivePlan && !promoApplied && (
              <button
                type="button"
                onClick={() => { setForm({ ...form, promo_code: "PLAN5" }); setTimeout(applyPromo, 0); }}
                className="text-xs text-primary hover:underline text-left"
              >
                💡 У вас активний план постачання — натисніть, щоб застосувати <span className="font-bold">PLAN5</span> (−5%)
              </button>
            )}
          </div>

          {/* Gift order toggle — Butternut "Send as a gift" pattern.
              When ON: order.message is prefixed with 🎁 marker so the
              packing team omits the price tag and promo inserts. */}
          <div className="rounded-lg border border-border bg-card/40 p-3 space-y-2">
            <label className="flex items-start gap-2.5 cursor-pointer">
              <Checkbox
                checked={form.is_gift}
                onCheckedChange={(v) => setForm({ ...form, is_gift: !!v })}
                className="mt-0.5"
              />
              <span className="flex-1">
                <span className="flex items-center gap-1.5 font-medium text-sm">
                  <Gift className="w-4 h-4 text-primary" /> Це подарунок
                </span>
                <span className="block text-xs text-muted-foreground mt-0.5">
                  Запакуємо без цінника та рекламних вкладишів. За бажанням — додамо вашу записку.
                </span>
              </span>
            </label>
            {form.is_gift && (
              <Textarea
                value={form.gift_message}
                onChange={(e) => setForm({ ...form, gift_message: e.target.value.slice(0, 240) })}
                placeholder="Записка одержувачу (до 240 символів)"
                rows={2}
                className="text-sm"
              />
            )}
          </div>

          {/* Comment */}
          <div className="space-y-1">
            <Label>{t("checkout.comment_label")}</Label>
            <Textarea value={form.message} onChange={(e) => setForm({ ...form, message: e.target.value })} />
          </div>

          {/* AI-driven smart upsell — co-purchase based */}
          <SmartCheckoutUpsell />

          {/* Upsell */}
          {upsellFiltered.length > 0 && (
            <div className="space-y-3">
              <h2 className="font-semibold">{t("checkout.upsell_title")}</h2>
              <div className="flex gap-3 overflow-x-auto pb-2 -mx-4 px-4">
                {upsellFiltered.map((p: any) => (
                  <div key={p.id} className="flex-shrink-0 w-40 bg-card border border-border rounded-lg overflow-hidden">
                    <img src={getProductImage(p)} alt={p.name} loading="lazy" decoding="async" className="w-full h-24 object-cover" />
                    <div className="p-2">
                      <div className="text-xs font-medium line-clamp-1">{p.name}</div>
                      <div className="flex items-center justify-between mt-1">
                        <span className="text-sm font-bold text-primary">{p.price} ₴</span>
                        <Button
                          type="button"
                          size="icon"
                          className="h-7 w-7"
                          onClick={() => {
                            addItem({ id: p.id, name: p.name, price: p.price, wholesale_price: p.wholesale_price ?? null, weight: p.weight, weight_grams: parseWeightToGrams(p.weight), image_url: getProductImage(p) });
                            showAddedToCartToast(p.name);
                          }}
                        >
                          <Plus className="w-4 h-4" />
                        </Button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Trust badges — reduce last-mile checkout drop-off */}
          <CheckoutTrustBadges />

          {/* Consent */}
          <label id="checkout-consent" className="flex items-start gap-2 text-sm scroll-mt-24">
            <Checkbox checked={agreed} onCheckedChange={(v) => setAgreed(v === true)} className="mt-0.5" />
            <span className="text-muted-foreground">
              {t("checkout.consent_pre")}
              <Link to={lp("/privacy")} target="_blank" className="text-primary hover:underline">
                {t("checkout.consent_link")}
              </Link>
            </span>
          </label>

          <Button type="submit" id="checkout-submit" className="w-full bg-primary text-primary-foreground text-lg py-6" disabled={loading || !agreed}>
            {loading ? (
              <span className="inline-flex items-center gap-2">
                <span className="w-4 h-4 rounded-full border-2 border-primary-foreground border-t-transparent animate-spin" />
                {submitStatus || t("checkout.submitting")}
              </span>
            ) : (
              t("checkout.submit", { total: finalTotal })
            )}
          </Button>
          {loading && submitStatus && (
            <p className="text-xs text-center text-muted-foreground -mt-2" aria-live="polite">
              {submitStatus}
            </p>
          )}
        </form>
      </div>

      {/* Sticky mobile mini-summary — submits in 1 tap when valid, otherwise
          scrolls to & focuses the first invalid required field. */}
      <div className="md:hidden fixed bottom-0 left-0 right-0 z-40 bg-card/95 backdrop-blur border-t border-border px-4 py-2.5 shadow-lg">
        <div className="flex items-center justify-between gap-3 max-w-2xl mx-auto">
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground leading-none">
              {t("checkout.to_pay")}
            </div>
            <div className="text-lg font-bold text-primary leading-tight mt-0.5">{finalTotal} ₴</div>
          </div>
          <Button
            type="button"
            size="sm"
            className="h-11 px-5 bg-primary text-primary-foreground font-semibold flex-shrink-0"
            disabled={loading}
            onClick={() => {
              const submitBtn = document.getElementById("checkout-submit") as HTMLButtonElement | null;
              if (!submitBtn) return;
              const formEl = submitBtn.closest("form") as HTMLFormElement | null;
              if (!formEl) {
                submitBtn.click();
                return;
              }
              const invalid = formEl.querySelector<HTMLInputElement | HTMLTextAreaElement>(
                "input:invalid, textarea:invalid"
              );
              if (invalid) {
                invalid.scrollIntoView({ behavior: "smooth", block: "center" });
                setTimeout(() => invalid.focus({ preventScroll: true }), 300);
                return;
              }
              if (!agreed) {
                toast({ title: "Підтвердіть згоду на обробку даних", variant: "destructive" });
                const consent = document.getElementById("checkout-consent");
                consent?.scrollIntoView({ behavior: "smooth", block: "center" });
                return;
              }
              submitBtn.click();
            }}
          >
            {loading ? (
              <span className="inline-flex items-center gap-1.5">
                <span className="w-3.5 h-3.5 rounded-full border-2 border-primary-foreground border-t-transparent animate-spin" />
                {submitStatus?.includes("повторюємо") ? "Повтор…" : t("checkout.submitting")}
              </span>
            ) : (
              t("checkout.submit", { total: finalTotal })
            )}
          </Button>
        </div>
      </div>
    </div>
  );
};

export default Checkout;
