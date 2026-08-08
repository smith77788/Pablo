import { useEffect, useMemo, useState } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { supabase } from "@/integrations/supabase/client";
import { Link, Navigate, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ArrowLeft, LogOut, Package, XCircle, Heart, MapPin, Key, Plus, Trash2, Send, Building2, Pencil, User as UserIcon, Gift, PawPrint, Repeat, Pause, Play } from "lucide-react";
import PetProfileForm, { PetProfileFormValues, emptyPetForm } from "@/components/PetProfileForm";
import PetProfileCard from "@/components/PetProfileCard";
import { useDeleteAccount } from "@/hooks/useDeleteAccount";
import { usePriceMode } from "@/contexts/PriceModeContext";
import { useToast } from "@/hooks/use-toast";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Checkbox } from "@/components/ui/checkbox";
import { getProductImage } from "@/components/CatalogSection";
import TelegramLinkPanel from "@/components/TelegramLinkPanel";
import ReferralPanel from "@/components/ReferralPanel";
import ReorderButton from "@/components/ReorderButton";
import { WebPushToggle } from "@/components/WebPushPrompt";
import Seo from "@/components/Seo";
import LinkedAccountsPanel from "@/components/LinkedAccountsPanel";
import ReferralCard from "@/components/ReferralCard";
import PendingReviewRequests from "@/components/PendingReviewRequests";
import ReorderPlanItemsEditor from "@/components/ReorderPlanItemsEditor";
import RecommendedNextBox from "@/components/RecommendedNextBox";
import LoyaltyStatusCard from "@/components/LoyaltyStatusCard";
import OrderFeedbackInline from "@/components/OrderFeedbackInline";
import PetProductReactions from "@/components/PetProductReactions";
import PauseReorderPlanDialog from "@/components/PauseReorderPlanDialog";
import SendNextBoxNowButton from "@/components/SendNextBoxNowButton";
import PlanAddonsManager from "@/components/PlanAddonsManager";
import HolidayModeCard from "@/components/HolidayModeCard";
import UpcomingBoxesTimeline from "@/components/UpcomingBoxesTimeline";
import PlanDislikeWarning from "@/components/PlanDislikeWarning";
import NextBoxPreview from "@/components/NextBoxPreview";
import PetWeightLogCard from "@/components/PetWeightLogCard";
import TreatAllowanceCard from "@/components/TreatAllowanceCard";
import { getLangFromPath, withLangPrefix } from "@/i18n";

const statusLabels: Record<string, string> = {
  new: "Новий", processing: "В обробці", shipped: "Відправлено",
  delivered: "Доставлено", completed: "Виконано", cancelled: "Скасовано",
};
const statusColors: Record<string, string> = {
  new: "bg-blue-900/30 text-blue-400", processing: "bg-yellow-900/30 text-yellow-400",
  shipped: "bg-purple-900/30 text-purple-400", delivered: "bg-green-900/30 text-green-400",
  completed: "bg-green-900/30 text-green-400", cancelled: "bg-red-900/30 text-red-400",
};

