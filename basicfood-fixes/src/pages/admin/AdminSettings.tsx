import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/hooks/use-toast";
import { Save, Send, Upload, Palette } from "lucide-react";
import { AcosSystemTools } from "@/components/admin/AcosSystemTools";
import { PageHelp } from "@/components/admin/PageHelp";

const AdminSettings = () => {
  const { toast } = useToast();
  const { user } = useAuth();
  const qc = useQueryClient();

  const { data: settings, isLoading } = useQuery({
    queryKey: ["admin-settings"],
    queryFn: async () => {
      const { data, error } = await supabase.from("site_settings").select("*");
      if (error) throw error;
      const map: Record<string, any> = {};
      data.forEach((s: any) => { map[s.key] = s.value; });
      return map;
    },
  });

  const [paymentMethods, setPaymentMethods] = useState<any>({});
  const [contacts, setContacts] = useState<any>({});
  const [general, setGeneral] = useState<any>({});
  const [delivery, setDelivery] = useState<any>({});
  const [branding, setBranding] = useState<any>({});
  const [socialLinks, setSocialLinks] = useState<any>({});
  const [workingHours, setWorkingHours] = useState<any>({});
  const [gamePrizes, setGamePrizes] = useState<any[]>([]);
  const [homepageSections, setHomepageSections] = useState<any>({});
  const [invoice, setInvoice] = useState<any>({});

  const defaultSections = {
    best_sellers: true,
    shop_by_need: true,
    recently_viewed: true,
    bundle_offer: true,
    game_banner: true,
    quick_nav: true,
    telegram_cta: true,
    instagram_cta: true,
    why_us: true,
  };

  useEffect(() => {
    if (settings) {
      setPaymentMethods(settings.payment_methods || {});
      setContacts(settings.contacts || {});
      setGeneral(settings.general || {});
      setDelivery(settings.delivery_info || {});
      setBranding(settings.branding || {});
      setSocialLinks(settings.social_links || {});
      setWorkingHours(settings.working_hours || {});
      setHomepageSections({ ...defaultSections, ...(settings.homepage_sections || {}) });
      setInvoice(settings.invoice || {});
      setGamePrizes(settings.game_prizes || [
        { label: "Знижка 10%", type: "discount", value: "10", color: "hsl(40, 70%, 50%)", probability: 20 },
        { label: "Спробуй ще!", type: "nothing", value: "", color: "hsl(30, 10%, 20%)", probability: 25 },
        { label: "Безкоштовна доставка", type: "bonus", value: "free_delivery", color: "hsl(40, 70%, 40%)", probability: 15 },
        { label: "Майже!", type: "nothing", value: "", color: "hsl(30, 10%, 15%)", probability: 25 },
        { label: "Знижка 5%", type: "discount", value: "5", color: "hsl(40, 60%, 45%)", probability: 10 },
        { label: "Подарунок 🎁", type: "gift", value: "free_sample", color: "hsl(40, 80%, 55%)", probability: 5 },
      ]);
    }
  }, [settings]);

  const saveSetting = useMutation({
    mutationFn: async ({ key, value }: { key: string; value: any }) => {
      const { error } = await supabase.from("site_settings").upsert({ key, value }, { onConflict: "key" });
      if (error) throw error;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-settings"] });
      toast({ title: "Збережено" });
    },
    onError: (e: any) => toast({ title: "Помилка", description: e.message, variant: "destructive" }),
  });

  if (isLoading) return <p className="text-muted-foreground">Завантаження...</p>;

  const sectionLabels: Record<string, string> = {
    best_sellers: "🔥 Хіти продажу",
    shop_by_need: "🐶 За потребою (Shop by Need)",
    recently_viewed: "🕐 Нещодавно переглянуті",
    bundle_offer: "📦 Bundle-пропозиція",
    game_banner: "🎰 Банер міні-гри",
    quick_nav: "🧭 Швидка навігація (3 картки)",
    telegram_cta: "📨 Telegram-бот CTA",
    instagram_cta: "📸 Instagram CTA",
    why_us: "💡 Чому ми",
  };

  return (
    <div className="space-y-8 max-w-2xl">
      <PageHelp
        title="Налаштування магазину"
        whatIsIt="Базові налаштування сайту: які блоки показувати на головній, куди надсилати сповіщення про замовлення, кольори бренду тощо."
        whyItMatters="Тут ти 'налаштовуєш магазин під себе'. Без цих налаштувань сайт працює, але втрачає заявки (бо не приходять сповіщення) або виглядає не зовсім твоїм брендом."
        whatToDo={[
          "Секції головної: вмикай/вимикай блоки на головній сторінці (відгуки, акції, тощо).",
          "Telegram-сповіщення: введи свій chat ID — будеш отримувати в Telegram кожне нове замовлення.",
          "Кольори/логотип: завантаж свій логотип, обери основний колір бренду.",
          "Не забудь натиснути 'Зберегти' внизу після змін!",
        ]}
        tips={[
          "Не вимикай блок 'Товари' на головній — без нього сайт виглядає порожньо.",
          "Telegram-сповіщення — мастхев. Без них ти пропустиш замовлення.",
        ]}
      />
      <h1 className="text-xl font-bold">⚙️ Налаштування сайту</h1>

      {/* Homepage sections visibility */}
      <section className="bg-card rounded-lg border border-border p-5 space-y-4">
        <h2 className="font-semibold">🏠 Секції головної сторінки</h2>
        <p className="text-sm text-muted-foreground">
          Вмикайте та вимикайте блоки головної сторінки. Зміни застосовуються одразу.
        </p>
        <div className="space-y-2">
          {Object.entries(sectionLabels).map(([key, label]) => (
            <label key={key} className="flex items-center justify-between p-2 rounded-lg bg-secondary cursor-pointer">
              <span className="text-sm">{label}</span>
              <Switch
                checked={homepageSections[key] !== false}
                onCheckedChange={(v) => setHomepageSections({ ...homepageSections, [key]: v })}
              />
            </label>
          ))}
        </div>
        <Button onClick={() => saveSetting.mutate({ key: "homepage_sections", value: homepageSections })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
          <Save className="w-4 h-4 mr-1" /> Зберегти
        </Button>
      </section>

      {/* General */}
      <section className="bg-card rounded-lg border border-border p-5 space-y-4">
        <h2 className="font-semibold">Загальні</h2>
        <div className="space-y-2">
          <Label>Назва сайту</Label>
          <Input value={general.site_name || ""} onChange={(e) => setGeneral({ ...general, site_name: e.target.value })} />
        </div>
        <div className="space-y-2">
          <Label>Опис сайту</Label>
          <Textarea value={general.site_description || ""} onChange={(e) => setGeneral({ ...general, site_description: e.target.value })} />
        </div>
        <div className="space-y-2">
          <Label>Мова сайту</Label>
          <Input value={general.language || "uk"} onChange={(e) => setGeneral({ ...general, language: e.target.value })} placeholder="uk" />
        </div>
        <div className="space-y-2">
          <Label>Валюта</Label>
          <Input value={general.currency || "UAH"} onChange={(e) => setGeneral({ ...general, currency: e.target.value })} placeholder="UAH" />
        </div>
        <Button onClick={() => saveSetting.mutate({ key: "general", value: general })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
          <Save className="w-4 h-4 mr-1" /> Зберегти
        </Button>
      </section>

      {/* Branding */}
      <section className="bg-card rounded-lg border border-border p-5 space-y-4">
        <h2 className="font-semibold flex items-center gap-2"><Palette className="w-4 h-4" /> Брендинг</h2>
        <div className="space-y-2">
          <Label>URL логотипу</Label>
          <Input value={branding.logo_url || ""} onChange={(e) => setBranding({ ...branding, logo_url: e.target.value })} placeholder="https://..." />
        </div>
        <div className="space-y-2">
          <Label>URL фавікону</Label>
          <Input value={branding.favicon_url || ""} onChange={(e) => setBranding({ ...branding, favicon_url: e.target.value })} placeholder="https://..." />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1">
            <Label>Основний колір</Label>
            <Input value={branding.primary_color || ""} onChange={(e) => setBranding({ ...branding, primary_color: e.target.value })} placeholder="#D4A017" />
          </div>
          <div className="space-y-1">
            <Label>Колір акценту</Label>
            <Input value={branding.accent_color || ""} onChange={(e) => setBranding({ ...branding, accent_color: e.target.value })} placeholder="#B8860B" />
          </div>
        </div>
        <Button onClick={() => saveSetting.mutate({ key: "branding", value: branding })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
          <Save className="w-4 h-4 mr-1" /> Зберегти
        </Button>
      </section>

      {/* Working Hours */}
      <section className="bg-card rounded-lg border border-border p-5 space-y-4">
        <h2 className="font-semibold">🕐 Графік роботи</h2>
        <div className="space-y-2">
          <Label>Пн–Пт</Label>
          <Input value={workingHours.weekdays || ""} onChange={(e) => setWorkingHours({ ...workingHours, weekdays: e.target.value })} placeholder="09:00 - 18:00" />
        </div>
        <div className="space-y-2">
          <Label>Сб</Label>
          <Input value={workingHours.saturday || ""} onChange={(e) => setWorkingHours({ ...workingHours, saturday: e.target.value })} placeholder="10:00 - 15:00" />
        </div>
        <div className="space-y-2">
          <Label>Нд</Label>
          <Input value={workingHours.sunday || ""} onChange={(e) => setWorkingHours({ ...workingHours, sunday: e.target.value })} placeholder="Вихідний" />
        </div>
        <Button onClick={() => saveSetting.mutate({ key: "working_hours", value: workingHours })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
          <Save className="w-4 h-4 mr-1" /> Зберегти
        </Button>
      </section>

      {/* Payment methods */}
      <section className="bg-card rounded-lg border border-border p-5 space-y-4">
        <h2 className="font-semibold">💳 Методи оплати</h2>
        {Object.keys(paymentMethods).length === 0 ? (
          <div className="space-y-2">
            <p className="text-sm text-muted-foreground">Методи оплати не налаштовані</p>
            <Button variant="outline" size="sm" onClick={() => setPaymentMethods({
              cash_on_delivery: { enabled: true, label: "Накладений платіж" },
              card_transfer: { enabled: true, label: "Переказ на картку", details: "" }
            })}>Додати стандартні методи</Button>
          </div>
        ) : Object.entries(paymentMethods).map(([key, pm]: [string, any]) => (
          <div key={key} className="p-3 bg-secondary rounded-lg space-y-2">
            <div className="flex items-center gap-3">
              <Switch
                checked={pm.enabled}
                onCheckedChange={(v) => setPaymentMethods({ ...paymentMethods, [key]: { ...pm, enabled: v } })}
              />
              <Label className="flex-1">{key === "cash_on_delivery" ? "Накладений платіж" : "Переказ на картку"}</Label>
            </div>
            <Input
              value={pm.label || ""}
              onChange={(e) => setPaymentMethods({ ...paymentMethods, [key]: { ...pm, label: e.target.value } })}
              placeholder="Назва методу"
            />
            {key === "card_transfer" && (
              <Textarea
                value={pm.details || ""}
                onChange={(e) => setPaymentMethods({ ...paymentMethods, [key]: { ...pm, details: e.target.value } })}
                placeholder="Реквізити картки для оплати"
              />
            )}
          </div>
        ))}
        {Object.keys(paymentMethods).length > 0 && (
          <Button onClick={() => saveSetting.mutate({ key: "payment_methods", value: paymentMethods })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
            <Save className="w-4 h-4 mr-1" /> Зберегти
          </Button>
        )}
      </section>

      {/* Contacts */}
      <section className="bg-card rounded-lg border border-border p-5 space-y-4">
        <h2 className="font-semibold">📞 Контакти</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div className="space-y-1"><Label>Телефон</Label><Input value={contacts.phone || ""} onChange={(e) => setContacts({ ...contacts, phone: e.target.value })} /></div>
          <div className="space-y-1"><Label>Email</Label><Input value={contacts.email || ""} onChange={(e) => setContacts({ ...contacts, email: e.target.value })} /></div>
          <div className="space-y-1"><Label>Telegram</Label><Input value={contacts.telegram || ""} onChange={(e) => setContacts({ ...contacts, telegram: e.target.value })} /></div>
          <div className="space-y-1"><Label>Instagram</Label><Input value={contacts.instagram || ""} onChange={(e) => setContacts({ ...contacts, instagram: e.target.value })} /></div>
          <div className="space-y-1"><Label>Viber</Label><Input value={contacts.viber || ""} onChange={(e) => setContacts({ ...contacts, viber: e.target.value })} /></div>
          <div className="space-y-1"><Label>WhatsApp</Label><Input value={contacts.whatsapp || ""} onChange={(e) => setContacts({ ...contacts, whatsapp: e.target.value })} /></div>
        </div>
        <Button onClick={() => saveSetting.mutate({ key: "contacts", value: contacts })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
          <Save className="w-4 h-4 mr-1" /> Зберегти
        </Button>
      </section>

      {/* Social Links */}
      <section className="bg-card rounded-lg border border-border p-5 space-y-4">
        <h2 className="font-semibold">🔗 Соціальні мережі</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div className="space-y-1"><Label>Facebook</Label><Input value={socialLinks.facebook || ""} onChange={(e) => setSocialLinks({ ...socialLinks, facebook: e.target.value })} placeholder="https://facebook.com/..." /></div>
          <div className="space-y-1"><Label>Instagram</Label><Input value={socialLinks.instagram || ""} onChange={(e) => setSocialLinks({ ...socialLinks, instagram: e.target.value })} placeholder="https://instagram.com/..." /></div>
          <div className="space-y-1"><Label>TikTok</Label><Input value={socialLinks.tiktok || ""} onChange={(e) => setSocialLinks({ ...socialLinks, tiktok: e.target.value })} placeholder="https://tiktok.com/..." /></div>
          <div className="space-y-1"><Label>YouTube</Label><Input value={socialLinks.youtube || ""} onChange={(e) => setSocialLinks({ ...socialLinks, youtube: e.target.value })} placeholder="https://youtube.com/..." /></div>
        </div>
        <Button onClick={() => saveSetting.mutate({ key: "social_links", value: socialLinks })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
          <Save className="w-4 h-4 mr-1" /> Зберегти
        </Button>
      </section>

      {/* Delivery */}
      <section className="bg-card rounded-lg border border-border p-5 space-y-4">
        <h2 className="font-semibold">🚚 Доставка</h2>
        <div className="space-y-2">
          <Label>Інформація про доставку</Label>
          <Textarea value={delivery.text || ""} onChange={(e) => setDelivery({ ...delivery, text: e.target.value })} />
        </div>
        <div className="space-y-2">
          <Label>Вартість доставки (грн, 0 = безкоштовна)</Label>
          <Input type="number" value={delivery.price || ""} onChange={(e) => setDelivery({ ...delivery, price: e.target.value })} placeholder="0" />
        </div>
        <div className="space-y-2">
          <Label>Безкоштовна доставка від (грн)</Label>
          <Input type="number" value={delivery.free_from || ""} onChange={(e) => setDelivery({ ...delivery, free_from: e.target.value })} placeholder="500" />
        </div>
        <Button onClick={() => saveSetting.mutate({ key: "delivery_info", value: delivery })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
          <Save className="w-4 h-4 mr-1" /> Зберегти
        </Button>
      </section>

      {/* Game Prizes */}
      <section className="bg-card rounded-lg border border-border p-5 space-y-4">
        <h2 className="font-semibold">🎰 Міні-гра (Колесо Фортуни)</h2>
        <p className="text-sm text-muted-foreground">Налаштуйте призи для гри. Сторінка доступна за адресою <code className="text-primary">/game</code></p>
        {gamePrizes.map((prize, i) => (
          <div key={i} className="p-3 bg-secondary rounded-lg space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium w-6">{i + 1}.</span>
              <Input
                value={prize.label}
                onChange={(e) => {
                  const updated = [...gamePrizes];
                  updated[i] = { ...updated[i], label: e.target.value };
                  setGamePrizes(updated);
                }}
                placeholder="Назва призу"
                className="flex-1"
              />
              <select
                value={prize.type}
                onChange={(e) => {
                  const updated = [...gamePrizes];
                  updated[i] = { ...updated[i], type: e.target.value };
                  setGamePrizes(updated);
                }}
                className="bg-background border border-border rounded px-2 py-2 text-sm"
              >
                <option value="discount">Знижка</option>
                <option value="bonus">Бонус</option>
                <option value="gift">Подарунок</option>
                <option value="nothing">Порожньо</option>
              </select>
              <Button variant="ghost" size="sm" className="text-destructive" onClick={() => {
                setGamePrizes(gamePrizes.filter((_, j) => j !== i));
              }}>✕</Button>
            </div>
            <div className="grid grid-cols-3 gap-2">
              <div className="space-y-1">
                <Label className="text-xs">Значення</Label>
                <Input
                  value={prize.value}
                  onChange={(e) => {
                    const updated = [...gamePrizes];
                    updated[i] = { ...updated[i], value: e.target.value };
                    setGamePrizes(updated);
                  }}
                  placeholder="10 або free_delivery"
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">Ймовірність %</Label>
                <Input
                  type="number"
                  value={prize.probability}
                  onChange={(e) => {
                    const updated = [...gamePrizes];
                    updated[i] = { ...updated[i], probability: parseInt(e.target.value) || 0 };
                    setGamePrizes(updated);
                  }}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">Колір</Label>
                <Input
                  value={prize.color}
                  onChange={(e) => {
                    const updated = [...gamePrizes];
                    updated[i] = { ...updated[i], color: e.target.value };
                    setGamePrizes(updated);
                  }}
                  placeholder="hsl(40, 70%, 50%)"
                />
              </div>
            </div>
          </div>
        ))}
        <Button variant="outline" size="sm" onClick={() => {
          setGamePrizes([...gamePrizes, { label: "Новий приз", type: "discount", value: "", color: "hsl(40, 70%, 50%)", probability: 10 }]);
        }}>+ Додати приз</Button>
        <Button onClick={() => saveSetting.mutate({ key: "game_prizes", value: gamePrizes })} disabled={saveSetting.isPending} className="bg-primary text-primary-foreground ml-2">
          <Save className="w-4 h-4 mr-1" /> Зберегти
        </Button>
      </section>

      {/* Реквізити для накладних — використовуються на /admin/invoices.
          Якір id="invoice" дозволяє переходити сюди з кнопки на сторінці накладних. */}
      <section id="invoice" className="bg-card rounded-lg border border-border p-5 space-y-4 scroll-mt-20">
        <div>
          <h2 className="font-semibold">📄 Реквізити для накладних</h2>
          <p className="text-xs text-muted-foreground mt-1">
            Ці дані з'являються на брендованих накладних, які ви генеруєте у розділі «Накладні».
          </p>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <Label className="text-xs">Назва бренду (велика в шапці)</Label>
            <Input
              value={invoice.brand_name || ""}
              onChange={(e) => setInvoice({ ...invoice, brand_name: e.target.value })}
              placeholder="BASIC.FOOD"
            />
          </div>
          <div>
            <Label className="text-xs">Юридична назва</Label>
            <Input
              value={invoice.legal_name || ""}
              onChange={(e) => setInvoice({ ...invoice, legal_name: e.target.value })}
              placeholder="ФОП Прізвище І.І."
            />
          </div>
          <div>
            <Label className="text-xs">ЄДРПОУ / ІПН</Label>
            <Input
              value={invoice.tax_id || ""}
              onChange={(e) => setInvoice({ ...invoice, tax_id: e.target.value })}
              placeholder="3xxxxxxxxx"
            />
          </div>
          <div>
            <Label className="text-xs">Юридична адреса</Label>
            <Input
              value={invoice.legal_address || ""}
              onChange={(e) => setInvoice({ ...invoice, legal_address: e.target.value })}
              placeholder="м. Рівне, вул. ..."
            />
          </div>
          <div>
            <Label className="text-xs">Телефон</Label>
            <Input
              value={invoice.phone || ""}
              onChange={(e) => setInvoice({ ...invoice, phone: e.target.value })}
              placeholder="+380 XX XXX XX XX"
            />
          </div>
          <div>
            <Label className="text-xs">Email</Label>
            <Input
              value={invoice.email || ""}
              onChange={(e) => setInvoice({ ...invoice, email: e.target.value })}
              placeholder="info@basicfood.com"
            />
          </div>
          <div>
            <Label className="text-xs">IBAN</Label>
            <Input
              value={invoice.iban || ""}
              onChange={(e) => setInvoice({ ...invoice, iban: e.target.value })}
              placeholder="UA00..."
            />
          </div>
          <div>
            <Label className="text-xs">Назва банку</Label>
            <Input
              value={invoice.bank_name || ""}
              onChange={(e) => setInvoice({ ...invoice, bank_name: e.target.value })}
              placeholder="АТ «Універсал Банк»"
            />
          </div>
          <div>
            <Label className="text-xs">URL логотипа (PNG/JPG)</Label>
            <Input
              value={invoice.logo_url || ""}
              onChange={(e) => setInvoice({ ...invoice, logo_url: e.target.value })}
              placeholder="https://..."
            />
          </div>
          <div>
            <Label className="text-xs">Підпис: посада</Label>
            <Input
              value={invoice.signatory_title || ""}
              onChange={(e) => setInvoice({ ...invoice, signatory_title: e.target.value })}
              placeholder="Директор"
            />
          </div>
          <div className="md:col-span-2">
            <Label className="text-xs">Підпис: ПІБ</Label>
            <Input
              value={invoice.signatory_name || ""}
              onChange={(e) => setInvoice({ ...invoice, signatory_name: e.target.value })}
              placeholder="Прізвище І.І."
            />
          </div>
          <div className="md:col-span-2">
            <Label className="text-xs">Підпис у футері накладної</Label>
            <Textarea
              rows={2}
              value={invoice.footer_note || ""}
              onChange={(e) => setInvoice({ ...invoice, footer_note: e.target.value })}
              placeholder="Дякуємо за замовлення!"
            />
          </div>
        </div>
        <Button
          onClick={() => saveSetting.mutate({ key: "invoice", value: invoice })}
          disabled={saveSetting.isPending}
          className="bg-primary text-primary-foreground"
        >
          <Save className="w-4 h-4 mr-1" /> Зберегти реквізити
        </Button>
      </section>

      <AcosSystemTools />

      {/* Telegram */}
      <section className="bg-card rounded-lg border border-border p-5 space-y-4">
        <h2 className="font-semibold">📱 Telegram сповіщення</h2>
        <p className="text-sm text-muted-foreground">
          Щоб отримувати сповіщення про нові замовлення в Telegram:
        </p>
        <ol className="text-sm text-muted-foreground list-decimal list-inside space-y-1">
          <li>Знайдіть бота в Telegram і напишіть <code className="text-primary">/start</code></li>
          <li>Бот покаже ваш Chat ID</li>
          <li>Введіть Chat ID нижче</li>
        </ol>
        <TelegramLink userId={user?.id} />
      </section>
    </div>
  );
};

const TelegramLink = ({ userId }: { userId?: string }) => {
  const [chatId, setChatId] = useState("");
  const { toast } = useToast();
  const qc = useQueryClient();

  const { data: existing } = useQuery({
    queryKey: ["my-telegram-chat", userId],
    queryFn: async () => {
      const { data } = await supabase.from("telegram_chat_ids").select("chat_id").eq("user_id", userId!).maybeSingle();
      return data;
    },
    enabled: !!userId,
  });

  const save = useMutation({
    mutationFn: async () => {
      const { error } = await supabase.from("telegram_chat_ids").upsert(
        { user_id: userId!, chat_id: parseInt(chatId) },
        { onConflict: "user_id" }
      );
      if (error) throw error;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["my-telegram-chat"] });
      toast({ title: "Telegram прив'язано!" });
    },
    onError: (e: any) => toast({ title: "Помилка", description: e.message, variant: "destructive" }),
  });

  if (existing) {
    return <p className="text-sm text-green-400">✅ Telegram прив'язано (Chat ID: {String(existing.chat_id)})</p>;
  }

  return (
    <div className="flex gap-2">
      <Input value={chatId} onChange={(e) => setChatId(e.target.value)} placeholder="Ваш Chat ID" className="max-w-xs" />
      <Button onClick={() => save.mutate()} disabled={!chatId || save.isPending} className="bg-primary text-primary-foreground">
        <Send className="w-4 h-4 mr-1" /> Прив'язати
      </Button>
    </div>
  );
};

export default AdminSettings;
