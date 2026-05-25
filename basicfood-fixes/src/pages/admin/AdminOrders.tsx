import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { supabase } from "@/integrations/supabase/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/hooks/use-toast";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Eye, Search } from "lucide-react";
import { CreateExternalOrderDialog } from "@/components/admin/CreateExternalOrderDialog";
import { PageHelp } from "@/components/admin/PageHelp";

const statusLabels: Record<string, string> = {
  new: "Новий",
  processing: "Підтверджено",
  shipped: "Відправлено",
  delivered: "Доставлено",
  completed: "Виконано",
  cancelled: "Скасовано",
};

const statusColors: Record<string, string> = {
  new: "bg-blue-900/30 text-blue-400",
  processing: "bg-yellow-900/30 text-yellow-400",
  shipped: "bg-purple-900/30 text-purple-400",
  delivered: "bg-green-900/30 text-green-400",
  completed: "bg-green-900/30 text-green-400",
  cancelled: "bg-red-900/30 text-red-400",
};

const paymentLabels: Record<string, string> = {
  cash_on_delivery: "Накладений платіж",
  card_transfer: "Переказ на картку",
  card_online: "Картка онлайн",
};

type PaymentBadge = { label: string; cls: string };

function getPaymentBadge(order: any): PaymentBadge | null {
  // Тільки для онлайн-оплати показуємо статус оплати
  if (order.payment_method !== "card_online") return null;
  if (order.status === "cancelled") return { label: "Скасовано", cls: "bg-red-900/30 text-red-400" };
  if (["processing", "shipped", "delivered", "completed"].includes(order.status)) {
    return { label: "Оплачено", cls: "bg-green-900/30 text-green-400" };
  }
  return { label: "Очікує", cls: "bg-yellow-900/30 text-yellow-400" };
}

import { useDataMode } from "@/contexts/DataModeContext";