const Profile = () => {
  const { user, isLoading, signOut } = useAuth();
  const { deleteAccount, isDeleting } = useDeleteAccount();
  const { isWholesaleCustomer } = usePriceMode();
  const { toast } = useToast();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const lang = getLangFromPath(pathname);
  const lp = (path: string) => withLangPrefix(path, lang);
  const requestedTab = searchParams.get("tab");
  const activeTab = useMemo(() => {
    const allowedTabs = new Set(["orders", "wishlist", "addresses", "pets", "plans", "telegram", "referrals", "settings"]);
    return requestedTab && allowedTabs.has(requestedTab) ? requestedTab : "orders";
  }, [requestedTab]);
  const [passwordForm, setPasswordForm] = useState({ current: "", new: "", confirm: "" });
  const [changingPassword, setChangingPassword] = useState(false);
  const [addressDialog, setAddressDialog] = useState(false);
  // editingId !== null → dialog is in EDIT mode (PATCH); null → CREATE mode (INSERT).
  // Lets users fix typos in saved addresses without deleting + re-adding.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [expandedPlanId, setExpandedPlanId] = useState<string | null>(null);
  // Deep-link feedback: 'missing' = plan id not found for this user;
  // 'paused' = plan exists but is on hold. Cleared once user dismisses or
  // navigates away. Validation runs after reorderPlans loads (see effect below).
  const [planLinkNotice, setPlanLinkNotice] = useState<null | "missing" | "paused">(null);
  const [pausePlanId, setPausePlanId] = useState<string | null>(null);
  const [addressForm, setAddressForm] = useState({ label: "Основна", city: "", address: "", phone: "", is_default: false });
  const [displayName, setDisplayName] = useState<string>(
    (user?.user_metadata as Record<string, unknown> | undefined)?.display_name as string ?? user?.email ?? ""
  );
  const [savingName, setSavingName] = useState(false);

  useEffect(() => {
    if (requestedTab && requestedTab !== activeTab) {
      setSearchParams(activeTab === "orders" ? {} : { tab: activeTab }, { replace: true });
    }
  }, [requestedTab, activeTab, setSearchParams]);

  // Orders
  const { data: orders = [] } = useQuery({
    queryKey: ["my-orders", user?.id],
    queryFn: async () => {
      const { data } = await supabase.from("orders").select("*").eq("user_id", user!.id).order("created_at", { ascending: false });
      return data || [];
    },
    enabled: !!user,
  });

  // Reorder logic now lives in useReorder() hook (used via <ReorderButton />)
  // which atomically: fetches items + fresh products → re-adds to cart
  // honouring wholesale_price → fires ACOS reorder_clicked → redirects.

  // Cancel
  const cancelMutation = useMutation({
    mutationFn: async (orderId: string) => {
      const { error } = await supabase.from("orders").update({ status: "cancelled" }).eq("id", orderId).eq("user_id", user!.id);
      if (error) throw error;
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["my-orders"] }); toast({ title: "Замовлення скасовано" }); },
  });

  // Wishlist
  const { data: wishlistItems = [] } = useQuery({
    queryKey: ["wishlist", user?.id],
    queryFn: async () => {
      const { data } = await supabase.from("wishlists").select("*, products(*)").eq("user_id", user!.id);
      return data || [];
    },
    enabled: !!user,
  });

  const removeWishlist = useMutation({
    mutationFn: async (id: string) => {
      await supabase.from("wishlists").delete().eq("id", id);
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["wishlist"] }); toast({ title: "Видалено з обраного" }); },
  });

  // Pets
  const [petDialog, setPetDialog] = useState(false);
  const [editingPet, setEditingPet] = useState<any | null>(null);
  const { data: pets = [] } = useQuery({
    queryKey: ["pet-profiles", user?.id],
    queryFn: async () => {
      const { data } = await supabase.from("pet_profiles").select("*").eq("user_id", user!.id).order("created_at", { ascending: true });
      return data || [];
    },
    enabled: !!user,
  });
  const savePet = useMutation({
    mutationFn: async (values: PetProfileFormValues) => {
      const payload = { ...values, user_id: user!.id, segments: values.segments as any };
      if (editingPet) {
        const { error } = await supabase.from("pet_profiles").update(payload).eq("id", editingPet.id).eq("user_id", user!.id);
        if (error) throw error;
      } else {
        const { error } = await supabase.from("pet_profiles").insert(payload);
        if (error) throw error;
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pet-profiles"] });
      setPetDialog(false);
      setEditingPet(null);
      toast({ title: editingPet ? "Профіль оновлено" : "Улюбленця додано" });
    },
    onError: (e: any) => toast({ title: "Помилка", description: e.message, variant: "destructive" }),
  });
  const deletePet = useMutation({
    mutationFn: async (id: string) => { await supabase.from("pet_profiles").delete().eq("id", id); },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["pet-profiles"] }); toast({ title: "Видалено" }); },
  });

  // Auto-import draft pet profile saved by DogAdvisor for anonymous users
  useEffect(() => {
    if (!user) return;
    try {
      const raw = localStorage.getItem("pet_profile_draft");
      if (!raw) return;
      const draft = JSON.parse(raw);
      if (!draft?.name) return;
      // 30-day TTL
      if (draft.savedAt && Date.now() - draft.savedAt > 30 * 86400_000) {
        localStorage.removeItem("pet_profile_draft");
        return;
      }
      if (window.confirm(`Імпортувати профіль улюбленця "${draft.name}" з quiz?`)) {
        supabase.from("pet_profiles").insert({
          user_id: user.id,
          name: draft.name,
          species: draft.species,
          age_months: draft.age_months,
          weight_kg: draft.weight_kg,
          activity: draft.activity,
          segments: draft.segments,
          sensitivities: draft.sensitivities ?? [],
          notes: draft.notes ?? "Імпортовано з quiz",
        }).then(({ error }) => {
          if (error) {
            toast({ title: "Помилка імпорту", description: error.message, variant: "destructive" as any });
            return;
          }
          localStorage.removeItem("pet_profile_draft");
          qc.invalidateQueries({ queryKey: ["pet-profiles"] });
          toast({ title: "Профіль імпортовано", description: draft.name });
        }).catch((err) => {
          console.error("[profile] pet import failed:", err);
        });
      } else {
        localStorage.removeItem("pet_profile_draft");
      }
    } catch { /* noop */ }
  }, [user?.id]);
  const { data: addresses = [] } = useQuery({
    queryKey: ["addresses", user?.id],
    queryFn: async () => {
      const { data } = await supabase.from("user_addresses").select("*").eq("user_id", user!.id).order("is_default", { ascending: false });
      return data || [];
    },
    enabled: !!user,
  });

  // Reorder plans
  const { data: reorderPlans = [] } = useQuery({
    queryKey: ["reorder-plans", user?.id],
    queryFn: async () => {
      const { data } = await supabase.from("reorder_plans").select("*").eq("user_id", user!.id).order("created_at", { ascending: false });
      return data || [];
    },
    enabled: !!user,
  });
  // Deep-link validation for ?plan=<id>: only expand if plan exists & is active,
  // otherwise surface a notice instead of silently showing an empty Plans tab.
  // Runs after reorderPlans resolves to avoid false negatives during loading.
  useEffect(() => {
    const planId = searchParams.get("plan");
    if (!planId || activeTab !== "plans" || !user) return;
    if (!reorderPlans) return;
    const found = (reorderPlans as any[]).find((p) => p.id === planId);
    if (!found) {
      setPlanLinkNotice("missing");
      setExpandedPlanId(null);
    } else if (!found.is_active) {
      setPlanLinkNotice("paused");
      setExpandedPlanId(planId);
    } else {
      setPlanLinkNotice(null);
      setExpandedPlanId(planId);
    }
  }, [searchParams, activeTab, reorderPlans, user]);
  // petNameById — for "Plan for X" labels and re-link dropdown. Reuses the
  // existing `pets` query above (avoid duplicate fetch).
  const petNameById = useMemo(() => {
    const m = new Map<string, string>();
    for (const p of pets as { id: string; name: string }[]) m.set(p.id, p.name);
    return m;
  }, [pets]);
  const togglePlan = useMutation({
    mutationFn: async (p: { id: string; active: boolean }) => {
      await supabase.from("reorder_plans").update({ is_active: !p.active }).eq("id", p.id);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["reorder-plans"] }),
  });
  const deletePlan = useMutation({
    mutationFn: async (id: string) => { await supabase.from("reorder_plans").delete().eq("id", id); },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["reorder-plans"] }); toast({ title: "План видалено" }); },
  });
  const updatePlanDate = useMutation({
    mutationFn: async (p: { id: string; next: string }) => {
      await supabase.from("reorder_plans").update({ next_reminder_at: p.next }).eq("id", p.id);
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["reorder-plans"] }); toast({ title: "Дату оновлено" }); },
  });
  const updatePlanCadence = useMutation({
    mutationFn: async (p: { id: string; cadence_days: number }) => {
      await supabase.from("reorder_plans").update({ cadence_days: p.cadence_days }).eq("id", p.id);
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["reorder-plans"] }); toast({ title: "Періодичність оновлено" }); },
  });
  const updatePlanPet = useMutation({
    mutationFn: async (p: { id: string; pet_profile_id: string | null }) => {
      await supabase.from("reorder_plans").update({ pet_profile_id: p.pet_profile_id }).eq("id", p.id);
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["reorder-plans"] }); toast({ title: "Прив'язку оновлено" }); },
  });
  const skipNext = (p: { id: string; next_reminder_at: string; cadence_days: number }) => {
    const d = new Date(p.next_reminder_at);
    d.setDate(d.getDate() + p.cadence_days);
    updatePlanDate.mutate({ id: p.id, next: d.toISOString() });
  };
  const saveAddress = useMutation({
    mutationFn: async () => {
      // If setting as default, unset other defaults first
      if (addressForm.is_default) {
        await supabase.from("user_addresses").update({ is_default: false }).eq("user_id", user!.id);
      }
      if (editingId) {
        const { error } = await supabase
          .from("user_addresses")
          .update(addressForm)
          .eq("id", editingId)
          .eq("user_id", user!.id);
        if (error) throw error;
      } else {
        const { error } = await supabase.from("user_addresses").insert({ ...addressForm, user_id: user!.id });
        if (error) throw error;
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["addresses"] });
      qc.invalidateQueries({ queryKey: ["default-address"] });
      setAddressDialog(false);
      setEditingId(null);
      setAddressForm({ label: "Основна", city: "", address: "", phone: "", is_default: false });
      toast({ title: editingId ? "Адресу оновлено" : "Адресу збережено" });
    },
  });

  const openEditDialog = (addr: { id: string; label: string; city: string; address: string; phone: string | null; is_default: boolean }) => {
    setEditingId(addr.id);
    setAddressForm({
      label: addr.label || "Основна",
      city: addr.city || "",
      address: addr.address || "",
      phone: addr.phone || "",
      is_default: !!addr.is_default,
    });
    setAddressDialog(true);
  };

  const openCreateDialog = () => {
    setEditingId(null);
    setAddressForm({ label: "Основна", city: "", address: "", phone: "", is_default: false });
    setAddressDialog(true);
  };

  const deleteAddress = useMutation({
    mutationFn: async (id: string) => { await supabase.from("user_addresses").delete().eq("id", id); },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["addresses"] }); toast({ title: "Адресу видалено" }); },
  });

  const handleSaveDisplayName = async () => {
    if (!displayName.trim()) return;
    setSavingName(true);
    // Update both the auth metadata (used by Header / OAuth display) and the
    // public profiles table (used by admin CRM / orders panel) in parallel —
    // they're independent stores so we keep them in sync ourselves.
    const [authRes, profRes] = await Promise.all([
      supabase.auth.updateUser({ data: { display_name: displayName.trim() } }),
      supabase.from("profiles").update({ display_name: displayName.trim() }).eq("user_id", user!.id),
    ]);
    setSavingName(false);
    if (authRes.error || profRes.error) {
      toast({ title: "Помилка", description: (authRes.error || profRes.error)?.message, variant: "destructive" });
    } else {
      toast({ title: "Імʼя оновлено" });
    }
  };

  // Change password
  const handleChangePassword = async () => {
    if (passwordForm.new !== passwordForm.confirm) {
      toast({ title: "Паролі не збігаються", variant: "destructive" }); return;
    }
    if (passwordForm.new.length < 6) {
      toast({ title: "Мінімум 6 символів", variant: "destructive" }); return;
    }
    setChangingPassword(true);
    const { error } = await supabase.auth.updateUser({ password: passwordForm.new });
    setChangingPassword(false);
    if (error) {
      toast({ title: "Помилка", description: error.message, variant: "destructive" });
    } else {
      toast({ title: "Пароль змінено!" });
      setPasswordForm({ current: "", new: "", confirm: "" });
    }
  };

  if (isLoading) return <div className="min-h-screen bg-background flex items-center justify-center"><p className="text-muted-foreground">Завантаження...</p></div>;
  if (!user) return <Navigate to={`${lp("/customer-login")}?next=${encodeURIComponent(lp("/profile"))}`} replace />;

  return (
    <div className="min-h-screen">
      <Seo title="Особистий кабінет — BASIC.FOOD" description="Ваш кабінет на BASIC.FOOD" canonicalPath="/profile" noindex />
      <div className="container mx-auto px-4 py-8 max-w-3xl">
        <div className="flex items-center justify-between mb-6 gap-2">
          <Link to="/" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
            <ArrowLeft size={16} /> На головну
          </Link>
          <div className="flex items-center gap-1">
            <Button variant="ghost" size="sm" onClick={signOut} className="text-muted-foreground">
              <LogOut size={16} className="mr-1" /> Вийти
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={isDeleting}
              onClick={() => void deleteAccount()}
              className="text-destructive hover:text-destructive"
            >
              <Trash2 size={16} className="mr-1" /> {isDeleting ? "Видалення…" : "Видалити акаунт"}
            </Button>
          </div>
        </div>

        <h1 className="text-2xl font-bold mb-1">Мій профіль</h1>
        <p className="text-muted-foreground text-sm mb-3">{user.email}</p>
        {isWholesaleCustomer && (
          <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-primary/15 text-primary border border-primary/30 text-xs font-semibold mb-6">
            <Building2 className="w-3.5 h-3.5" /> Оптовий клієнт — діють оптові ціни
          </div>
        )}

        <div className="mb-6"><ReferralCard /></div>
        <PendingReviewRequests />

        <div className="mb-6 grid grid-cols-2 sm:grid-cols-4 gap-2">
          <Button variant="outline" size="sm" className="justify-start gap-2" onClick={() => setSearchParams({ tab: "orders" })}>
            <Package className="w-4 h-4" /> Замовлення
          </Button>
          <Button variant="outline" size="sm" className="justify-start gap-2" onClick={() => setSearchParams({ tab: "wishlist" })}>
            <Heart className="w-4 h-4" /> Обране
          </Button>
          <Button variant="outline" size="sm" className="justify-start gap-2" onClick={() => setSearchParams({ tab: "addresses" })}>
            <MapPin className="w-4 h-4" /> Адреси
          </Button>
          <Button variant="outline" size="sm" className="justify-start gap-2" onClick={() => navigate(lp("/catalog"))}>
            <Plus className="w-4 h-4" /> Нове замовлення
          </Button>
        </div>

        <Tabs value={activeTab} onValueChange={(value) => setSearchParams(value === "orders" ? {} : { tab: value })}>
          <TabsList className="mb-6 flex-wrap h-auto">
            <TabsTrigger value="orders" className="gap-1"><Package className="w-4 h-4" /> Замовлення</TabsTrigger>
            <TabsTrigger value="wishlist" className="gap-1"><Heart className="w-4 h-4" /> Обране</TabsTrigger>
            <TabsTrigger value="addresses" className="gap-1"><MapPin className="w-4 h-4" /> Адреси</TabsTrigger>
            <TabsTrigger value="pets" className="gap-1"><PawPrint className="w-4 h-4" /> Улюбленці</TabsTrigger>
            <TabsTrigger value="plans" className="gap-1"><Repeat className="w-4 h-4" /> Плани</TabsTrigger>
            <TabsTrigger value="telegram" className="gap-1"><Send className="w-4 h-4" /> Telegram</TabsTrigger>
            <TabsTrigger value="referrals" className="gap-1"><Gift className="w-4 h-4" /> Запросити</TabsTrigger>
            <TabsTrigger value="settings" className="gap-1"><Key className="w-4 h-4" /> Безпека</TabsTrigger>
          </TabsList>

          {/* ORDERS */}
          <TabsContent value="orders">
            <LoyaltyStatusCard />
            <RecommendedNextBox />
            {orders.length === 0 ? (
              <p className="text-muted-foreground">У вас ще немає замовлень</p>
            ) : (
              <div className="space-y-3">
                {orders.map((order) => (
                  <div key={order.id} className="bg-card rounded-lg border border-border p-4">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-sm font-medium">#{order.id.slice(0, 8)}</span>
                      <span className={`text-xs px-2 py-0.5 rounded-full ${statusColors[order.status] || ""}`}>
                        {statusLabels[order.status] || order.status}
                      </span>
                    </div>
                    <div className="flex justify-between items-center text-sm">
                      <span className="text-muted-foreground">
                        {new Date(order.created_at).toLocaleDateString("uk-UA")}
                      </span>
                      <div className="flex items-center gap-2">
                        <span className="font-bold text-primary">{order.total} ₴</span>
                        <ReorderButton
                          orderId={order.id}
                          source="profile"
                          variant="ghost"
                          size="sm"
                          label="Повторити"
                          className="h-7 text-xs"
                        />
                        {(order.status === "new" || order.status === "processing") && (
                          <Button variant="ghost" size="sm" className="h-7 text-xs text-destructive hover:text-destructive" disabled={cancelMutation.isPending} onClick={() => cancelMutation.mutate(order.id)}>
                            <XCircle className="w-3.5 h-3.5 mr-1" /> Скасувати
                          </Button>
                        )}
                      </div>
                    </div>
                    {(order.status === "delivered" || order.status === "completed") && user && (
                      <>
                        <OrderFeedbackInline orderId={order.id} userId={user.id} />
                        <PetProductReactions orderId={order.id} />
                      </>
                    )}
                  </div>
                ))}
              </div>
            )}
          </TabsContent>

          {/* WISHLIST */}
          <TabsContent value="wishlist">
            {wishlistItems.length === 0 ? (
              <p className="text-muted-foreground">Обране порожнє</p>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                {wishlistItems.map((wi: any) => {
                  const prod = wi.products;
                  if (!prod) return null;
                  return (
                    <div key={wi.id} className="bg-card rounded-lg border border-border overflow-hidden flex">
                      <Link to={`/product/${prod.id}`} className="w-24 h-24 flex-shrink-0">
                        <img src={getProductImage(prod)} alt={prod.name} className="w-full h-full object-cover" />
                      </Link>
                      <div className="p-3 flex-1 flex flex-col justify-between">
                        <Link to={`/product/${prod.id}`} className="text-sm font-medium hover:text-primary">{prod.name}</Link>
                        <div className="flex items-center justify-between mt-1">
                          <span className="text-primary font-bold text-sm">{prod.price} ₴</span>
                          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => removeWishlist.mutate(wi.id)}>
                            <Trash2 className="w-3.5 h-3.5 text-destructive" />
                          </Button>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </TabsContent>

          {/* ADDRESSES */}
          <TabsContent value="addresses">
            <div className="flex justify-between items-center mb-4">
              <h2 className="font-semibold">Мої адреси</h2>
              <Button size="sm" onClick={openCreateDialog} className="gap-1">
                <Plus className="w-4 h-4" /> Додати
              </Button>
            </div>
            {addresses.length === 0 ? (
              <p className="text-muted-foreground">Немає збережених адрес</p>
            ) : (
              <div className="space-y-3">
                {addresses.map((addr: any) => (
                  <div key={addr.id} className="bg-card rounded-lg border border-border p-4 flex justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-medium">{addr.label} {addr.is_default && <span className="text-xs text-primary">(за замовчуванням)</span>}</div>
                      <div className="text-xs text-muted-foreground mt-1 break-words">{addr.city}, {addr.address}</div>
                      {addr.phone && <div className="text-xs text-muted-foreground">{addr.phone}</div>}
                    </div>
                    <div className="flex flex-col sm:flex-row gap-1 shrink-0">
                      <Button variant="ghost" size="icon" onClick={() => openEditDialog(addr)} aria-label="Редагувати адресу">
                        <Pencil className="w-4 h-4 text-muted-foreground" />
                      </Button>
                      <Button variant="ghost" size="icon" onClick={() => deleteAddress.mutate(addr.id)} aria-label="Видалити адресу">
                        <Trash2 className="w-4 h-4 text-destructive" />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            <Dialog open={addressDialog} onOpenChange={setAddressDialog}>
              <DialogContent className="max-w-sm bg-card max-h-[90vh] overflow-y-auto">
                <DialogHeader><DialogTitle>{editingId ? "Редагувати адресу" : "Нова адреса"}</DialogTitle></DialogHeader>
                <div className="space-y-3">
                  <div className="space-y-1">
                    <Label>Назва</Label>
                    <Input value={addressForm.label} onChange={(e) => setAddressForm({ ...addressForm, label: e.target.value })} placeholder="Дім, Робота..." />
                  </div>
                  <div className="space-y-1">
                    <Label>Місто</Label>
                    <Input
                      value={addressForm.city}
                      onChange={(e) => setAddressForm((prev) => ({ ...prev, city: e.target.value }))}
                      placeholder="Наприклад, Київ"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label>Відділення / адреса</Label>
                    <Input
                      value={addressForm.address}
                      onChange={(e) => setAddressForm((prev) => ({ ...prev, address: e.target.value }))}
                      placeholder="Номер відділення або адреса"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label>Телефон</Label>
                    <Input value={addressForm.phone} onChange={(e) => setAddressForm({ ...addressForm, phone: e.target.value })} placeholder="+380..." />
                  </div>
                  <label className="flex items-center gap-2 text-sm cursor-pointer">
                    <Checkbox checked={addressForm.is_default} onCheckedChange={(v) => setAddressForm({ ...addressForm, is_default: v === true })} />
                    <span>Використовувати за замовчуванням</span>
                  </label>
                  <Button className="w-full" onClick={() => saveAddress.mutate()} disabled={!addressForm.city || !addressForm.address || saveAddress.isPending}>Зберегти</Button>
                </div>
              </DialogContent>
            </Dialog>
          </TabsContent>

          {/* PLANS */}
          <TabsContent value="plans">
            <h2 className="font-semibold mb-3">Плани постачання</h2>
            {planLinkNotice && (
              <div className="mb-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs flex items-start justify-between gap-2">
                <div>
                  <p className="font-medium">
                    {planLinkNotice === "missing"
                      ? "План не знайдено"
                      : "Цей план зараз на паузі"}
                  </p>
                  <p className="text-muted-foreground mt-0.5">
                    {planLinkNotice === "missing"
                      ? "Можливо, його було видалено або посилання застаріло. Оберіть план зі списку нижче або створіть новий."
                      : "Активуйте його кнопкою «Відновити», щоб додати позиції у наступний бокс."}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => {
                    setPlanLinkNotice(null);
                    const next = new URLSearchParams(searchParams);
                    next.delete("plan");
                    setSearchParams(next, { replace: true });
                  }}
                  className="text-muted-foreground hover:text-foreground flex-shrink-0"
                  aria-label="Закрити"
                >
                  <XCircle className="w-4 h-4" />
                </button>
              </div>
            )}
            <NextBoxPreview />
            <HolidayModeCard />
            <UpcomingBoxesTimeline />
            {reorderPlans.length === 0 ? (
              <p className="text-muted-foreground text-sm">Поки що немає планів. Створити план можна на сторінці підтвердження замовлення — отримуватимете нагадування коли час повторити.</p>
            ) : (
              <div className="space-y-3">
                {reorderPlans.map((p: any) => {
                  const itemsCount = Array.isArray(p.items) ? p.items.length : 0;
                  const next = new Date(p.next_reminder_at).toLocaleDateString("uk-UA");
                  const expanded = expandedPlanId === p.id;
                  return (
                    <div key={p.id} className="bg-card rounded-lg border border-border p-4">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2 flex-wrap">
                            <Repeat className="w-4 h-4 text-primary" />
                            <span className="font-medium text-sm">
                              {p.pet_profile_id && petNameById.get(p.pet_profile_id)
                                ? `План для ${petNameById.get(p.pet_profile_id)} · раз на ${p.cadence_days} днів`
                                : `Раз на ${p.cadence_days} днів`}
                            </span>
                            {p.pet_profile_id && petNameById.get(p.pet_profile_id) && (
                              <PawPrint className="w-3.5 h-3.5 text-primary" aria-hidden />
                            )}
                            {!p.is_active && <span className="text-[10px] uppercase px-1.5 py-0.5 rounded bg-muted text-muted-foreground">Пауза</span>}
                          </div>
                          <p className="text-xs text-muted-foreground mt-1">
                            {itemsCount} {itemsCount === 1 ? "позиція" : itemsCount < 5 ? "позиції" : "позицій"} · наступне нагадування {next}
                          </p>
                          {pets.length > 0 && (
                            <div className="mt-2">
                              <select
                                value={p.pet_profile_id ?? ""}
                                onChange={(e) =>
                                  updatePlanPet.mutate({
                                    id: p.id,
                                    pet_profile_id: e.target.value || null,
                                  })
                                }
                                className="h-7 text-[11px] px-2 rounded border border-border bg-background text-muted-foreground"
                                aria-label="Прив'язати до тварини"
                              >
                                <option value="">Без прив'язки</option>
                                {(pets as { id: string; name: string }[]).map((pp) => (
                                  <option key={pp.id} value={pp.id}>
                                    Для {pp.name}
                                  </option>
                                ))}
                              </select>
                            </div>
                          )}
                          <div className="flex flex-wrap gap-2 mt-3">
                            <Button variant="outline" size="sm" className="h-7 text-xs"
                              onClick={() => skipNext({ id: p.id, next_reminder_at: p.next_reminder_at, cadence_days: p.cadence_days })}>
                              Пропустити (+{p.cadence_days} дн)
                            </Button>
                            <input
                              type="date"
                              defaultValue={new Date(p.next_reminder_at).toISOString().slice(0, 10)}
                              min={new Date().toISOString().slice(0, 10)}
                              onChange={(e) => {
                                if (!e.target.value) return;
                                updatePlanDate.mutate({ id: p.id, next: new Date(e.target.value).toISOString() });
                              }}
                              className="h-7 text-xs px-2 rounded border border-border bg-background"
                              aria-label="Перенести дату"
                            />
                            <select
                              value={p.cadence_days}
                              onChange={(e) => updatePlanCadence.mutate({ id: p.id, cadence_days: Number(e.target.value) })}
                              className="h-7 text-xs px-2 rounded border border-border bg-background"
                              aria-label="Періодичність"
                            >
                              {[14, 21, 30, 45, 60].map((d) => (
                                <option key={d} value={d}>раз на {d} дн</option>
                              ))}
                            </select>
                            <Button variant="ghost" size="sm" className="h-7 text-xs"
                              onClick={() => setExpandedPlanId(expanded ? null : p.id)}>
                              {expanded ? "Приховати склад" : "Редагувати склад"}
                            </Button>
                            {p.is_active && Array.isArray(p.items) && p.items.length > 0 && (
                              <SendNextBoxNowButton
                                planId={p.id}
                                items={p.items as { product_id: string; quantity: number }[]}
                                addons={Array.isArray((p as any).addons) ? (p as any).addons : []}
                                cadenceDays={p.cadence_days}
                                onAdvanced={() => qc.invalidateQueries({ queryKey: ["reorder-plans"] })}
                              />
                            )}
                          </div>
                          {p.is_active && (
                            <PlanAddonsManager
                              planId={p.id}
                              planItems={Array.isArray(p.items) ? p.items as any : []}
                              addons={Array.isArray((p as any).addons) ? (p as any).addons : []}
                              onChanged={() => qc.invalidateQueries({ queryKey: ["reorder-plans"] })}
                            />
                          )}
                          {p.is_active && Array.isArray(p.items) && p.items.length > 0 && (
                            <PlanDislikeWarning
                              planId={p.id}
                              petProfileId={p.pet_profile_id ?? null}
                              items={p.items as { product_id: string; quantity: number }[]}
                              onEdit={() => setExpandedPlanId(p.id)}
                            />
                          )}
                          {expanded && (
                            <ReorderPlanItemsEditor
                              planId={p.id}
                              items={Array.isArray(p.items) ? p.items : []}
                              onChanged={() => qc.invalidateQueries({ queryKey: ["reorder-plans"] })}
                            />
                          )}
                        </div>
                        <div className="flex gap-1">
                          <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => {
                            if (p.is_active) setPausePlanId(p.id);
                            else togglePlan.mutate({ id: p.id, active: p.is_active });
                          }} aria-label={p.is_active ? "Призупинити" : "Активувати"}>
                            {p.is_active ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
                          </Button>
                          <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => deletePlan.mutate(p.id)} aria-label="Видалити">
                            <Trash2 className="w-3.5 h-3.5 text-destructive" />
                          </Button>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
            {pausePlanId && user && (
              <PauseReorderPlanDialog
                open={!!pausePlanId}
                onOpenChange={(v) => { if (!v) setPausePlanId(null); }}
                planId={pausePlanId}
                userId={user.id}
                onPaused={() => qc.invalidateQueries({ queryKey: ["reorder-plans"] })}
              />
            )}
          </TabsContent>

          {/* TELEGRAM + WEB PUSH */}
          {/* PETS */}
          <TabsContent value="pets">
            <div className="flex justify-between items-center mb-4">
              <h2 className="font-semibold">Мої улюбленці</h2>
              <Button size="sm" onClick={() => { setEditingPet(null); setPetDialog(true); }} className="gap-1">
                <Plus className="w-4 h-4" /> Додати
              </Button>
            </div>
            {pets.length === 0 ? (
              <p className="text-muted-foreground text-sm">Розкажіть про вашого собаку чи кота — і ми підберемо найкращі ласощі саме для нього.</p>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {pets.map((p: any) => (
                  <div key={p.id} className="flex flex-col">
                    <PetProfileCard
                      pet={p}
                      onEdit={() => { setEditingPet(p); setPetDialog(true); }}
                      onDelete={() => deletePet.mutate(p.id)}
                    />
                    <PetWeightLogCard petId={p.id} petName={p.name} />
                    <TreatAllowanceCard
                      petId={p.id}
                      petName={p.name}
                      weightKg={p.weight_kg}
                      activity={p.activity}
                    />
                  </div>
                ))}
              </div>
            )}
            <Dialog open={petDialog} onOpenChange={(o) => { setPetDialog(o); if (!o) setEditingPet(null); }}>
              <DialogContent className="max-w-md bg-card max-h-[90vh] overflow-y-auto">
                <DialogHeader><DialogTitle>{editingPet ? "Редагувати улюбленця" : "Новий улюбленець"}</DialogTitle></DialogHeader>
                <PetProfileForm
                  initial={editingPet ? {
                    name: editingPet.name,
                    species: editingPet.species,
                    breed: editingPet.breed ?? "",
                    age_months: editingPet.age_months,
                    weight_kg: editingPet.weight_kg ? Number(editingPet.weight_kg) : null,
                    activity: editingPet.activity,
                    sensitivities: editingPet.sensitivities ?? [],
                    segments: editingPet.segments ?? [],
                    notes: editingPet.notes ?? "",
                  } : undefined}
                  submitting={savePet.isPending}
                  onSubmit={(v) => savePet.mutate(v)}
                  onCancel={() => { setPetDialog(false); setEditingPet(null); }}
                />
              </DialogContent>
            </Dialog>
          </TabsContent>

          <TabsContent value="telegram">
            <h2 className="font-semibold mb-4">Сповіщення</h2>
            <div className="space-y-4 max-w-md">
              <WebPushToggle />
              <TelegramLinkPanel />
            </div>
          </TabsContent>

          {/* REFERRALS */}
          <TabsContent value="referrals">
            <h2 className="font-semibold mb-4">Запросити друзів</h2>
            <ReferralPanel />
          </TabsContent>

          {/* SECURITY */}
          <TabsContent value="settings">
            <div className="max-w-sm space-y-6">
              <div>
                <h2 className="font-semibold mb-3 flex items-center gap-2">
                  <UserIcon className="w-4 h-4" /> Імʼя
                </h2>
                <div className="flex gap-2">
                  <Input
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    placeholder="Ваше імʼя"
                  />
                  <Button onClick={handleSaveDisplayName} disabled={savingName || !displayName.trim()}>
                    {savingName ? "..." : "Зберегти"}
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground mt-1">
                  Відображається у вітальних листах та коментарях.
                </p>
              </div>

              <LinkedAccountsPanel />

              <div>
                <h2 className="font-semibold mb-3 flex items-center gap-2">
                  <Key className="w-4 h-4" /> Змінити пароль
                </h2>
                <div className="space-y-3">
                  <div className="space-y-1">
                    <Label>Новий пароль</Label>
                    <Input type="password" value={passwordForm.new} onChange={(e) => setPasswordForm({ ...passwordForm, new: e.target.value })} minLength={6} />
                  </div>
                  <div className="space-y-1">
                    <Label>Підтвердити пароль</Label>
                    <Input type="password" value={passwordForm.confirm} onChange={(e) => setPasswordForm({ ...passwordForm, confirm: e.target.value })} />
                  </div>
                  <Button onClick={handleChangePassword} disabled={changingPassword}>
                    {changingPassword ? "Збереження..." : "Змінити пароль"}
                  </Button>
                </div>
              </div>
            </div>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
};

export default Profile;
