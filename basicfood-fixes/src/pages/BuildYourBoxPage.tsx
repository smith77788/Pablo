import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { supabase } from "@/integrations/supabase/client";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import OptimizedImage from "@/components/OptimizedImage";
import PageSeo from "@/components/PageSeo";
import { ShoppingCart, Plus, Minus, PackageCheck, Loader2, Sparkles } from "lucide-react";
import { useCart } from "@/contexts/CartContext";
import { useToast } from "@/hooks/use-toast";
import { useAuth } from "@/contexts/AuthContext";
import { usePrimaryPet } from "@/hooks/usePrimaryPet";

/**
 * Build-Your-Box: Butternut-style multi-product picker tuned for our offal range.
 *
 * Picks are gated by the user's primary pet profile when present:
 *   - cat → only cat-tagged products
 *   - dog → only dog-tagged products
 *   - puppy/sensitive → soft offal preferred (visual sort)
 *
 * Bundle math is illustrative on this page — the AUTHORITATIVE discount
 * is computed server-side by `create_order_with_items` on checkout.
 * (Smart Bundles / promo logic remain untouched.)
 */
type Product = {
  id: string;
  name: string;
  description: string | null;
  price: number;
  weight: string;
  image_url: string | null;
  categories: string[];
  min_age_months: number | null;
};

const SOFT_KEYWORDS = ["печінка", "легені", "легеня"];
const HARD_KEYWORDS = ["трахея", "аорта", "жила", "пеніс", "шия"];

const TIERS = [
  { count: 3, label: "Стартовий", offText: "−5% від суми", pct: 0.05 },
  { count: 5, label: "Сімейний", offText: "−10% від суми", pct: 0.10 },
  { count: 7, label: "Преміум", offText: "−15% від суми", pct: 0.15 },
] as const;

