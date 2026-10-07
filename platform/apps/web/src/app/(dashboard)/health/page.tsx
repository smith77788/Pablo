'use client';
import { Activity, Server, Database, Cpu, Wifi, Bot, RefreshCw } from 'lucide-react';
import { PageHeader, Card, Badge, ScoreRing, Button, type Tone } from '@/components/ui';

const SERVICES: { name: string; detail: string; status: string; tone: Tone; Icon: typeof Server }[] = [
  { name: 'API Gateway', detail: 'p95 42 мс · 0 ошибок', status: 'Онлайн', tone: 'success', Icon: Server },
  { name: 'Worker (операции)', detail: '3 активных воркера', status: 'Онлайн', tone: 'success', Icon: Cpu },
  { name: 'База данных', detail: 'репликация · 18% нагрузка', status: 'Онлайн', tone: 'success', Icon: Database },
  { name: 'Telegram Relay', detail: 'очередь 12 сообщений', status: 'Онлайн', tone: 'success', Icon: Bot },
  { name: 'Пул прокси', detail: '1 из 14 деградирует', status: 'Внимание', tone: 'warning', Icon: Wifi },
];

const RINGS = [
  { label: 'Аккаунты', value: 86 },
  { label: 'Прокси', value: 78 },
  { label: 'Каналы', value: 95 },
  { label: 'Боты', value: 92 },
];

export default function HealthPage() {
  const overall = Math.round(RINGS.reduce((a, r) => a + r.value, 0) / RINGS.length);

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader
        title="Здоровье системы"
        subtitle="Состояние компонентов и инфраструктуры в реальном времени"
        action={
          <Button variant="secondary" size="sm">
            <RefreshCw size={14} /> Обновить
          </Button>
        }
      />

      {/* Overall hero */}
      <Card className="flex items-center gap-5 p-5">
        <ScoreRing value={overall} size={84} stroke={8}>
          <span className="text-lg font-bold">{overall}</span>
        </ScoreRing>
        <div>
          <div className="flex items-center gap-2">
            <Activity size={16} className="text-success" />
            <p className="font-semibold text-fg">Инфраструктура в норме</p>
          </div>
          <p className="mt-1 text-sm text-fg-muted">
            Все критичные сервисы онлайн. 1 прокси требует внимания.
          </p>
        </div>
      </Card>

      {/* Category rings */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {RINGS.map((r) => (
          <Card key={r.label} className="flex flex-col items-center gap-2 p-4">
            <ScoreRing value={r.value} />
            <span className="text-xs font-medium text-fg-muted">{r.label}</span>
          </Card>
        ))}
      </div>

      {/* Services */}
      <Card>
        <div className="flex items-center gap-2 border-b border-line px-4 py-3.5">
          <Server size={16} className="text-fg-muted" />
          <h2 className="text-sm font-semibold text-fg">Сервисы</h2>
        </div>
        <div className="divide-y divide-line">
          {SERVICES.map((s) => (
            <div key={s.name} className="flex items-center gap-3 px-4 py-3.5">
              <span
                className="flex h-9 w-9 items-center justify-center rounded-xl"
                style={{ background: `var(--${s.tone}-weak)`, color: `var(--${s.tone})` }}
              >
                <s.Icon size={17} />
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-semibold text-fg">{s.name}</p>
                <p className="truncate text-xs text-fg-hint">{s.detail}</p>
              </div>
              <Badge tone={s.tone} dot>
                {s.status}
              </Badge>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
