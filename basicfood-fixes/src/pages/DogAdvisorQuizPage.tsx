import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { supabase } from "@/integrations/supabase/client";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import OptimizedImage from "@/components/OptimizedImage";
import PageSeo from "@/components/PageSeo";
import { ArrowLeft, ArrowRight, RotateCcw, Sparkles, Loader2, ShoppingCart, PawPrint, Gift } from "lucide-react";
import { useCart } from "@/contexts/CartContext";
import { useToast } from "@/hooks/use-toast";
import { useAuth } from "@/contexts/AuthContext";
import { Input } from "@/components/ui/input";
import FrequentlyBoughtTogether from "@/components/FrequentlyBoughtTogether";
import QuizEmailCapture from "@/components/QuizEmailCapture";
import {
  trackQuizStart,
  trackQuizStep,
  trackQuizCompleted,
  trackQuizBoxAdded,
  trackQuizPetSaved,
  trackQuizRestart,
} from "@/lib/quizAnalytics";

type Answers = {
  pet?: "dog" | "cat";
  age?: "puppy" | "young" | "adult";
  size?: "small" | "medium" | "large";
  goal?: "training" | "chew" | "snack";
  texture?: "soft" | "hard" | "mixed";
};

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

type Question = {
  key: keyof Answers;
  title: string;
  subtitle?: string;
  options: { value: string; label: string; emoji: string }[];
};

const Q_PET: Question = {
  key: "pet",
  title: "Кому шукаємо ласощі?",
  options: [
    { value: "dog", label: "Собаці", emoji: "🐶" },
    { value: "cat", label: "Коту", emoji: "🐱" },
  ],
};
const Q_AGE_DOG: Question = {
  key: "age",
  title: "Який вік собаки?",
  subtitle: "Це впливає на твердість і жувальне навантаження",
  options: [
    { value: "puppy", label: "До 6 міс", emoji: "🍼" },
    { value: "young", label: "6–18 міс", emoji: "🐕" },
    { value: "adult", label: "Дорослий", emoji: "🦴" },
  ],
};
const Q_AGE_CAT: Question = {
  key: "age",
  title: "Який вік кота?",
  options: [
    { value: "puppy", label: "Кошеня (до 6 міс)", emoji: "🍼" },
    { value: "young", label: "Молодий (6–18 міс)", emoji: "🐈" },
    { value: "adult", label: "Дорослий", emoji: "🐱" },
  ],
};
const Q_SIZE: Question = {
  key: "size",
  title: "Розмір собаки?",
  options: [
    { value: "small", label: "Маленький (до 10 кг)", emoji: "🐾" },
    { value: "medium", label: "Середній (10–25 кг)", emoji: "🐕" },
    { value: "large", label: "Великий (25+ кг)", emoji: "🐺" },
  ],
};
const Q_GOAL_DOG: Question = {
  key: "goal",
  title: "Для чого ласощі?",
  options: [
    { value: "training", label: "Дресура / навчання", emoji: "🎾" },
    { value: "chew", label: "Довге жування", emoji: "🦷" },
    { value: "snack", label: "Просто смаколик", emoji: "❤️" },
  ],
};
const Q_GOAL_CAT: Question = {
  key: "goal",
  title: "Для чого ласощі?",
  options: [
    { value: "training", label: "Привчання / нагорода", emoji: "🎾" },
    { value: "snack", label: "Смаколик до раціону", emoji: "❤️" },
  ],
};
const Q_TEXTURE_DOG: Question = {
  key: "texture",
  title: "Яка текстура подобається?",
  options: [
    { value: "soft", label: "М'які / середні", emoji: "🥩" },
    { value: "hard", label: "Тверді / для жування", emoji: "🦴" },
    { value: "mixed", label: "Не знаю — здивуй", emoji: "🎁" },
  ],
};

// Динамічний список питань залежно від виду улюбленця.
// Для котів пропускаємо «Розмір» (нерелевантно) і «Текстура» (тверді жувальні
// для собак котам не підходять) — лишаємо pet → age → goal.
const buildQuestions = (pet?: Answers["pet"]): Question[] => {
  if (pet === "cat") return [Q_PET, Q_AGE_CAT, Q_GOAL_CAT];
  if (pet === "dog") return [Q_PET, Q_AGE_DOG, Q_SIZE, Q_GOAL_DOG, Q_TEXTURE_DOG];
  return [Q_PET];
};