const BuildYourBoxPage = () => {
  const [products, setProducts] = useState<Product[]>([]);
  const [loading, setLoading] = useState(true);
  const [box, setBox] = useState<Record<string, number>>({});
  const { addItem } = useCart();
  const { toast } = useToast();
  const { user } = useAuth();
  const { data: pet } = usePrimaryPet();

  useEffect(() => {
    supabase
      .from("products")
      .select("id, name, description, price, weight, image_url, categories, min_age_months")
      .eq("is_active", true)
      .order("price", { ascending: true })
      .then(({ data }) => {
        setProducts((data ?? []) as Product[]);
      })
      .catch((err) => console.error("[build-box] products load failed:", err))
      .finally(() => setLoading(false));
  }, []);

  // Pet-aware filter + sort
  const visible = useMemo(() => {
    let list = products;
    if (pet?.species === "cat") list = list.filter((p) => p.categories?.includes("cats"));
    else if (pet?.species === "dog") list = list.filter((p) => p.categories?.includes("dogs"));

    // Sensitive / puppy → push soft offal up; chewers (large adult) → hard up.
    const isSoftPref =
      (pet?.age_months !== null && pet?.age_months !== undefined && pet.age_months < 6) ||
      (pet?.sensitivities && pet.sensitivities.length > 0);
    const isHardPref = pet?.weight_kg !== null && pet?.weight_kg !== undefined && pet.weight_kg >= 25;

    return [...list].sort((a, b) => {
      const aSoft = SOFT_KEYWORDS.some((k) => a.name.toLowerCase().includes(k));
      const bSoft = SOFT_KEYWORDS.some((k) => b.name.toLowerCase().includes(k));
      const aHard = HARD_KEYWORDS.some((k) => a.name.toLowerCase().includes(k));
      const bHard = HARD_KEYWORDS.some((k) => b.name.toLowerCase().includes(k));
      if (isSoftPref && aSoft !== bSoft) return aSoft ? -1 : 1;
      if (isHardPref && aHard !== bHard) return aHard ? -1 : 1;
      return 0;
    });
  }, [products, pet]);

  const totalCount = useMemo(
    () => Object.values(box).reduce((s, q) => s + q, 0),
    [box],
  );
  const subtotal = useMemo(
    () =>
      Object.entries(box).reduce((s, [pid, qty]) => {
        const p = products.find((x) => x.id === pid);
        return s + (p ? p.price * qty : 0);
      }, 0),
    [box, products],
  );

  const currentTier = useMemo(
    () => [...TIERS].reverse().find((t) => totalCount >= t.count) ?? null,
    [totalCount],
  );
  const nextTier = useMemo(() => TIERS.find((t) => totalCount < t.count) ?? null, [totalCount]);
  const estDiscount = currentTier ? Math.round(subtotal * currentTier.pct) : 0;
  const estTotal = subtotal - estDiscount;

  const inc = (id: string) => setBox((b) => ({ ...b, [id]: (b[id] ?? 0) + 1 }));
  const dec = (id: string) =>
    setBox((b) => {
      const next = { ...b };
      const cur = (b[id] ?? 0) - 1;
      if (cur <= 0) delete next[id];
      else next[id] = cur;
      return next;
    });

  const addBoxToCart = () => {
    if (totalCount === 0) {
      toast({ title: "Бокс порожній", description: "Додайте хоча б один товар" });
      return;
    }
    Object.entries(box).forEach(([pid, qty]) => {
      const p = products.find((x) => x.id === pid);
      if (!p) return;
      for (let i = 0; i < qty; i++) {
        addItem({
          id: p.id,
          name: p.name,
          price: p.price,
          image_url: p.image_url ?? null,
          weight: p.weight,
        });
      }
    });
    toast({
      title: "Бокс у кошику",
      description: `${totalCount} ${totalCount === 1 ? "товар" : totalCount < 5 ? "товари" : "товарів"}`,
    });
    setBox({});
  };

  return (
    <main className="min-h-screen bg-background">
      <PageSeo
        title="Збери свій бокс ласощів — BASIC.FOOD"
        description="Створіть персональний набір натуральних сушених ласощів для свого собаки чи кота. Чим більше товарів — тим вигідніше."
        keywords="бокс ласощів, набір сушених ласощів, конструктор ласощів, BASIC.FOOD"
        canonical="https://basic-food.shop/build-your-box"
      />

      <div className="container mx-auto max-w-5xl px-4 py-8">
        <header className="text-center mb-6">
          <h1 className="text-3xl md:text-4xl font-bold flex items-center justify-center gap-2">
            <PackageCheck className="h-7 w-7 text-primary" />
            Збери свій бокс
          </h1>
          <p className="text-muted-foreground mt-2 text-sm md:text-base">
            Оберіть улюблені ласощі — чим більший бокс, тим краща ціна.
          </p>
          {!user && (
            <p className="text-xs text-muted-foreground mt-1">
              <Link to="/quiz" className="text-primary underline">Пройдіть quiz</Link> — і ми відсортуємо товари під вашого улюбленця.
            </p>
          )}
          {pet && (
            <Badge variant="secondary" className="mt-2 gap-1">
              <Sparkles className="h-3 w-3" /> Підбірка під {pet.name}
            </Badge>
          )}
        </header>

        {/* Trust strip — risk reversal in line with checkout badges */}
        <ul className="flex flex-wrap justify-center gap-x-4 gap-y-1 text-[11px] md:text-xs text-muted-foreground mb-4">
          <li>✓ Натуральний склад</li>
          <li>✓ Контроль якості кожної партії</li>
          <li>✓ Безкоштовна доставка по Рівному від 500 ₴</li>
          <li>✓ Гарантія повернення 14 днів</li>
        </ul>

        {/* Tier progress */}
        <Card className="mb-4 border-primary/30">
          <CardContent className="p-4">
            <div className="flex items-center justify-between text-xs md:text-sm font-medium mb-2">
              <span>Зібрано: <span className="text-primary font-bold">{totalCount}</span></span>
              <span className="text-muted-foreground">
                {nextTier ? `+${nextTier.count - totalCount} до «${nextTier.label}» (${nextTier.offText})` : "Максимальний рівень досягнуто 🎉"}
              </span>
            </div>
            <div className="grid grid-cols-3 gap-1 h-2 rounded-full overflow-hidden bg-muted">
              {TIERS.map((t) => (
                <div
                  key={t.count}
                  className={totalCount >= t.count ? "bg-primary" : "bg-transparent"}
                />
              ))}
            </div>
            <div className="grid grid-cols-3 text-[10px] md:text-xs text-muted-foreground mt-1.5">
              {TIERS.map((t) => (
                <span key={t.count} className="text-center">
                  {t.count}+ · {t.offText}
                </span>
              ))}
            </div>
          </CardContent>
        </Card>

        {loading ? (
          <div className="flex justify-center py-16">
            <Loader2 className="h-6 w-6 animate-spin" />
          </div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3 md:gap-4">
            {visible.map((p) => {
              const qty = box[p.id] ?? 0;
              return (
                <Card key={p.id} className={qty > 0 ? "border-primary" : ""}>
                  <CardContent className="p-3 flex flex-col gap-2">
                    {p.image_url && (
                      <Link to={`/product/${p.id}`} className="block">
                        <OptimizedImage
                          src={p.image_url}
                          alt={p.name}
                          width={200}
                          height={140}
                          className="w-full aspect-[4/3] object-cover rounded-md"
                        />
                      </Link>
                    )}
                    <Link to={`/product/${p.id}`} className="font-semibold text-sm leading-tight hover:text-primary line-clamp-2">
                      {p.name}
                    </Link>
                    <div className="text-xs text-muted-foreground">{p.weight}</div>
                    <div className="flex items-center justify-between mt-auto pt-1">
                      <span className="font-bold text-primary">{p.price} ₴</span>
                      {qty === 0 ? (
                        <Button size="sm" variant="outline" className="h-8 px-2.5" onClick={() => inc(p.id)}>
                          <Plus className="h-3.5 w-3.5" />
                        </Button>
                      ) : (
                        <div className="flex items-center gap-1">
                          <Button size="icon" variant="outline" className="h-7 w-7" onClick={() => dec(p.id)} aria-label="Менше">
                            <Minus className="h-3 w-3" />
                          </Button>
                          <span className="text-sm font-bold w-5 text-center">{qty}</span>
                          <Button size="icon" variant="outline" className="h-7 w-7" onClick={() => inc(p.id)} aria-label="Більше">
                            <Plus className="h-3 w-3" />
                          </Button>
                        </div>
                      )}
                    </div>
                  </CardContent>
                </Card>
              );
            })}
          </div>
        )}

        {/* Sticky bottom summary */}
        {totalCount > 0 && (
          <div className="sticky bottom-3 mt-6 z-10">
            <Card className="border-primary shadow-lg bg-card/95 backdrop-blur">
              <CardContent className="p-3 md:p-4 flex items-center gap-3">
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-muted-foreground">
                    {totalCount} товар{totalCount === 1 ? "" : totalCount < 5 ? "и" : "ів"}
                    {currentTier ? ` · бонус «${currentTier.label}»` : ""}
                  </div>
                  <div className="font-bold">
                    <span className="text-primary text-lg">{estTotal} ₴</span>
                    {estDiscount > 0 && (
                      <span className="text-xs text-muted-foreground line-through ml-2">{subtotal} ₴</span>
                    )}
                  </div>
                  {estDiscount === 0 && nextTier && (
                    <div className="text-[10px] text-muted-foreground">
                      Точна знижка розраховується автоматично у кошику.
                    </div>
                  )}
                  {totalCount >= 3 && (
                    <div className="text-[10px] text-primary mt-0.5">
                      💡 Додатково −5% з промокодом <span className="font-mono font-bold">PLAN5</span>, якщо налаштуєте автоповтор
                    </div>
                  )}
                </div>
                <Button onClick={addBoxToCart} className="gap-1.5">
                  <ShoppingCart className="h-4 w-4" /> У кошик
                </Button>
              </CardContent>
            </Card>
          </div>
        )}

        <Card className="mt-6 bg-muted/30">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Як працює бокс</CardTitle>
            <CardDescription className="text-xs">
              Вкажіть бажану кількість кожного товару. Підсумкова ціна та точна знижка розраховуються
              автоматично у кошику відповідно до правил акцій BASIC.FOOD.
            </CardDescription>
          </CardHeader>
        </Card>
      </div>
    </main>
  );
};

export default BuildYourBoxPage;
