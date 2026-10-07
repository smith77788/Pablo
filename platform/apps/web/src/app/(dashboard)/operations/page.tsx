'use client';
import Link from 'next/link';
import { Play, Clock, CheckCircle, XCircle, PlusCircle, ChevronRight } from 'lucide-react';
import { PageHeader, Button, Card, Badge, StatusBadge } from '@/components/ui';

const OPS = [
  { id: '1', name: 'Mass follow Cluster A', type: 'FOLLOW', status: 'RUNNING', created: '10 мин', estimated: '25 мин', progress: 62 },
  { id: '2', name: 'Broadcast «Акция»', type: 'BROADCAST', status: 'QUEUED', created: '15 мин', estimated: '10 мин', progress: 0 },
  { id: '5', name: 'Публикация по расписанию', type: 'POST', status: 'QUEUED', created: '30 мин', estimated: '5 мин', progress: 0 },
  { id: '3', name: 'Прогрев аккаунтов #3', type: 'WARMUP', status: 'COMPLETED', created: '2 ч', estimated: '—', progress: 100 },
  { id: '6', name: 'Проверка прокси', type: 'HEALTH', status: 'COMPLETED', created: '4 ч', estimated: '—', progress: 100 },
  { id: '4', name: 'Сбор конкурентов', type: 'SCRAPE', status: 'FAILED', created: '3 ч', estimated: '—', progress: 34 },
];

const METRICS = [
  { label: 'Активных', value: 1, Icon: Play, tone: 'accent' as const },
  { label: 'В очереди', value: 2, Icon: Clock, tone: 'warning' as const },
  { label: 'Завершено', value: 2, Icon: CheckCircle, tone: 'success' as const },
  { label: 'С ошибкой', value: 1, Icon: XCircle, tone: 'danger' as const },
];

export default function OperationsPage() {
  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader
        title="Операции"
        subtitle="Мониторинг и управление массовыми операциями"
        action={
          <Link href="/operations/new">
            <Button>
              <PlusCircle size={16} /> Создать
            </Button>
          </Link>
        }
      />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {METRICS.map((m) => (
          <Card key={m.label} className="p-4">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-fg-muted">{m.label}</span>
              <span
                className="flex h-8 w-8 items-center justify-center rounded-xl"
                style={{ background: `var(--${m.tone}-weak)`, color: `var(--${m.tone})` }}
              >
                <m.Icon size={15} />
              </span>
            </div>
            <p className="mt-2 text-3xl font-bold tabular-nums" style={{ color: `var(--${m.tone})` }}>
              {m.value}
            </p>
          </Card>
        ))}
      </div>

      <Card>
        <div className="flex items-center gap-2 border-b border-line px-4 py-3.5">
          <h2 className="text-sm font-semibold text-fg">Последние операции</h2>
          <Link href="/operations/history" className="ml-auto flex items-center gap-0.5 text-xs font-medium text-link hover:underline">
            История <ChevronRight size={13} />
          </Link>
        </div>
        <div className="divide-y divide-line">
          {OPS.map((op) => (
            <div key={op.id} className="px-4 py-3.5">
              <div className="flex items-center gap-3">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-semibold text-fg">{op.name}</p>
                  <p className="mt-0.5 text-xs text-fg-hint">
                    {op.created} назад{op.estimated !== '—' && ` · осталось ${op.estimated}`}
                  </p>
                </div>
                <Badge tone="neutral">{op.type}</Badge>
                <StatusBadge status={op.status} />
              </div>
              {op.status === 'RUNNING' && (
                <div className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-surface-2">
                  <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${op.progress}%` }} />
                </div>
              )}
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