// Категоризація товарів за «характеристиками» — на базі знань про субпродукти.
// soft: печінка, легені; medium: вим'я, серце, рубець; hard: трахея, аорта, жила, пеніс, шия куряча
const TEXTURE_HARD = new Set(["трахея", "аорта", "жила", "пеніс", "шия"]);
const TEXTURE_SOFT = new Set(["печінка", "легені", "легеня"]);

function scoreProduct(p: Product, a: Answers): number {
  let score = 0;
  const lname = p.name.toLowerCase();

  // 1. Pet match (must)
  if (a.pet === "cat") {
    if (!p.categories.includes("cats")) return -999;
    score += 10;
  } else {
    if (!p.categories.includes("dogs")) return -999;
    score += 10;
  }

  // 2. Age (min_age_months)
  const ageMin = p.min_age_months ?? 0;
  if (a.age === "puppy" && ageMin > 4) score -= 8;
  if (a.age === "young" && ageMin > 6) score -= 3;
  // adult — будь-який підходить

  // 3. Goal
  if (a.goal === "training") {
    if (p.categories.includes("training")) score += 8;
    else score -= 2;
  }
  if (a.goal === "chew") {
    if ([...TEXTURE_HARD].some((k) => lname.includes(k))) score += 8;
  }
  if (a.goal === "snack") {
    if ([...TEXTURE_SOFT].some((k) => lname.includes(k))) score += 4;
    if (lname.includes("набір") || lname.includes("дегустац")) score += 6;
  }

  // 4. Size — для малих собак тверді жувальні менш безпечні
  if (a.size === "small") {
    if ([...TEXTURE_HARD].some((k) => lname.includes(k))) score -= 3;
    if ([...TEXTURE_SOFT].some((k) => lname.includes(k))) score += 3;
  }
  if (a.size === "large") {
    if ([...TEXTURE_HARD].some((k) => lname.includes(k))) score += 4;
  }

  // 5. Texture preference
  if (a.texture === "hard" && [...TEXTURE_HARD].some((k) => lname.includes(k))) score += 6;
  if (a.texture === "soft" && [...TEXTURE_SOFT].some((k) => lname.includes(k))) score += 6;
  if (a.texture === "mixed" && (lname.includes("набір") || lname.includes("дегустац"))) score += 10;

  return score;
}

