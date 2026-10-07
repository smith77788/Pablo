'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Users, Server, Check } from 'lucide-react';
import {
  PageHeader,
  Button,
  Card,
  CardHeader,
  Badge,
  Input,
  Select,
  Textarea,
  Label,
  useToast,
  cn,
} from '@/components/ui';

const OP_TYPES = [
  { value: 'FOLLOW', label: 'Mass Follow' },
  { value: 'BROADCAST', label: 'Рассылка' },
  { value: 'WARMUP', label: 'Прогрев аккаунтов' },
  { value: 'SCRAPE', label: 'Сбор данных' },
  { value: 'POST', label: 'Публикация в канал' },
  { value: 'HEALTH', label: 'Проверка здоровья' },
  { value: 'CUSTOM', label: 'Другое' },
];

const ACCOUNTS = [
  { id: 'a1', name: '+7 912 345-67-80', status: 'ACTIVE' },
  { id: 'a2', name: '+7 987 654-32-10', status: 'ACTIVE' },
  { id: 'a3', name: '@account_promo', status: 'LIMITED' },
];
const CLUSTERS = [
  { id: 'c1', name: 'Cluster A' },
  { id: 'c2', name: 'Cluster B' },
  { id: 'c3', name: 'Cluster C' },
];

function SelectRow({
  label,
  checked,
  onToggle,
  right,
}: {
  label: string;
  checked: boolean;
  onToggle: () => void;
  right?: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className={cn(
        'flex w-full items-center gap-3 rounded-xl border px-3.5 py-3 text-left transition-colors',
        checked ? 'border-accent/40 bg-accent-weak' : 'border-line bg-surface-2 hover:border-line-strong',
      )}
    >
      <span
        className={cn(
          'flex h-5 w-5 items-center justify-center rounded-md border',
          checked ? 'border-accent bg-accent text-accent-fg' : 'border-line-strong',
        )}
      >
        {checked && <Check size={13} />}
      </span>
      <span className="flex-1 text-sm font-medium text-fg">{label}</span>
      {right}
    </button>
  );
}

export default function NewOperationPage() {
  const router = useRouter();
  const toast = useToast();
  const [form, setForm] = useState({ name: '', type: '', description: '' });
  const [accounts, setAccounts] = useState<Set<string>>(new Set());
  const [clusters, setClusters] = useState<Set<string>>(new Set());

  const toggle = (set: React.Dispatch<React.SetStateAction<Set<string>>>) => (id: string) =>
    set((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const targetCount = accounts.size + clusters.size;

  function createDraft() {
    toast('Черновик операции сохранён', 'success');
    setTimeout(() => router.push('/operations/queue'), 600);
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5 p-4 sm:p-6">
      <PageHeader title="Создать операцию" subtitle="Настройте параметры и выберите цели" />

      <Card className="space-y-4 p-5">
        <div>
          <Label>Название операции</Label>
          <Input
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            placeholder="Mass follow Cluster A"
          />
        </div>
        <div>
          <Label>Тип операции</Label>
          <Select value={form.type} onChange={(e) => setForm((f) => ({ ...f, type: e.target.value }))}>
            <option value="">Выберите тип…</option>
            {OP_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Описание</Label>
          <Textarea
            value={form.description}
            onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            placeholder="Что должна сделать операция…"
          />
        </div>
      </Card>

      <Card>
        <CardHeader title="Аккаунты" subtitle="Выберите аккаунты-исполнители" icon={<Users size={16} />} />
        <div className="space-y-2 p-4">
          {ACCOUNTS.map((a) => (
            <SelectRow
              key={a.id}
              label={a.name}
              checked={accounts.has(a.id)}
              onToggle={() => toggle(setAccounts)(a.id)}
              right={<Badge tone={a.status === 'ACTIVE' ? 'success' : 'danger'}>{a.status}</Badge>}
            />
          ))}
        </div>
      </Card>

      <Card>
        <CardHeader title="Кластеры" subtitle="Или выберите кластеры целиком" icon={<Server size={16} />} />
        <div className="space-y-2 p-4">
          {CLUSTERS.map((c) => (
            <SelectRow
              key={c.id}
              label={c.name}
              checked={clusters.has(c.id)}
              onToggle={() => toggle(setClusters)(c.id)}
            />
          ))}
        </div>
      </Card>

      <div className="sticky bottom-[calc(var(--tabbar-h)+env(safe-area-inset-bottom,0px))] z-10 flex items-center gap-3 rounded-2xl border border-line bg-surface/90 p-3 shadow-md backdrop-blur-md md:static md:bg-transparent md:p-0 md:shadow-none md:backdrop-blur-none">
        <span className="text-xs text-fg-muted">
          Целей выбрано: <b className="text-fg">{targetCount}</b>
        </span>
        <div className="ml-auto flex gap-2.5">
          <Button variant="secondary" onClick={() => router.back()}>
            Отмена
          </Button>
          <Button onClick={createDraft} disabled={!form.name || !form.type}>
            Создать черновик
          </Button>
        </div>
      </div>
    </div>
  );
}
