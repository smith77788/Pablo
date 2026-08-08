import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { supabase } from "@/integrations/supabase/client";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Loader2, RefreshCw, AlertTriangle, Package, TrendingUp, TrendingDown } from "lucide-react";
import { useToast } from "@/hooks/use-toast";

type Forecast = {
  id: string;
  product_id: string;
  current_stock: number;
  avg_daily_sales: number;
  recent_7d_daily: number;
  trend_multiplier: number;
  forecast_7d: number;
  forecast_14d: number;
  forecast_30d: number;
  days_until_stockout: number | null;
  stockout_date: string | null;
  recommended_production: number;
  urgency: "ok" | "watch" | "warning" | "critical" | "stockout";
  computed_at: string;
};

const URGENCY_ORDER: Record<Forecast["urgency"], number> = {
  stockout: 0, critical: 1, warning: 2, watch: 3, ok: 4,
};

const urgencyVariant = (u: Forecast["urgency"]) => {
  switch (u) {
    case "stockout": return "destructive";
    case "critical": return "destructive";
    case "warning": return "default";
    case "watch": return "secondary";
    default: return "outline";
  }
};

const urgencyLabel = (u: Forecast["urgency"]) => ({
  stockout: "STOCKOUT",
  critical: "Критично",
  warning: "Попередження",
  watch: "Слідкувати",
  ok: "OK",
}[u]);

const AdminInventoryForecast = () => {
  const { toast } = useToast();
  const qc = useQueryClient();
  const [productNames, setProductNames] = useState<Record<string, string>>({});

  const { data: forecasts, isLoading } = useQuery({
    queryKey: ["inventory-forecasts"],
    queryFn: async () => {
      const { data, error } = await supabase
        .from("inventory_forecasts" as never)
        .select("*")
        .order("computed_at", { ascending: false });
      if (error) throw error;
      return (data ?? []) as unknown as Forecast[];
    },
    refetchInterval: 60_000,
  });

  useEffect(() => {
    if (!forecasts || forecasts.length === 0) return;
    const ids = forecasts.map((f) => f.product_id);
    supabase
      .from("products")
      .select("id, name")
      .in("id", ids)
      .then(({ data }) => {
        if (data) setProductNames(Object.fromEntries(data.map((p) => [p.id, p.name])));
      })
      .catch((err) => console.error("[forecast] product names load failed:", err));
  }, [forecasts]);

  const recompute = useMutation({
    mutationFn: async () => {
      const { data, error } = await supabase.functions.invoke("inventory-forecaster");
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      toast({ title: "Прогноз оновлено", description: `Оброблено ${data?.products_processed ?? 0} товарів` });
      qc.invalidateQueries({ queryKey: ["inventory-forecasts"] });
    },
    onError: (e: unknown) => {
      toast({ title: "Помилка", description: e instanceof Error ? e.message : String(e), variant: "destructive" });
    },
  });

  const sorted = [...(forecasts ?? [])].sort((a, b) => {
    const ua = URGENCY_ORDER[a.urgency] - URGENCY_ORDER[b.urgency];
    if (ua !== 0) return ua;
    return (a.days_until_stockout ?? 9999) - (b.days_until_stockout ?? 9999);
  });

  const counts = (forecasts ?? []).reduce(
    (acc, f) => { acc[f.urgency] = (acc[f.urgency] ?? 0) + 1; return acc; },
    {} as Record<string, number>,
  );

  return (
    <div className="container mx-auto px-4 py-6 space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Package className="h-6 w-6 text-primary" />
            Прогноз запасів
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            Передбачення попиту на основі продажів за 30 днів з вагою останніх 7
          </p>
        </div>
        <Button onClick={() => recompute.mutate()} disabled={recompute.isPending}>
          {recompute.isPending ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <RefreshCw className="h-4 w-4 mr-2" />}
          Перерахувати зараз
        </Button>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        {(["stockout","critical","warning","watch","ok"] as const).map((u) => (
          <Card key={u}>
            <CardContent className="p-4">
              <div className="text-xs text-muted-foreground">{urgencyLabel(u)}</div>
              <div className="text-2xl font-bold mt-1">{counts[u] ?? 0}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      {isLoading ? (
        <div className="flex justify-center py-12"><Loader2 className="h-6 w-6 animate-spin" /></div>
      ) : sorted.length === 0 ? (
        <Card><CardContent className="py-12 text-center text-muted-foreground">
          Немає даних. Натисни «Перерахувати зараз».
        </CardContent></Card>
      ) : (
        <div className="space-y-3">
          {sorted.map((f) => {
            const name = productNames[f.product_id] ?? f.product_id.slice(0, 8);
            const trendIcon = f.trend_multiplier > 1.1 ? <TrendingUp className="h-4 w-4 text-green-500" />
              : f.trend_multiplier < 0.9 ? <TrendingDown className="h-4 w-4 text-red-500" />
              : null;
            return (
              <Card key={f.id} className={f.urgency === "critical" || f.urgency === "stockout" ? "border-destructive/50" : ""}>
                <CardHeader className="pb-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <CardTitle className="text-base truncate">{name}</CardTitle>
                      <CardDescription className="flex items-center gap-2 mt-1">
                        Залишок: <strong>{f.current_stock}</strong> · Продажі: <strong>{f.recent_7d_daily}/день</strong> {trendIcon}
                      </CardDescription>
                    </div>
                    <Badge variant={urgencyVariant(f.urgency) as never}>
                      {f.urgency === "critical" || f.urgency === "stockout" ? <AlertTriangle className="h-3 w-3 mr-1" /> : null}
                      {urgencyLabel(f.urgency)}
                    </Badge>
                  </div>
                </CardHeader>
                <CardContent className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
                  <div>
                    <div className="text-muted-foreground text-xs">До stockout</div>
                    <div className="font-semibold">
                      {f.days_until_stockout != null ? `${f.days_until_stockout} дн` : "∞"}
                      {f.stockout_date && <div className="text-xs text-muted-foreground">~{new Date(f.stockout_date).toLocaleDateString("uk-UA")}</div>}
                    </div>
                  </div>
                  <div>
                    <div className="text-muted-foreground text-xs">Прогноз 7/14/30 дн</div>
                    <div className="font-semibold">{f.forecast_7d} / {f.forecast_14d} / {f.forecast_30d}</div>
                  </div>
                  <div>
                    <div className="text-muted-foreground text-xs">Тренд</div>
                    <div className="font-semibold">{(f.trend_multiplier * 100).toFixed(0)}%</div>
                  </div>
                  <div>
                    <div className="text-muted-foreground text-xs">Виготовити</div>
                    <div className="font-bold text-primary">{f.recommended_production} шт</div>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default AdminInventoryForecast;