const DogAdvisorQuizPage = () => {
  const [step, setStep] = useState(0);
  const [answers, setAnswers] = useState<Answers>({});
  const [products, setProducts] = useState<Product[]>([]);
  const [loading, setLoading] = useState(true);
  const { addItem } = useCart();
  const { toast } = useToast();
  const { user } = useAuth();
  const [petName, setPetName] = useState("");
  const [savingPet, setSavingPet] = useState(false);
  const [petSaved, setPetSaved] = useState(false);

  const ageMonths = (a?: Answers["age"]): number | null => a === "puppy" ? 4 : a === "young" ? 12 : a === "adult" ? 36 : null;
  const weightKg = (s?: Answers["size"]): number | null => s === "small" ? 7 : s === "medium" ? 18 : s === "large" ? 32 : null;
  const segmentsFromAnswers = (a: Answers): string[] => {
    const out: string[] = [];
    if (a.pet === "cat") out.push("cats"); else out.push("dogs");
    if (a.goal === "training") out.push("training");
    if (a.age === "puppy") out.push("sensitive");
    if (a.goal === "chew") out.push("dental");
    return Array.from(new Set(out));
  };

  const savePetProfile = async () => {
    const name = petName.trim();
    if (!name) { toast({ title: "Вкажіть ім'я улюбленця" }); return; }
    if (!user) {
      try {
        localStorage.setItem("pet_profile_draft", JSON.stringify({
          name, species: answers.pet ?? "dog",
          age_months: ageMonths(answers.age), weight_kg: weightKg(answers.size),
          activity: "medium", segments: segmentsFromAnswers(answers), sensitivities: [], notes: "",
          savedAt: Date.now(),
        }));
      } catch { /* noop */ }
      trackQuizPetSaved("guest_local");
      toast({ title: "Збережено локально", description: "Зареєструйтеся, щоб імпортувати профіль" });
      return;
    }
    setSavingPet(true);
    const { error } = await supabase.from("pet_profiles").insert({
      user_id: user.id,
      name,
      species: (answers.pet ?? "dog") as "dog" | "cat",
      age_months: ageMonths(answers.age),
      weight_kg: weightKg(answers.size),
      activity: "medium",
      segments: segmentsFromAnswers(answers) as any,
      sensitivities: [],
      notes: "Створено через quiz",
    });
    setSavingPet(false);
    if (error) { toast({ title: "Помилка", description: error.message, variant: "destructive" as any }); return; }
    setPetSaved(true);
    trackQuizPetSaved("auth");
    toast({ title: "Профіль збережено!", description: `${name} додано до ваших улюбленців` });
  };

  useEffect(() => {
    supabase
      .from("products")
      .select("id, name, description, price, weight, image_url, categories, min_age_months")
      .eq("is_active", true)
      .then(({ data }) => {
        setProducts((data ?? []) as Product[]);
      })
      .catch((err) => console.error("[quiz] products load failed:", err))
      .finally(() => setLoading(false));
  }, []);

  // Fire quiz_start once on mount.
  useEffect(() => {
    trackQuizStart();
  }, []);

  const QUESTIONS = useMemo(() => buildQuestions(answers.pet), [answers.pet]);
  const isResults = step >= QUESTIONS.length;
  const progress = Math.round((step / QUESTIONS.length) * 100);
  const currentQ = QUESTIONS[step];
  const currentValue = currentQ ? (answers[currentQ.key] as string | undefined) : undefined;

  const recommendations = useMemo(() => {
    if (!isResults) return [];
    const scored = products
      .map((p) => ({ product: p, score: scoreProduct(p, answers) }))
      .filter((x) => x.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 3);
    return scored;
  }, [isResults, products, answers]);

  // Fire quiz_completed exactly once when user reaches results screen with recos loaded.
  const [completedTracked, setCompletedTracked] = useState(false);
  useEffect(() => {
    if (isResults && !completedTracked && !loading) {
      setCompletedTracked(true);
      trackQuizCompleted(answers, recommendations.length);
    }
  }, [isResults, completedTracked, loading, answers, recommendations.length]);

  const handleSelect = (val: string) => {
    if (currentQ) trackQuizStep(step, currentQ.key, val);
    setAnswers((prev) => ({ ...prev, [currentQ.key]: val as never }));
    setTimeout(() => setStep((s) => s + 1), 200);
  };

  const restart = () => {
    trackQuizRestart();
    setStep(0);
    setAnswers({});
    setCompletedTracked(false);
    setPetSaved(false);
  };


  return (
    <main className="min-h-screen bg-background">
      <PageSeo
        title="Підбір ласощів для собаки — quiz BASIC.FOOD"
        description="5 питань — і ми порадимо натуральні ласощі з яловичих субпродуктів, які підійдуть саме вашому улюбленцю. Безкоштовно, без реєстрації."
        keywords="підбір ласощів, quiz собака, ласощі для собаки, натуральні ласощі"
        canonical="https://basic-food.shop/quiz"
      />
      <div className="container mx-auto max-w-2xl px-4 py-8">
        <header className="text-center mb-8">
          <h1 className="text-3xl md:text-4xl font-bold flex items-center justify-center gap-2">
            <Sparkles className="h-7 w-7 text-primary" />
            Підбір ласощів
          </h1>
          <p className="text-muted-foreground mt-2 text-sm md:text-base">
            Кілька питань — і ми порадимо ідеальні натуральні ласощі для вашого улюбленця
          </p>
        </header>

        {!isResults && (
          <>
            <Progress value={progress} className="mb-6" />
            <div className="text-xs text-muted-foreground text-center mb-4">
              Питання {step + 1} з {QUESTIONS.length}
            </div>
          </>
        )}

        {!isResults && currentQ && (
          <Card>
            <CardHeader>
              <CardTitle className="text-xl">{currentQ.title}</CardTitle>
              {currentQ.subtitle && <CardDescription>{currentQ.subtitle}</CardDescription>}
            </CardHeader>
            <CardContent className="space-y-3">
              {currentQ.options.map((opt) => {
                const selected = currentValue === opt.value;
                return (
                  <button
                    key={opt.value}
                    onClick={() => handleSelect(opt.value)}
                    className={`w-full text-left p-4 rounded-lg border-2 transition-all hover:border-primary hover:bg-primary/5 ${
                      selected ? "border-primary bg-primary/10" : "border-border"
                    }`}
                  >
                    <div className="flex items-center gap-3">
                      <span className="text-2xl">{opt.emoji}</span>
                      <span className="font-medium">{opt.label}</span>
                    </div>
                  </button>
                );
              })}
            </CardContent>
            {step > 0 && (
              <div className="px-6 pb-6">
                <Button variant="ghost" size="sm" onClick={() => setStep((s) => s - 1)}>
                  <ArrowLeft className="h-4 w-4 mr-1" /> Назад
                </Button>
              </div>
            )}
          </Card>
        )}

        {isResults && (
          <div className="space-y-4">
            <Card className="border-primary bg-gradient-to-br from-primary/15 via-primary/5 to-background">
              <CardContent className="p-4 flex items-start gap-3">
                <Gift className="h-6 w-6 text-primary shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0">
                  <div className="font-semibold text-sm">−15% на перше замовлення</div>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Промокод <span className="font-mono font-bold text-primary">WELCOME15</span> · мінімум 300 ₴ · автозастосування у кошику
                  </p>
                </div>
                <Button asChild size="sm" variant="outline" className="border-primary/40">
                  <Link to="/cart?promo=WELCOME15">Активувати</Link>
                </Button>
              </CardContent>
            </Card>

            {(() => {
              const subtotal = recommendations.reduce((s, r) => s + r.product.price, 0);
              if (subtotal === 0) return null;
              const eligible = subtotal >= 300;
              const discount = eligible ? Math.round(subtotal * 0.15) : 0;
              const total = subtotal - discount;
              const toFreeShip = Math.max(0, 500 - total);
              return (
                <Card className="border-primary/50">
                  <CardHeader className="pb-3">
                    <CardTitle className="flex items-center gap-2 text-base">
                      <Sparkles className="h-5 w-5 text-primary" />
                      Ваш стартовий бокс
                    </CardTitle>
                    <CardDescription>
                      {recommendations.length} {recommendations.length === 1 ? "товар" : recommendations.length < 5 ? "товари" : "товарів"} · персональна підбірка
                    </CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-1.5 text-sm">
                    <div className="flex justify-between"><span className="text-muted-foreground">Сума</span><span>{subtotal} ₴</span></div>
                    {eligible && (
                      <div className="flex justify-between text-primary"><span>Знижка WELCOME15</span><span>−{discount} ₴</span></div>
                    )}
                    <div className="flex justify-between font-bold text-base pt-1.5 border-t border-border">
                      <span>До сплати</span><span className="text-primary">{total} ₴</span>
                    </div>
                    {toFreeShip > 0 ? (
                      <p className="text-xs text-muted-foreground pt-1">+{toFreeShip} ₴ до безкоштовної доставки по Рівному</p>
                    ) : (
                      <p className="text-xs text-primary pt-1">✓ Безкоштовна доставка по Рівному</p>
                    )}
                    <Button
                      size="sm"
                      className="w-full mt-3"
                      onClick={() => {
                        recommendations.forEach(({ product }) => {
                          addItem({
                            id: product.id,
                            name: product.name,
                            price: product.price,
                            image_url: product.image_url ?? null,
                            weight: product.weight,
                          });
                        });
                        trackQuizBoxAdded(subtotal, recommendations.length);
                        toast({ title: "Бокс додано в кошик", description: `${recommendations.length} товарів` });
                      }}
                    >
                      <ShoppingCart className="h-4 w-4 mr-1.5" /> Додати весь бокс
                    </Button>
                  </CardContent>
                </Card>
              );
            })()}

            {loading ? (
              <div className="flex justify-center py-12"><Loader2 className="h-6 w-6 animate-spin" /></div>
            ) : recommendations.length === 0 ? (
              <Card><CardContent className="py-8 text-center text-muted-foreground">
                Не знайшли точних збігів — подивіться <Link to="/catalog" className="underline text-primary">весь каталог</Link>.
              </CardContent></Card>
            ) : (
              recommendations.map(({ product, score }, idx) => (
                <Card key={product.id} className={idx === 0 ? "border-primary" : ""}>
                  <CardContent className="p-4 flex gap-4">
                    {product.image_url && (
                      <Link to={`/product/${product.id}`} className="shrink-0">
                        <OptimizedImage
                          src={product.image_url}
                          alt={product.name}
                          width={96}
                          height={96}
                          className="w-24 h-24 rounded-lg object-cover"
                        />
                      </Link>
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-start gap-2 flex-wrap">
                        <Link to={`/product/${product.id}`} className="font-semibold hover:text-primary">
                          {product.name}
                        </Link>
                        {idx === 0 && <Badge className="bg-primary">Найкращий вибір</Badge>}
                      </div>
                      <div className="text-xs text-muted-foreground mt-1">{product.weight} · мінімум {product.min_age_months ?? 4} міс</div>
                      <div className="text-lg font-bold text-primary mt-2">{product.price} ₴</div>
                      <Button
                        size="sm"
                        className="mt-2"
                        onClick={() => {
                          addItem({
                            id: product.id,
                            name: product.name,
                            price: product.price,
                            image_url: product.image_url ?? null,
                            weight: product.weight,
                          });
                          toast({ title: "Додано в кошик", description: product.name });
                        }}
                      >
                        <ShoppingCart className="h-4 w-4 mr-1" /> В кошик
                      </Button>
                    </div>
                  </CardContent>
                </Card>
              ))
            )}

            {/* Cross-sell anchored on top recommendation — Butternut "complete the box" pattern.
                Real bundle pricing is computed server-side in create_order_with_items. */}
            {recommendations[0] && (
              <FrequentlyBoughtTogether
                currentProduct={{
                  id: recommendations[0].product.id,
                  name: recommendations[0].product.name,
                  price: recommendations[0].product.price,
                  weight: recommendations[0].product.weight,
                  image_url: recommendations[0].product.image_url,
                  categories: recommendations[0].product.categories,
                }}
              />
            )}

            {/* Email capture — для гостей, leads ідуть в quiz_leads → win-back */}
            {!user && recommendations.length > 0 && (
              <QuizEmailCapture
                petName={petName || undefined}
                species={(answers.pet ?? "dog") as "dog" | "cat"}
                ageBand={answers.age}
                sizeBand={answers.size}
                goal={answers.goal}
                texture={answers.texture}
                recommendedProductIds={recommendations.map((r) => r.product.id)}
              />
            )}

            {/* Save quiz answers as Pet Profile */}
            <Card className="border-primary/30 bg-primary/5">
              <CardContent className="p-4 space-y-3">
                <div className="flex items-center gap-2">
                  <PawPrint className="h-4 w-4 text-primary" />
                  <h3 className="font-semibold text-sm">
                    {petSaved ? "Профіль збережено" : "Зберегти як профіль улюбленця"}
                  </h3>
                </div>
                {!petSaved && (
                  <>
                    <p className="text-xs text-muted-foreground">
                      {user
                        ? "Збережемо параметри, щоб показувати персональні рекомендації по всьому сайту."
                        : "Зареєструйтеся, щоб ми пам'ятали параметри і підказували релевантні товари."}
                    </p>
                    <div className="flex gap-2">
                      <Input
                        placeholder="Ім'я улюбленця (напр. Барон)"
                        value={petName}
                        onChange={(e) => setPetName(e.target.value)}
                        maxLength={40}
                      />
                      <Button onClick={savePetProfile} disabled={savingPet || !petName.trim()}>
                        {savingPet ? <Loader2 className="h-4 w-4 animate-spin" /> : "Зберегти"}
                      </Button>
                    </div>
                    {!user && (
                      <Button asChild variant="outline" size="sm" className="w-full">
                        <Link to="/register">Зареєструватися →</Link>
                      </Button>
                    )}
                  </>
                )}
                {petSaved && (
                  <Button asChild size="sm" variant="outline">
                    <Link to="/profile?tab=pets">Перейти до улюбленців →</Link>
                  </Button>
                )}
              </CardContent>
            </Card>

            <div className="flex flex-wrap gap-2 pt-4">
              <Button variant="outline" onClick={restart}>
                <RotateCcw className="h-4 w-4 mr-1" /> Пройти ще раз
              </Button>
              <Button asChild variant="outline">
                <Link to="/build-your-box">Зібрати свій бокс</Link>
              </Button>
              <Button asChild>
                <Link to="/catalog">Весь каталог <ArrowRight className="h-4 w-4 ml-1" /></Link>
              </Button>
            </div>
          </div>
        )}
      </div>
    </main>
  );
};

export default DogAdvisorQuizPage;