const AdminOrders = () => {
  const { toast } = useToast();
  const qc = useQueryClient();
  const { realOnly } = useDataMode();
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [paymentStatusFilter, setPaymentStatusFilter] = useState("all");
  const [detailOrder, setDetailOrder] = useState<any>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [pendingStatus, setPendingStatus] = useState<{ id: string; value: string } | null>(null);

  const { data: orders = [], isLoading } = useQuery({
    queryKey: ["admin-orders", realOnly],
    queryFn: async () => {
      let q = supabase.from("orders").select("*").order("created_at", { ascending: false });
      if (realOnly) q = q.neq("message", "seed");
      const { data, error } = await q;
      if (error) throw error;
      return data ?? [];
    },
  });

  const { data: orderItems = [] } = useQuery({
    queryKey: ["order-items", detailOrder?.id],
    queryFn: async () => {
      const { data, error } = await supabase.from("order_items").select("*").eq("order_id", detailOrder!.id);
      if (error) throw error;
      return data ?? [];
    },
    enabled: !!detailOrder,
  });

  const updateOrder = useMutation({
    mutationFn: async ({ id, updates }: { id: string; updates: any }) => {
      const { error } = await supabase.from("orders").update(updates).eq("id", id);
      if (error) throw error;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-orders"] });
      toast({ title: "Оновлено" });
    },
  });

  const filtered = orders.filter((o: any) => {
    const matchSearch = !search || o.customer_name.toLowerCase().includes(search.toLowerCase()) || o.customer_phone?.includes(search) || o.id.includes(search);
    const matchStatus = statusFilter === "all" || o.status === statusFilter;
    let matchPayment = true;
    if (paymentStatusFilter !== "all") {
      const badge = getPaymentBadge(o);
      if (paymentStatusFilter === "cod") {
        matchPayment = o.payment_method !== "card_online";
      } else if (paymentStatusFilter === "paid") {
        matchPayment = badge?.label === "Оплачено";
      } else if (paymentStatusFilter === "pending") {
        matchPayment = badge?.label === "Очікує";
      } else if (paymentStatusFilter === "failed") {
        matchPayment = badge?.label === "Скасовано";
      }
    }
    return matchSearch && matchStatus && matchPayment;
  });

  const openDetail = (order: any) => {
    setDetailOrder(order);
    setDetailOpen(true);
  };

  return (
    <div>
      <PageHelp
        title="Замовлення"
        whatIsIt="Тут зібрані всі замовлення, які зробили клієнти на сайті — нові, в обробці, відправлені та доставлені."
        whyItMatters="Це найважливіша сторінка магазину. Якщо клієнт замовив, але ти не побачив — він не отримає товар і більше не повернеться."
        whatToDo={[
          "Перевір верх списку — нові замовлення зі статусом 'Новий' треба обробити першими.",
          "Натисни на замовлення (іконка ока 👁️) — побачиш товари, адресу, контакти.",
          "Зміни статус: Підтверджено → Відправлено → Доставлено. Клієнт автоматично отримає сповіщення.",
          "Кнопка 'Створити замовлення' — якщо клієнт замовив через дзвінок або Telegram, додай його сюди вручну.",
        ]}
        tips={[
          "Фільтр зверху допомагає швидко знайти потрібний статус (наприклад, тільки 'Нові').",
          "Пошук працює по імені клієнта, телефону, номеру замовлення.",
        ]}
      />
      <div className="flex items-center justify-between gap-3 mb-4 flex-wrap">
        <h1 className="text-xl font-bold">Замовлення</h1>
        <CreateExternalOrderDialog />
      </div>

      <div className="flex flex-wrap gap-2 mb-4">
        <div className="relative w-full sm:flex-1 sm:min-w-[180px]">
          <Search className="absolute left-3 top-2.5 w-4 h-4 text-muted-foreground" />
          <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Пошук..." className="pl-9" />
        </div>
        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger className="flex-1 sm:flex-none sm:w-40 min-w-0"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Всі статуси</SelectItem>
            {Object.entries(statusLabels).map(([k, v]) => (
              <SelectItem key={k} value={k}>{v}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={paymentStatusFilter} onValueChange={setPaymentStatusFilter}>
          <SelectTrigger className="flex-1 sm:flex-none sm:w-44 min-w-0"><SelectValue placeholder="Оплата" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Будь-яка оплата</SelectItem>
            <SelectItem value="paid">💳 Оплачено</SelectItem>
            <SelectItem value="pending">⏳ Очікує оплати</SelectItem>
            <SelectItem value="failed">❌ Скасовано</SelectItem>
            <SelectItem value="cod">📦 Післяплата</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {isLoading ? <p className="text-muted-foreground">Завантаження...</p> : filtered.length === 0 ? (
        <p className="text-muted-foreground">Замовлень не знайдено</p>
      ) : (
        <div className="space-y-3">
          {filtered.map((order: any) => (
            <div key={order.id} className="p-4 bg-card rounded-lg border border-border">
              <div className="flex items-start justify-between gap-3 mb-2">
                <div>
                  <div className="font-medium text-sm">{order.customer_name}</div>
                  <div className="text-xs text-muted-foreground">
                    {order.customer_phone && <span className="mr-3">📞 {order.customer_phone}</span>}
                    {order.customer_email && <span>✉️ {order.customer_email}</span>}
                  </div>
                </div>
                <div className="flex items-center gap-2 flex-wrap justify-end">
                  <span className="font-bold text-primary text-sm">{order.total} ₴</span>
                  {(() => {
                    const pb = getPaymentBadge(order);
                    return pb ? (
                      <span className={`text-xs px-2 py-0.5 rounded-full ${pb.cls}`}>{pb.label}</span>
                    ) : null;
                  })()}
                  <span className={`text-xs px-2 py-0.5 rounded-full ${statusColors[order.status] || ""}`}>
                    {statusLabels[order.status] || order.status}
                  </span>
                </div>
              </div>
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                <div className="text-xs text-muted-foreground">
                  {new Date(order.created_at).toLocaleString("uk-UA")}
                  {" · "}{paymentLabels[order.payment_method] || order.payment_method}
                </div>
                <div className="flex items-center gap-2 self-end sm:self-auto">
                  <Select
                    value={order.status}
                    onValueChange={(v) => {
                      if (v === "cancelled" || v === order.status) {
                        setPendingStatus({ id: order.id, value: v });
                      } else {
                        updateOrder.mutate({ id: order.id, updates: { status: v } });
                      }
                    }}
                    disabled={updateOrder.isPending}
                  >
                    <SelectTrigger className="w-32 h-8 text-xs"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {Object.entries(statusLabels).map(([k, v]) => (
                        <SelectItem key={k} value={k}>{v}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {pendingStatus?.id === order.id && (
                    <div className="flex gap-1 mt-1">
                      <Button size="sm" className="h-6 text-xs px-2" onClick={() => { updateOrder.mutate({ id: order.id, updates: { status: pendingStatus.value } }); setPendingStatus(null); }}>
                        Підтвердити
                      </Button>
                      <Button size="sm" variant="ghost" className="h-6 text-xs px-2" onClick={() => setPendingStatus(null)}>
                        Скасувати
                      </Button>
                    </div>
                  )}
                  <Button variant="ghost" size="icon" onClick={() => openDetail(order)} className="h-9 w-9 shrink-0">
                    <Eye className="w-4 h-4" />
                  </Button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Order detail dialog */}
      <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
        <DialogContent className="max-w-lg bg-card">
          <DialogHeader>
            <DialogTitle>Замовлення #{detailOrder?.id?.slice(0, 8)}</DialogTitle>
          </DialogHeader>
          {detailOrder && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-2 text-sm">
                <div><span className="text-muted-foreground">Клієнт:</span> {detailOrder.customer_name}</div>
                <div><span className="text-muted-foreground">Телефон:</span> {detailOrder.customer_phone || "—"}</div>
                <div><span className="text-muted-foreground">Email:</span> {detailOrder.customer_email || "—"}</div>
                <div><span className="text-muted-foreground">Оплата:</span> {paymentLabels[detailOrder.payment_method] || detailOrder.payment_method}</div>
                <div className="col-span-2"><span className="text-muted-foreground">Адреса:</span> {detailOrder.delivery_address || "—"}</div>
                {detailOrder.preferred_delivery_date && (
                  <div className="col-span-2">
                    <span className="text-muted-foreground">Бажана дата доставки:</span>{" "}
                    <span className="font-medium text-primary">
                      {new Date(detailOrder.preferred_delivery_date).toLocaleDateString("uk-UA", {
                        weekday: "short", day: "numeric", month: "long",
                      })}
                    </span>
                  </div>
                )}
              </div>

              {detailOrder.message && (
                <div className="text-sm"><span className="text-muted-foreground">Коментар:</span> {detailOrder.message}</div>
              )}

              <div>
                <h3 className="text-sm font-semibold mb-2">Товари</h3>
                {orderItems.map((item: any) => (
                  <div key={item.id} className="flex justify-between text-sm py-1 border-b border-border last:border-0">
                    <span>{item.product_name} × {item.quantity}</span>
                    <span>{item.product_price * item.quantity} ₴</span>
                  </div>
                ))}
              </div>

              <div className="border-t border-border pt-2 space-y-1 text-sm">
                <div className="flex justify-between"><span>Підсумок:</span><span>{detailOrder.subtotal} ₴</span></div>
                {detailOrder.discount_amount > 0 && (
                  <div className="flex justify-between text-green-400"><span>Знижка:</span><span>-{detailOrder.discount_amount} ₴</span></div>
                )}
                <div className="flex justify-between font-bold text-base"><span>Разом:</span><span className="text-primary">{detailOrder.total} ₴</span></div>
              </div>

              {detailOrder.status === "shipped" && (
                <div className="space-y-2">
                  <span className="text-sm font-semibold">📦 Номер ТТН (Нова Пошта)</span>
                  <Input
                    defaultValue={detailOrder.tracking_number || ""}
                    placeholder="20400000000000"
                    disabled={updateOrder.isPending}
                    onBlur={(e) => {
                      if (e.target.value !== (detailOrder.tracking_number || "")) {
                        updateOrder.mutate({ id: detailOrder.id, updates: { tracking_number: e.target.value } });
                      }
                    }}
                  />
                </div>
              )}

              <div className="space-y-2">
                <span className="text-sm font-semibold">Нотатки адміна</span>
                <Textarea
                  defaultValue={detailOrder.admin_notes || ""}
                  onBlur={(e) => {
                    if (e.target.value !== (detailOrder.admin_notes || "")) {
                      updateOrder.mutate({ id: detailOrder.id, updates: { admin_notes: e.target.value } });
                    }
                  }}
                  placeholder="Додати нотатку..."
                />
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
};

export default AdminOrders;
