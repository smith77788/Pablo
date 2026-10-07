'use client';
import { useState } from 'react';
import { UserCheck, Plus, TrendingUp, TrendingDown } from 'lucide-react';
import { PageHeader, Button, Card, Avatar, Badge, Sheet, Input, Label, useToast } from '@/components/ui';

interface Competitor {
  id: string;
  name: string;
  handle: string;
  keywordsOverlap: number;
  avgPosition: number;
  trend: number;
}

const INITIAL: Competitor[] = [
  { id: '1', name: 'TeleRaptor', handle: '@teleraptor', keywordsOverlap: 64, avgPosition: 3.1, trend: -2 },
  { id: '2', name: 'Telegram Expert', handle: '@tg_expert', keywordsOverlap: 41, avgPosition: 5.4, trend: 1 },
  { id: '3', name: 'ChannelKit', handle: '@channelkit', keywordsOverlap: 28, avgPosition: 7.8, trend: 3 },
];

export default function CompetitorsPage() {
  const toast = useToast();
  const [list, setList] = useState(INITIAL);
  const [adding, setAdding] = useState(false);
  const [handle, setHandle] = useState('');

  function add() {
    if (!handle) return;
    const clean = handle.startsWith('@') ? handle : `@${handle}`;
    setList((p) => [
      { id: String(Date.now()), name: clean.replace('@', ''), handle: clean, keywordsOverlap: 0, avgPosition: 0, trend: 0 },
      ...p,
    ]);
    setHandle('');
    setAdding(false);
    toast('Конкурент добавлен в мониторинг', 'success');
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader
        title="Конкуренты"
        subtitle="Мониторинг пересечений по ключевым словам"
        action={
          <Button onClick={() => setAdding(true)}>
            <Plus size={16} /> Добавить
          </Button>
        }
      />

      <div className="grid gap-3 sm:grid-cols-2">
        {list.map((c) => (
          <Card key={c.id} className="p-4">
            <div className="flex items-center gap-3">
              <Avatar name={c.name} size={44} />
              <div className="min-w-0 flex-1">
                <p className="truncate font-semibold text-fg">{c.name}</p>
                <p className="truncate text-xs text-fg-hint">{c.handle}</p>
              </div>
              <Badge tone={c.trend <= 0 ? 'success' : 'danger'}>
                {c.trend <= 0 ? <TrendingUp size={11} /> : <TrendingDown size={11} />}
                {c.trend === 0 ? '—' : Math.abs(c.trend)}
              </Badge>
            </div>
            <div className="mt-4 grid grid-cols-2 gap-2.5">
              <div className="rounded-xl bg-surface-2 p-3 text-center">
                <p className="text-xl font-bold tabular-nums text-fg">{c.keywordsOverlap}</p>
                <p className="text-2xs text-fg-hint">общих слов</p>
              </div>
              <div className="rounded-xl bg-surface-2 p-3 text-center">
                <p className="text-xl font-bold tabular-nums text-fg">{c.avgPosition || '—'}</p>
                <p className="text-2xs text-fg-hint">ср. позиция</p>
              </div>
            </div>
          </Card>
        ))}
      </div>

      <Sheet
        open={adding}
        onClose={() => setAdding(false)}
        title="Добавить конкурента"
        description="Мы начнём отслеживать пересечение по вашим ключевым словам"
        footer={
          <>
            <Button variant="secondary" onClick={() => setAdding(false)}>
              Отмена
            </Button>
            <Button onClick={add} disabled={!handle}>
              Добавить
            </Button>
          </>
        }
      >
        <div className="py-1">
          <Label>Username или ссылка</Label>
          <Input value={handle} onChange={(e) => setHandle(e.target.value)} placeholder="@competitor" />
        </div>
      </Sheet>
    </div>
  );
}
