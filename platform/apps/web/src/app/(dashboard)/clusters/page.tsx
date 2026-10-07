'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Network, Plus } from 'lucide-react';
import { authApi } from '@/lib/api';
import {
  PageHeader,
  Button,
  Card,
  StatusBadge,
  Sheet,
  Input,
  Label,
  ScoreBar,
  SkeletonCards,
  EmptyState,
  useToast,
} from '@/components/ui';

interface Cluster {
  id: string;
  name: string;
  description: string;
  accountsCount: number;
  assetsCount: number;
  healthScore: number;
  status: string;
}

const MOCK: Cluster[] = [
  { id: '1', name: 'Cluster A', description: 'Основной кластер продуктовых аккаунтов', accountsCount: 12, assetsCount: 28, healthScore: 95, status: 'ACTIVE' },
  { id: '2', name: 'Cluster B', description: 'Вторичный кластер для рекламных операций', accountsCount: 8, assetsCount: 15, healthScore: 72, status: 'WARNING' },
  { id: '3', name: 'Cluster C', description: 'Тестовый кластер', accountsCount: 3, assetsCount: 5, healthScore: 60, status: 'WARNING' },
  { id: '4', name: 'Cluster D', description: 'Резервный кластер', accountsCount: 0, assetsCount: 0, healthScore: 100, status: 'ACTIVE' },
];

export default function ClustersPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: '', description: '' });

  const { data, isLoading, isError } = useQuery<Cluster[]>({
    queryKey: ['clusters'],
    queryFn: () => authApi.get('/clusters').then((r: any) => r.data ?? r),
    retry: false,
  });
  const clusters = isError ? MOCK : data ?? [];

  const addCluster = useMutation({
    mutationFn: () => authApi.post('/clusters', form),
    onSuccess: () => {
      setForm({ name: '', description: '' });
      setShowForm(false);
      toast('Кластер создан', 'success');
      qc.invalidateQueries({ queryKey: ['clusters'] });
    },
    onError: () => toast('Не удалось создать кластер', 'error'),
  });

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader
        title="Кластеры"
        subtitle="Группировка аккаунтов и активов"
        action={
          <Button onClick={() => setShowForm(true)}>
            <Plus size={16} /> Создать
          </Button>
        }
      />

      {isLoading && !isError ? (
        <SkeletonCards count={4} className="sm:grid-cols-2" />
      ) : clusters.length === 0 ? (
        <EmptyState icon={<Network size={26} />} title="Нет кластеров" description="Создайте первый кластер для группировки ресурсов." />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {clusters.map((c) => (
            <Card key={c.id} className="space-y-4 p-5">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent-weak text-accent">
                      <Network size={15} />
                    </span>
                    <h3 className="truncate font-semibold text-fg">{c.name}</h3>
                  </div>
                  <p className="mt-1.5 text-xs text-fg-muted">{c.description}</p>
                </div>
                <StatusBadge status={c.status} />
              </div>
              <div className="grid grid-cols-2 gap-2.5">
                <div className="rounded-xl bg-surface-2 p-3 text-center">
                  <p className="text-2xl font-bold tabular-nums text-fg">{c.accountsCount}</p>
                  <p className="mt-0.5 text-2xs text-fg-hint">Аккаунтов</p>
                </div>
                <div className="rounded-xl bg-surface-2 p-3 text-center">
                  <p className="text-2xl font-bold tabular-nums text-fg">{c.assetsCount}</p>
                  <p className="mt-0.5 text-2xs text-fg-hint">Активов</p>
                </div>
              </div>
              <div>
                <div className="mb-1 flex items-center justify-between text-xs">
                  <span className="text-fg-hint">Health Score</span>
                  <span className="font-semibold text-fg">{c.healthScore}%</span>
                </div>
                <ScoreBar value={c.healthScore} width="w-full" />
              </div>
            </Card>
          ))}
        </div>
      )}

      <Sheet
        open={showForm}
        onClose={() => setShowForm(false)}
        title="Новый кластер"
        footer={
          <>
            <Button variant="secondary" onClick={() => setShowForm(false)}>
              Отмена
            </Button>
            <Button onClick={() => addCluster.mutate()} disabled={!form.name} loading={addCluster.isPending}>
              Создать
            </Button>
          </>
        }
      >
        <div className="space-y-3 py-1">
          <div>
            <Label>Название</Label>
            <Input value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} placeholder="Cluster E" />
          </div>
          <div>
            <Label>Описание</Label>
            <Input
              value={form.description}
              onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
              placeholder="Для чего этот кластер"
            />
          </div>
        </div>
      </Sheet>
    </div>
  );
}
