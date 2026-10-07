'use client';
import { useRouter } from 'next/navigation';
import { FileText, Plus, Copy } from 'lucide-react';
import { PageHeader, Button, Card, Badge, useToast } from '@/components/ui';

const TEMPLATES = [
  { id: '1', name: 'Ежедневный прогрев', type: 'WARMUP', description: 'Прогрев аккаунтов в кластере каждый день', usedCount: 24, lastUsed: '3 д' },
  { id: '2', name: 'Промо-рассылка', type: 'BROADCAST', description: 'Рассылка промо-сообщений по базе', usedCount: 8, lastUsed: '1 нед' },
  { id: '3', name: 'Полная проверка здоровья', type: 'HEALTH', description: 'Проверка всех прокси и аккаунтов', usedCount: 50, lastUsed: '1 д' },
];

export default function TemplatesPage() {
  const router = useRouter();
  const toast = useToast();

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader
        title="Шаблоны"
        subtitle="Готовые конфигурации для повторяющихся операций"
        action={
          <Button onClick={() => toast('Создание шаблона скоро будет доступно', 'info')}>
            <Plus size={16} /> Создать
          </Button>
        }
      />

      <div className="grid gap-3 sm:grid-cols-2">
        {TEMPLATES.map((tpl) => (
          <Card key={tpl.id} className="flex flex-col gap-3 p-4">
            <div className="flex items-start gap-3">
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent-weak text-accent">
                <FileText size={18} />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <p className="truncate font-semibold text-fg">{tpl.name}</p>
                  <Badge tone="neutral">{tpl.type}</Badge>
                </div>
                <p className="mt-0.5 text-xs text-fg-muted">{tpl.description}</p>
              </div>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-2xs text-fg-hint">
                Использован {tpl.usedCount}× · {tpl.lastUsed} назад
              </span>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => {
                  toast(`Шаблон «${tpl.name}» применён`, 'success');
                  router.push('/operations/new');
                }}
              >
                <Copy size={13} /> Использовать
              </Button>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
