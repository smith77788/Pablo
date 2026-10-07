'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Wifi, Plus } from 'lucide-react';
import { authApi } from '@/lib/api';
import {
  PageHeader,
  Button,
  Card,
  StatusBadge,
  Badge,
  Sheet,
  Input,
  Select,
  Label,
  ScoreBar,
  SkeletonList,
  EmptyState,
  useToast,
} from '@/components/ui';

interface Proxy {
  id: string;
  host: string;
  port: number;
  type: string;
  region: string;
  status: string;
  latencyMs: number;
  healthScore: number;
  assignedAccountsCount: number;
}

const MOCK: Proxy[] = [
  { id: '1', host: '185.12.45.67', port: 3128, type: 'HTTP', region: 'RU', status: 'ACTIVE', latencyMs: 45, healthScore: 98, assignedAccountsCount: 4 },
  { id: '2', host: '91.234.56.78', port: 1080, type: 'SOCKS5', region: 'DE', status: 'ACTIVE', latencyMs: 112, healthScore: 87, assignedAccountsCount: 2 },
  { id: '3', host: '104.21.34.56', port: 8080, type: 'HTTP', region: 'US', status: 'WARNING', latencyMs: 380, healthScore: 52, assignedAccountsCount: 1 },
  { id: '4', host: '172.67.89.12', port: 3128, type: 'HTTP', region: 'NL', status: 'DISCONNECTED', latencyMs: 0, healthScore: 0, assignedAccountsCount: 0 },
  { id: '5', host: '45.76.12.34', port: 1080, type: 'SOCKS5', region: 'UA', status: 'ACTIVE', latencyMs: 78, healthScore: 94, assignedAccountsCount: 3 },
];

const EMPTY = { host: '', port: '', type: 'HTTP', username: '', password: '', region: '' };

function latencyTone(ms: number) {
  if (ms === 0) return 'text-fg-hint';
  if (ms < 100) return 'text-success';
  if (ms < 300) return 'text-warning';
  return 'text-danger';
}

export default function ProxiesPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY);

  const { data, isLoading, isError } = useQuery<Proxy[]>({
    queryKey: ['proxies'],
    queryFn: () => authApi.get('/proxies').then((r: any) => r.data ?? r),
    retry: false,
  });
  const proxies = isError ? MOCK : data ?? [];

  const addProxy = useMutation({
    mutationFn: () =>
      authApi.post('/proxies', { ...form, port: Number(form.port) }),
    onSuccess: () => {
      setForm(EMPTY);
      setShowForm(false);
      toast('Прокси добавлен', 'success');
      qc.invalidateQueries({ queryKey: ['proxies'] });
    },
    onError: () => toast('Не удалось добавить прокси', 'error'),
  });

  const set = (k: keyof typeof EMPTY) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader
        title="Прокси"
        subtitle="Прокси-серверы и их здоровье"
        action={
          <Button onClick={() => setShowForm(true)}>
            <Plus size={16} /> Добавить
          </Button>
        }
      />

      {isLoading && !isError ? (
        <SkeletonList rows={5} />
      ) : proxies.length === 0 ? (
        <EmptyState icon={<Wifi size={26} />} title="Нет прокси" description="Добавьте первый прокси-сервер." />
      ) : (
        <div className="space-y-2.5">
          {proxies.map((p) => (
            <Card key={p.id} className="flex items-center gap-3 p-3.5">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <p className="truncate font-mono text-sm font-semibold text-fg">
                    {p.host}:{p.port}
                  </p>
                  <StatusBadge status={p.status} />
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-fg-hint">
                  <Badge tone="neutral">{p.type}</Badge>
                  <span>{p.region}</span>
                  <span className={latencyTone(p.latencyMs)}>
                    {p.latencyMs > 0 ? `${p.latencyMs} мс` : '—'}
                  </span>
                  <span>· {p.assignedAccountsCount} акк.</span>
                </div>
              </div>
              <ScoreBar value={p.healthScore} label width="w-14" />
            </Card>
          ))}
        </div>
      )}

      <Sheet
        open={showForm}
        onClose={() => setShowForm(false)}
        title="Новый прокси"
        footer={
          <>
            <Button variant="secondary" onClick={() => setShowForm(false)}>
              Отмена
            </Button>
            <Button onClick={() => addProxy.mutate()} disabled={!form.host || !form.port} loading={addProxy.isPending}>
              Добавить
            </Button>
          </>
        }
      >
        <div className="grid grid-cols-2 gap-3 py-1">
          <div className="col-span-2">
            <Label>Host</Label>
            <Input value={form.host} onChange={set('host')} placeholder="185.12.45.67" />
          </div>
          <div>
            <Label>Port</Label>
            <Input type="number" value={form.port} onChange={set('port')} placeholder="3128" />
          </div>
          <div>
            <Label>Тип</Label>
            <Select value={form.type} onChange={set('type')}>
              <option value="HTTP">HTTP</option>
              <option value="SOCKS5">SOCKS5</option>
              <option value="HTTPS">HTTPS</option>
            </Select>
          </div>
          <div>
            <Label>Логин</Label>
            <Input value={form.username} onChange={set('username')} placeholder="user" />
          </div>
          <div>
            <Label>Пароль</Label>
            <Input type="password" value={form.password} onChange={set('password')} placeholder="••••••" />
          </div>
          <div className="col-span-2">
            <Label>Регион</Label>
            <Input value={form.region} onChange={set('region')} placeholder="RU" />
          </div>
        </div>
      </Sheet>
    </div>
  );
}
