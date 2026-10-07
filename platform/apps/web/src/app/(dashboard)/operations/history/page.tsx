'use client';
import { useState } from 'react';
import { CheckCircle, XCircle, Clock } from 'lucide-react';
import { PageHeader, Card, Badge, StatusBadge, Segmented } from '@/components/ui';

interface HistoryItem {
  id: string;
  name: string;
  type: string;
  status: 'COMPLETED' | 'FAILED' | 'CANCELLED';
  duration: string;
  completedAt: string;
  result: string;
}

const HISTORY: HistoryItem[] = [
  { id: '1', name: 'Проверка прокси', type: 'HEALTH', status: 'COMPLETED', duration: '2 мин', completedAt: '4 ч', result: '5/5 OK' },
  { id: '2', name: 'Прогрев аккаунтов #3', type: 'WARMUP', status: 'COMPLETED', duration: '45 мин', completedAt: '5 ч', result: '8 аккаунтов' },
  { id: '3', name: 'Сбор конкурентов', type: 'SCRAPE', status: 'FAILED', duration: '3 мин', completedAt: '3 ч', result: 'Ошибка: rate limit' },
  { id: '4', name: 'Broadcast «Апрель»', type: 'BROADCAST', status: 'COMPLETED', duration: '12 мин', completedAt: '1 д', result: '1 240 доставлено' },
  { id: '5', name: 'Mass follow v2', type: 'FOLLOW', status: 'CANCELLED', duration: '—', completedAt: '2 д', result: 'Отменено' },
];

const ICON = {
  COMPLETED: <CheckCircle size={15} className="text-success" />,
  FAILED: <XCircle size={15} className="text-danger" />,
  CANCELLED: <Clock size={15} className="text-fg-hint" />,
};

type Filter = 'ALL' | 'COMPLETED' | 'FAILED' | 'CANCELLED';

export default function HistoryPage() {
  const [filter, setFilter] = useState<Filter>('ALL');
  const rows = HISTORY.filter((h) => filter === 'ALL' || h.status === filter);

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader title="История" subtitle="Завершённые, провальные и отменённые операции" />

      <Segmented
        value={filter}
        onChange={setFilter}
        options={[
          { value: 'ALL', label: 'Все', count: HISTORY.length },
          { value: 'COMPLETED', label: 'Успешно', count: HISTORY.filter((h) => h.status === 'COMPLETED').length },
          { value: 'FAILED', label: 'Ошибки', count: HISTORY.filter((h) => h.status === 'FAILED').length },
          { value: 'CANCELLED', label: 'Отменено', count: HISTORY.filter((h) => h.status === 'CANCELLED').length },
        ]}
      />

      <div className="space-y-2.5">
        {rows.map((op) => (
          <Card key={op.id} className="flex items-center gap-3 p-3.5">
            <span className="shrink-0">{ICON[op.status]}</span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <p className="truncate text-sm font-semibold text-fg">{op.name}</p>
                <Badge tone="neutral">{op.type}</Badge>
              </div>
              <p className="mt-0.5 truncate text-xs text-fg-hint">
                {op.result} · {op.duration} · {op.completedAt} назад
              </p>
            </div>
            <StatusBadge status={op.status} />
          </Card>
        ))}
      </div>
    </div>
  );
}
