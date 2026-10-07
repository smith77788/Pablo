'use client';
import { useState } from 'react';
import { Bell, AlertTriangle, TrendingDown, ShieldAlert, Check } from 'lucide-react';
import { PageHeader, Card, Badge, Button, Segmented, EmptyState, useToast, type Tone } from '@/components/ui';

interface Alert {
  id: string;
  severity: 'high' | 'medium' | 'low';
  title: string;
  detail: string;
  time: string;
  Icon: typeof AlertTriangle;
  resolved?: boolean;
}

const INITIAL: Alert[] = [
  { id: '1', severity: 'high', title: 'Резкое падение позиции', detail: '«telegram рассылка» упал с #7 на #12', time: '12 мин', Icon: TrendingDown },
  { id: '2', severity: 'high', title: 'Ограничение аккаунта', detail: '@account_promo получил flood-wait 3600s', time: '40 мин', Icon: ShieldAlert },
  { id: '3', severity: 'medium', title: 'Конкурент обошёл вас', detail: 'TeleRaptor поднялся выше по 4 словам', time: '2 ч', Icon: AlertTriangle },
  { id: '4', severity: 'low', title: 'Новое ключевое слово в топ-10', detail: '«telegram automation» вошёл в топ-3', time: '5 ч', Icon: Bell },
];

const SEV: Record<Alert['severity'], { tone: Tone; label: string }> = {
  high: { tone: 'danger', label: 'Важно' },
  medium: { tone: 'warning', label: 'Средне' },
  low: { tone: 'accent', label: 'Инфо' },
};

type Filter = 'ALL' | 'high' | 'medium' | 'low';

export default function AlertsPage() {
  const toast = useToast();
  const [list, setList] = useState(INITIAL);
  const [filter, setFilter] = useState<Filter>('ALL');

  const rows = list.filter((a) => !a.resolved && (filter === 'ALL' || a.severity === filter));

  const resolve = (id: string) => {
    setList((p) => p.map((a) => (a.id === id ? { ...a, resolved: true } : a)));
    toast('Алерт отмечен как обработанный', 'success');
  };

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader title="Алерты" subtitle="Уведомления о рисках и изменениях" />

      <Segmented
        value={filter}
        onChange={setFilter}
        options={[
          { value: 'ALL', label: 'Все', count: list.filter((a) => !a.resolved).length },
          { value: 'high', label: 'Важные' },
          { value: 'medium', label: 'Средние' },
          { value: 'low', label: 'Инфо' },
        ]}
      />

      {rows.length === 0 ? (
        <EmptyState icon={<Check size={26} />} title="Всё спокойно" description="Нет активных алертов по выбранному фильтру." />
      ) : (
        <div className="space-y-2.5">
          {rows.map((a) => (
            <Card key={a.id} className="flex items-start gap-3 p-3.5">
              <span
                className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl"
                style={{ background: `var(--${SEV[a.severity].tone}-weak)`, color: `var(--${SEV[a.severity].tone})` }}
              >
                <a.Icon size={17} />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <p className="text-sm font-semibold text-fg">{a.title}</p>
                  <Badge tone={SEV[a.severity].tone}>{SEV[a.severity].label}</Badge>
                </div>
                <p className="mt-0.5 text-xs text-fg-muted">{a.detail}</p>
                <p className="mt-0.5 text-2xs text-fg-hint">{a.time} назад</p>
              </div>
              <Button size="sm" variant="ghost" onClick={() => resolve(a.id)}>
                <Check size={14} /> Готово
              </Button>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
