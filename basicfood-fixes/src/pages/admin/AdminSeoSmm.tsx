import { useState, useEffect, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import ContentGenerator from "@/components/admin/ContentGenerator";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/hooks/use-toast";
import { Save, Globe, Share2, BarChart3, Code, FileText, Link2, Zap } from "lucide-react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import SeoIndexingPanel from "@/components/admin/SeoIndexingPanel";

const AdminSeoSmm = () => {
  const { toast } = useToast();
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

  const [seo, setSeo] = useState<any>({});
  const [ogTags, setOgTags] = useState<any>({});
  const [analytics, setAnalytics] = useState<any>({});
  const [smm, setSmm] = useState<any>({});
  const [schema, setSchema] = useState<any>({});
  const merchantFeedBase = useMemo(
    () => `${import.meta.env.VITE_SUPABASE_URL}/functions/v1/google-merchant-feed`,
    [],
  );
  const merchantFeedEn = `${merchantFeedBase}?lang=en`;

  const copyText = async (text: string, label: string) => {
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else if (typeof document !== "undefined") {
        const input = document.createElement("textarea");
        input.value = text;
        input.setAttribute("readonly", "true");
        input.style.position = "fixed";
        input.style.opacity = "0";
        document.body.appendChild(input);
        input.select();
        document.execCommand("copy");
        document.body.removeChild(input);
      } else {
        throw new Error("clipboard_unavailable");
      }
      toast({ title: "Скопійовано", description: label });
    } catch {
      toast({ title: "Не вдалося скопіювати", description: label, variant: "destructive" });
    }
  };

  useEffect(() => {
    if (settings) {
      setSeo(settings.seo || {});
      setOgTags(settings.og_tags || {});
      setAnalytics(settings.analytics || {});
      setSmm(settings.smm || {});
      setSchema(settings.schema_org || {});
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

  return (
    <div className="space-y-6 max-w-3xl">
      <h1 className="text-xl font-bold">🔍 SEO & SMM</h1>

      <Tabs defaultValue="seo">
        <TabsList className="grid w-full grid-cols-2 gap-2 h-auto md:grid-cols-7">
          <TabsTrigger value="indexing" className="gap-1 text-xs"><Zap className="w-3.5 h-3.5" /> Індексація</TabsTrigger>
          <TabsTrigger value="seo" className="gap-1 text-xs"><Globe className="w-3.5 h-3.5" /> SEO</TabsTrigger>
          <TabsTrigger value="og" className="gap-1 text-xs"><Share2 className="w-3.5 h-3.5" /> OG</TabsTrigger>
          <TabsTrigger value="analytics" className="gap-1 text-xs"><BarChart3 className="w-3.5 h-3.5" /> Analytics</TabsTrigger>
          <TabsTrigger value="schema" className="gap-1 text-xs"><Code className="w-3.5 h-3.5" /> Schema</TabsTrigger>
          <TabsTrigger value="smm" className="gap-1 text-xs"><FileText className="w-3.5 h-3.5" /> SMM</TabsTrigger>
          <TabsTrigger value="domain" className="gap-1 text-xs"><Link2 className="w-3.5 h-3.5" /> Домен</TabsTrigger>
        </TabsList>

        {/* Indexing — IndexNow + sitemap ping + GSC instructions */}
        <TabsContent value="indexing" className="mt-4">
          <SeoIndexingPanel />
        </TabsContent>


        {/* SEO */}
        <TabsContent value="seo" className="space-y-4 mt-4">
          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">Мета-теги</h2>
            <div className="space-y-2">
              <Label>Title (до 60 символів)</Label>
              <Input value={seo.title || ""} onChange={(e) => setSeo({ ...seo, title: e.target.value })} placeholder="BASIC.FOOD — Сушене м'ясо для тварин" maxLength={60} />
              <p className="text-xs text-muted-foreground">{(seo.title || "").length}/60</p>
            </div>
            <div className="space-y-2">
              <Label>Meta Description (до 160 символів)</Label>
              <Textarea value={seo.description || ""} onChange={(e) => setSeo({ ...seo, description: e.target.value })} placeholder="Натуральні сушені ласощі для собак та котів..." maxLength={160} />
              <p className="text-xs text-muted-foreground">{(seo.description || "").length}/160</p>
            </div>
            <div className="space-y-2">
              <Label>Keywords (через кому)</Label>
              <Input value={seo.keywords || ""} onChange={(e) => setSeo({ ...seo, keywords: e.target.value })} placeholder="сушене м'ясо, ласощі для собак, натуральний корм" />
            </div>
            <div className="space-y-2">
              <Label>Canonical URL</Label>
              <Input value={seo.canonical || ""} onChange={(e) => setSeo({ ...seo, canonical: e.target.value })} placeholder="https://basic-food.shop" />
            </div>
            <div className="flex items-center gap-3">
              <Switch checked={seo.index !== false} onCheckedChange={(v) => setSeo({ ...seo, index: v })} />
              <Label>Дозволити індексацію (index, follow)</Label>
            </div>
            <Button onClick={() => saveSetting.mutate({ key: "seo", value: seo })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
              <Save className="w-4 h-4 mr-1" /> Зберегти
            </Button>
          </section>

          <section className="bg-card rounded-lg border border-border p-5 space-y-3">
            <h2 className="font-semibold">Robots.txt</h2>
            <p className="text-xs text-muted-foreground">
              Файл <code className="bg-muted px-1 py-0.5 rounded">robots.txt</code> віддається статично з{" "}
              <code className="bg-muted px-1 py-0.5 rounded">public/robots.txt</code> і не редагується звідси —
              інакше Googlebot побачив би одне, а ваш CDN віддавав би інше.
              Щоб змінити правила, відредагуйте файл у репозиторії або попросіть розробника.
            </p>
            <Button asChild size="sm" variant="outline">
              <a href="/robots.txt" target="_blank" rel="noreferrer">
                Переглянути поточний robots.txt
              </a>
            </Button>
          </section>

          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">Sitemap</h2>
            <div className="space-y-2">
              <Label>URL карти сайту</Label>
              <Input value={seo.sitemap_url || ""} onChange={(e) => setSeo({ ...seo, sitemap_url: e.target.value })} placeholder="https://basic-food.shop/sitemap.xml" />
            </div>
            <div className="flex items-center gap-3">
              <Switch checked={seo.auto_sitemap !== false} onCheckedChange={(v) => setSeo({ ...seo, auto_sitemap: v })} />
              <Label>Автоматична генерація sitemap</Label>
            </div>
            <Button onClick={() => saveSetting.mutate({ key: "seo", value: seo })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
              <Save className="w-4 h-4 mr-1" /> Зберегти
            </Button>
          </section>

          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">🛒 Google Merchant Center Feed</h2>
            <p className="text-sm text-muted-foreground">
              XML-фід усіх активних товарів у форматі Google Shopping. Підключіть у Merchant Center → <strong>Products → Feeds → Add → Scheduled fetch</strong>.
            </p>
            <div className="space-y-2">
              <Label>URL фіду (українська)</Label>
              <div className="flex gap-2">
                <Input
                  readOnly
                    value={merchantFeedBase}
                  className="font-mono text-xs"
                  onFocus={(e) => e.target.select()}
                />
                <Button
                  variant="outline"
                    onClick={() => void copyText(merchantFeedBase, "URL фіду (українська)")}
                >Копіювати</Button>
              </div>
            </div>
            <div className="space-y-2">
              <Label>URL фіду (English)</Label>
              <div className="flex gap-2">
                <Input
                  readOnly
                    value={merchantFeedEn}
                  className="font-mono text-xs"
                  onFocus={(e) => e.target.select()}
                />
                <Button
                  variant="outline"
                    onClick={() => void copyText(merchantFeedEn, "URL фіду (English)")}
                >Копіювати</Button>
              </div>
            </div>
            <div className="bg-secondary rounded-lg p-3 text-xs text-muted-foreground space-y-1">
              <p>✓ Включає: title, description, price, image, availability, brand, shipping (Nova Poshta), Google product category</p>
              <p>✓ Auto-refresh: фід завжди показує актуальні товари з БД (cache 1 год)</p>
              <p>✓ Безкоштовна доставка від {800} ₴ враховується автоматично</p>
              <p className="text-primary mt-2">⚠ У Merchant Center встановіть: Country = Ukraine, Language = Ukrainian (або English для en-фіду), Frequency = Daily</p>
            </div>
            <Button
              variant="outline"
              onClick={() => window.open(merchantFeedBase, "_blank", "noopener,noreferrer")}
            >
              Переглянути фід →
            </Button>
          </section>
        </TabsContent>

        {/* OG Tags */}
        <TabsContent value="og" className="space-y-4 mt-4">
          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">Open Graph (Facebook, Telegram)</h2>
            <div className="space-y-2">
              <Label>OG Title</Label>
              <Input value={ogTags.title || ""} onChange={(e) => setOgTags({ ...ogTags, title: e.target.value })} placeholder="BASIC.FOOD" />
            </div>
            <div className="space-y-2">
              <Label>OG Description</Label>
              <Textarea value={ogTags.description || ""} onChange={(e) => setOgTags({ ...ogTags, description: e.target.value })} placeholder="Натуральні сушені ласощі для тварин" />
            </div>
            <div className="space-y-2">
              <Label>OG Image URL</Label>
              <Input value={ogTags.image || ""} onChange={(e) => setOgTags({ ...ogTags, image: e.target.value })} placeholder="https://basic-food.shop/og-image.jpg" />
            </div>
            <div className="space-y-2">
              <Label>OG URL</Label>
              <Input value={ogTags.url || ""} onChange={(e) => setOgTags({ ...ogTags, url: e.target.value })} placeholder="https://basic-food.shop" />
            </div>
            <div className="space-y-2">
              <Label>OG Type</Label>
              <Input value={ogTags.type || "website"} onChange={(e) => setOgTags({ ...ogTags, type: e.target.value })} placeholder="website" />
            </div>
            <Button onClick={() => saveSetting.mutate({ key: "og_tags", value: ogTags })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
              <Save className="w-4 h-4 mr-1" /> Зберегти
            </Button>
          </section>

          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">Twitter Card</h2>
            <div className="space-y-2">
              <Label>Twitter Card Type</Label>
              <Input value={ogTags.twitter_card || "summary_large_image"} onChange={(e) => setOgTags({ ...ogTags, twitter_card: e.target.value })} />
            </div>
            <div className="space-y-2">
              <Label>Twitter @username</Label>
              <Input value={ogTags.twitter_site || ""} onChange={(e) => setOgTags({ ...ogTags, twitter_site: e.target.value })} placeholder="@basicfood" />
            </div>
            <Button onClick={() => saveSetting.mutate({ key: "og_tags", value: ogTags })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
              <Save className="w-4 h-4 mr-1" /> Зберегти
            </Button>
          </section>
        </TabsContent>

        {/* Analytics */}
        <TabsContent value="analytics" className="space-y-4 mt-4">
          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">Google Analytics</h2>
            <div className="space-y-2">
              <Label>Google Analytics ID (GA4)</Label>
              <Input value={analytics.ga_id || ""} onChange={(e) => setAnalytics({ ...analytics, ga_id: e.target.value })} placeholder="G-XXXXXXXXXX" />
            </div>
            <div className="flex items-center gap-3">
              <Switch checked={analytics.ga_enabled || false} onCheckedChange={(v) => setAnalytics({ ...analytics, ga_enabled: v })} />
              <Label>Увімкнути Google Analytics</Label>
            </div>
          </section>

          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">Google Tag Manager</h2>
            <div className="space-y-2">
              <Label>GTM ID</Label>
              <Input value={analytics.gtm_id || ""} onChange={(e) => setAnalytics({ ...analytics, gtm_id: e.target.value })} placeholder="GTM-XXXXXXX" />
            </div>
            <div className="flex items-center gap-3">
              <Switch checked={analytics.gtm_enabled || false} onCheckedChange={(v) => setAnalytics({ ...analytics, gtm_enabled: v })} />
              <Label>Увімкнути GTM</Label>
            </div>
          </section>

          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">Facebook Pixel</h2>
            <div className="space-y-2">
              <Label>Pixel ID</Label>
              <Input value={analytics.fb_pixel || ""} onChange={(e) => setAnalytics({ ...analytics, fb_pixel: e.target.value })} placeholder="123456789" />
            </div>
            <div className="flex items-center gap-3">
              <Switch checked={analytics.fb_enabled || false} onCheckedChange={(v) => setAnalytics({ ...analytics, fb_enabled: v })} />
              <Label>Увімкнути Facebook Pixel</Label>
            </div>
          </section>

          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">Довільний код в &lt;head&gt;</h2>
            <div className="space-y-2">
              <Label>HTML/JS код (буде вставлено в head)</Label>
              <Textarea
                value={analytics.custom_head || ""}
                onChange={(e) => setAnalytics({ ...analytics, custom_head: e.target.value })}
                rows={5}
                className="font-mono text-xs"
                placeholder="<!-- Ваш код тут -->"
              />
            </div>
          </section>

          <Button onClick={() => saveSetting.mutate({ key: "analytics", value: analytics })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
            <Save className="w-4 h-4 mr-1" /> Зберегти
          </Button>
        </TabsContent>

        {/* Schema.org */}
        <TabsContent value="schema" className="space-y-4 mt-4">
          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">Schema.org (JSON-LD)</h2>
            <div className="space-y-2">
              <Label>Назва організації</Label>
              <Input value={schema.org_name || ""} onChange={(e) => setSchema({ ...schema, org_name: e.target.value })} placeholder="BASIC.FOOD" />
            </div>
            <div className="space-y-2">
              <Label>Тип бізнесу</Label>
              <Input value={schema.business_type || ""} onChange={(e) => setSchema({ ...schema, business_type: e.target.value })} placeholder="PetStore" />
            </div>
            <div className="space-y-2">
              <Label>URL сайту</Label>
              <Input value={schema.url || ""} onChange={(e) => setSchema({ ...schema, url: e.target.value })} placeholder="https://basic-food.shop" />
            </div>
            <div className="space-y-2">
              <Label>Телефон</Label>
              <Input value={schema.phone || ""} onChange={(e) => setSchema({ ...schema, phone: e.target.value })} placeholder="+380..." />
            </div>
            <div className="space-y-2">
              <Label>Адреса</Label>
              <Input value={schema.address || ""} onChange={(e) => setSchema({ ...schema, address: e.target.value })} placeholder="Київ, Україна" />
            </div>
            <div className="space-y-2">
              <Label>Логотип URL</Label>
              <Input value={schema.logo || ""} onChange={(e) => setSchema({ ...schema, logo: e.target.value })} placeholder="https://..." />
            </div>
            <div className="space-y-2">
              <Label>Довільний JSON-LD</Label>
              <Textarea
                value={schema.custom_jsonld || ""}
                onChange={(e) => setSchema({ ...schema, custom_jsonld: e.target.value })}
                rows={6}
                className="font-mono text-xs"
                placeholder='{"@context": "https://schema.org", ...}'
              />
            </div>
            <Button onClick={() => saveSetting.mutate({ key: "schema_org", value: schema })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
              <Save className="w-4 h-4 mr-1" /> Зберегти
            </Button>
          </section>
        </TabsContent>

        {/* SMM */}
        <TabsContent value="smm" className="space-y-4 mt-4">
          <ContentGenerator />

          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">SMM Налаштування</h2>
            <div className="space-y-2">
              <Label>Хештеги за замовчуванням</Label>
              <Textarea value={smm.default_hashtags || ""} onChange={(e) => setSmm({ ...smm, default_hashtags: e.target.value })} placeholder="#basicfood #сушенем'ясо #ласощідлясобак" />
            </div>
            <div className="space-y-2">
              <Label>UTM Source за замовчуванням</Label>
              <Input value={smm.utm_source || ""} onChange={(e) => setSmm({ ...smm, utm_source: e.target.value })} placeholder="instagram" />
            </div>
            <div className="space-y-2">
              <Label>UTM Medium за замовчуванням</Label>
              <Input value={smm.utm_medium || ""} onChange={(e) => setSmm({ ...smm, utm_medium: e.target.value })} placeholder="social" />
            </div>
            <div className="space-y-2">
              <Label>UTM Campaign за замовчуванням</Label>
              <Input value={smm.utm_campaign || ""} onChange={(e) => setSmm({ ...smm, utm_campaign: e.target.value })} placeholder="spring_2026" />
            </div>
          </section>

          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">Генератор UTM посилань</h2>
            <div className="space-y-2">
              <Label>Базовий URL</Label>
              <Input value={smm.link_base || ""} onChange={(e) => setSmm({ ...smm, link_base: e.target.value })} placeholder="https://basic-food.shop" />
            </div>
            {smm.link_base && (
              <div className="bg-secondary rounded p-3">
                <p className="text-xs text-muted-foreground mb-1">Згенероване посилання:</p>
                <code className="text-xs text-primary break-all">
                  {`${smm.link_base}?utm_source=${smm.utm_source || "source"}&utm_medium=${smm.utm_medium || "medium"}&utm_campaign=${smm.utm_campaign || "campaign"}`}
                </code>
              </div>
            )}
          </section>

          <Button onClick={() => saveSetting.mutate({ key: "smm", value: smm })} className="bg-primary text-primary-foreground" disabled={saveSetting.isPending}>
            <Save className="w-4 h-4 mr-1" /> Зберегти
          </Button>
        </TabsContent>

        {/* Domain */}
        <TabsContent value="domain" className="space-y-4 mt-4">
          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">🌐 Підключення власного домену</h2>
            <p className="text-sm text-muted-foreground">
              Щоб підключити власний домен (наприклад <code className="text-primary">basic-food.shop</code>) до вашого сайту:
            </p>
            <ol className="text-sm text-muted-foreground space-y-2 list-decimal list-inside">
              <li>Відкрийте <strong>Project Settings → Domains</strong> в Lovable</li>
              <li>Натисніть <strong>Connect Domain</strong> та введіть ваш домен</li>
              <li>Додайте DNS-записи у вашого реєстратора домену:</li>
            </ol>
            <div className="bg-secondary rounded-lg p-4 space-y-2">
              <p className="text-xs font-semibold text-foreground">DNS записи (A Record):</p>
              <div className="grid grid-cols-3 gap-2 text-xs font-mono">
                <span className="text-muted-foreground">Type</span><span className="text-muted-foreground">Name</span><span className="text-muted-foreground">Value</span>
                <span>A</span><span>@</span><span className="text-primary">185.158.133.1</span>
                <span>A</span><span>www</span><span className="text-primary">185.158.133.1</span>
              </div>
              <p className="text-xs font-semibold text-foreground mt-3">TXT запис (верифікація):</p>
              <div className="grid grid-cols-3 gap-2 text-xs font-mono">
                <span className="text-muted-foreground">Type</span><span className="text-muted-foreground">Name</span><span className="text-muted-foreground">Value</span>
                <span>TXT</span><span>_lovable</span><span className="text-primary">lovable_verify=...</span>
              </div>
              <p className="text-xs text-muted-foreground mt-2">* Точне значення TXT запису буде показане при підключенні домену</p>
            </div>
          </section>

          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">📧 Email DNS записи (для пошти на домені)</h2>
            <p className="text-sm text-muted-foreground">Якщо вам потрібна пошта на власному домені, додайте наступні записи:</p>
            <div className="bg-secondary rounded-lg p-4 space-y-2">
              <p className="text-xs font-semibold">MX записи (пошта):</p>
              <p className="text-xs text-muted-foreground font-mono">Залежать від вашого поштового провайдера (Gmail, Zoho, тощо)</p>
              <p className="text-xs font-semibold mt-2">SPF запис (захист від спаму):</p>
              <p className="text-xs text-muted-foreground font-mono">TXT @ "v=spf1 include:_spf.google.com ~all"</p>
              <p className="text-xs font-semibold mt-2">DMARC запис:</p>
              <p className="text-xs text-muted-foreground font-mono">TXT _dmarc "v=DMARC1; p=quarantine; rua=mailto:admin@yourdomain"</p>
            </div>
          </section>

          <section className="bg-card rounded-lg border border-border p-5 space-y-4">
            <h2 className="font-semibold">⚡ Cloudflare / Proxy</h2>
            <p className="text-sm text-muted-foreground">
              Якщо ви використовуєте Cloudflare або інший DNS-проксі, при підключенні домену увімкніть опцію <strong>"Domain uses Cloudflare or a similar proxy"</strong> в розділі Advanced.
            </p>
            <p className="text-sm text-muted-foreground">
              SSL сертифікат буде автоматично випущений після верифікації DNS. Пропагація DNS може зайняти до 72 годин.
            </p>
          </section>
        </TabsContent>
      </Tabs>
    </div>
  );
};

export default AdminSeoSmm;
