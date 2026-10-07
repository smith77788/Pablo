'use client';
import { useState } from 'react';
import { BookOpen, User, Cog, Shield, LogIn } from 'lucide-react';
import { PageHeader, Card, Badge, Segmented, type Tone } from '@/components/ui';

interface AuditEntry {
  id: string;
  action: 'CREATE' | 'UPDATE' | 'DELETE' | 'EXECUTE' | 'LOGIN';
  entity: string;
  detail: string;
  user: string;
  ip: string;
  at: string;
}

const AUDIT: AuditEntry[] = [
  { id: '1', action: 'EXECUTE', entity: 'Operation', detail: 'Запущена «Проверка прокси»', user: 'admin', ip: '192.168.1.1', at: '4 ч' },
  { id: '2', action: 'CREATE', entity: 'Proxy', detail: 'Добавлен прокси 45.76.12.34:1080', user: 'admin', ip: '192.168.1.1', at: '5 ч' },
  { id: '3', action: 'UPDATE', entity: 'Account', detail: 'Статус +7912… → WARNING', user: 'system', ip: '—', at: '6 ч' },
  { id: '4', action: 'DELETE', entity: 'Cluster', detail: 'Удалён кластер «Test»', user: 'admin', ip: '192.168.1.1', at: '1 д' },
  { id: '5', action: 'LOGIN', entity: 'Auth', detail: 'Успешный вход', user: 'admin', ip: '192.168.1.1', at: '1 д' },
  { id: '6', action: 'CREATE', entity: 'Account', detail: 'Добавлен аккаунт +7987…', user: 'admin', ip: '192.168.1.1', at: '2 д' },
];

const ACTION_TONE: Record<AuditEntry['action'], { tone: Tone; label: string }> = {
  CREATE: { tone: 'success', label: 'Создание' },
  UPDATE: { tone: 'accent', label: 'Изменение' },
  DELETE: { tone: 'danger', label: 'Удаление' },
  EXECUTE: { tone: 'violet', label: 'Запуск' },
  LOGIN: { tone: 'neutral', label: 'Вход' },
};

const ENTITY_ICON: Record<string, React.ReactNode> = {
  Operation: <Cog size={14} />,
  Account: <Shield size={14} />,
  Auth: <LogIn size={14} />,
  Proxy: <Cog size={14} />,
  Cluster: <Cog size={14} />,
};

type Filter = 'ALL' | AuditEntry['action'];

export default function AuditPage() {
  const [filter, setFilter] = useState<Filter>('ALL');
  const rows = AUDIT.filter((a) => filter === 'ALL' || a.action === filter);

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader title="Журнал аудита" subtitle="Все действия в системе с полной трассировкой" />

      <Segmented
        value={filter}
        onChange={setFilter}
        options={[
          { value: 'ALL', label: 'Все' },
          { value: 'EXECUTE', label: 'Запуски' },
          { value: 'CREATE', label: 'Создание' },
          { value: 'UPDATE', label: 'Изменения' },
          { value: 'DELETE', label: 'Удаления' },
        ]}
      />

      <Card>
        <div className="flex items-center gap-2 border-b border-line px-4 py-3.5">
          <BookOpen size={16} className="text-fg-muted" />
          <h2 className="text-sm font-semibold text-fg">События</h2>
          <Badge tone="neutral" className="ml-auto">
            {rows.length}
          </Badge>
        </div>
        <div className="divide-y divide-line">
          {rows.map((e) => (
            <div key={e.id} className="flex items-start gap-3 px-4 py-3">
              <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-fg-muted">
                {ENTITY_ICON[e.entity] ?? <Cog size={14} />}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={ACTION_TONE[e.action].tone}>{ACTION_TONE[e.action].label}</Badge>
                  <span className="text-sm text-fg">{e.detail}</span>
                </div>
                <p className="mt-1 flex items-center gap-2 text-2xs text-fg-hint">
                  <span className="flex items-center gap-1">
                    <User size={10} /> {e.user}
                  </span>
                  <span className="font-mono">{e.ip}</span>
                  <span>· {e.at} назад</span>
                </p>
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
