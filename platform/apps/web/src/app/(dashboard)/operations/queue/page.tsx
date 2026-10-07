'use client';
import { useState } from 'react';
import { List, Clock, Pause, Play, X } from 'lucide-react';
import { PageHeader, Card, Badge, Button, EmptyState, useToast, type Tone } from '@/components/ui';

interface QueueItem {
  id: string;
  name: string;
  type: string;
  priority: string;
  addedAt: string;
  estimatedStart: string;
  paused?: boolean;
}

const INITIAL: QueueItem[] = [
  { id: '1', name: 'Broadcast «Акция»', type: 'BROADCAST', priority: 'HIGH', addedAt: '15 мин', estimatedStart: '5 мин' },
  { id: '2', name: 'Публикация по расписанию', type: 'POST', priority: 'NORMAL', addedAt: '30 мин', estimatedStart: '10 мин' },
  { id: '3', name: 'Прогрев, партия #4', type: 'WARMUP', priority: 'LOW', addedAt: '1 ч', estimatedStart: '35 мин' },
];

const PRIORITY: Record<string, { tone: Tone; label: string }> = {
  HIGH: { tone: 'danger', label: 'Высокий' },
  NORMAL: { tone: 'accent', label: 'Обычный' },
  LOW: { tone: 'neutral', label: 'Низкий' },
};

export default function QueuePage() {
  const toast = useToast();
  const [items, setItems] = useState(INITIAL);

  const togglePause = (id: string) => {
    setItems((p) => p.map((i) => (i.id === id ? { ...i, paused: !i.paused } : i)));
    const it = items.find((i) => i.id === id);
    toast(it?.paused ? 'Операция возобновлена' : 'Операция на паузе', 'info');
  };
  const remove = (id: string) => {
    setItems((p) => p.filter((i) => i.id !== id));
    toast('Удалено из очереди', 'success');
  };

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader title="Очередь" subtitle="Операции, ожидающие выполнения" />

      {items.length === 0 ? (
        <EmptyState icon={<List size={26} />} title="Очередь пуста" description="Новые операции появятся здесь после создания." />
      ) : (
        <div className="space-y-2.5">
          {items.map((item, idx) => (
            <Card key={item.id} className="flex items-center gap-3 p-3.5">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-surface-2 text-xs font-bold text-fg-muted">
                {idx + 1}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <p className="truncate text-sm font-semibold text-fg">{item.name}</p>
                  {item.paused && <Badge tone="warning">Пауза</Badge>}
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-fg-hint">
                  <Badge tone="neutral">{item.type}</Badge>
                  <Badge tone={PRIORITY[item.priority].tone}>{PRIORITY[item.priority].label}</Badge>
                  <span className="flex items-center gap-1">
                    <Clock size={11} /> старт через {item.estimatedStart}
                  </span>
                </div>
              </div>
              <div className="flex gap-1.5">
                <Button size="icon" variant="ghost" onClick={() => togglePause(item.id)} title={item.paused ? 'Возобновить' : 'Пауза'}>
                  {item.paused ? <Play size={15} /> : <Pause size={15} />}
                </Button>
                <Button size="icon" variant="ghost" onClick={() => remove(item.id)} title="Удалить">
                  <X size={15} />
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
