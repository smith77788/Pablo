'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { authApi } from '@/lib/api';
import { Zap, Plus, Trash2, MessageSquare, Tag, Webhook } from 'lucide-react';
import {
  PageHeader,
  Button,
  Card,
  Badge,
  Sheet,
  Input,
  Textarea,
  Select,
  Label,
  EmptyState,
  useToast,
  cn,
} from '@/components/ui';

interface AutomationItem {
  id: string;
  name: string;
  triggerType: string;
  keyword?: string;
  actionType: string;
  actionPayload: string;
  isActive: boolean;
  createdAt: string;
}

const TRIGGER_LABELS: Record<string, string> = {
  message_received: 'Любое сообщение',
  keyword: 'Ключевое слово',
  user_joined: 'Новый пользователь',
};
const ACTION_META: Record<string, { label: string; Icon: typeof MessageSquare }> = {
  send_message: { label: 'Сообщение', Icon: MessageSquare },
  add_tag: { label: 'Тег', Icon: Tag },
  webhook: { label: 'Вебхук', Icon: Webhook },
};

const MOCK: AutomationItem[] = [
  { id: 'm1', name: 'Приветствие новых', triggerType: 'user_joined', actionType: 'send_message', actionPayload: 'Добро пожаловать! Чем могу помочь?', isActive: true, createdAt: '' },
  { id: 'm2', name: 'Тег по ключевому слову', triggerType: 'keyword', keyword: 'цена', actionType: 'add_tag', actionPayload: 'interested', isActive: false, createdAt: '' },
  { id: 'm3', name: 'Уведомление на вебхук', triggerType: 'message_received', actionType: 'webhook', actionPayload: 'https://example.com/webhook', isActive: true, createdAt: '' },
];

const EMPTY_FORM = { name: '', triggerType: 'message_received', keyword: '', actionType: 'send_message', actionPayload: '' };

export default function AutomationsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState<AutomationItem | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);

  const { data, isError } = useQuery<AutomationItem[]>({
    queryKey: ['automations'],
    queryFn: () => authApi.get('/automations'),
  });
  const rules = isError ? MOCK : data ?? [];

  const create = useMutation({
    mutationFn: () =>
      authApi.post('/automations', {
        name: form.name,
        triggerType: form.triggerType,
        keyword: form.triggerType === 'keyword' ? form.keyword : undefined,
        actionType: form.actionType,
        actionPayload: form.actionPayload,
      }),
    onSuccess: () => {
      setForm(EMPTY_FORM);
      setAdding(false);
      toast('Правило создано', 'success');
      qc.invalidateQueries({ queryKey: ['automations'] });
    },
    onError: () => toast('Ошибка при создании правила', 'error'),
  });

  const toggle = useMutation({
    mutationFn: (id: string) => authApi.patch(`/automations/${id}/toggle`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['automations'] }),
  });

  const remove = useMutation({
    mutationFn: (id: string) => authApi.delete(`/automations/${id}`),
    onSuccess: () => {
      setDeleteConfirm(null);
      toast('Правило удалено', 'success');
      qc.invalidateQueries({ queryKey: ['automations'] });
    },
  });

  const payloadLabel =
    form.actionType === 'send_message' ? 'Текст сообщения' : form.actionType === 'add_tag' ? 'Название тега' : 'URL вебхука';
  const valid = form.name.trim() && form.actionPayload.trim() && (form.triggerType !== 'keyword' || form.keyword.trim());
  const set = (k: keyof typeof EMPTY_FORM, v: string) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader
        title="Автоматизации"
        subtitle="Правила автодействий на события"
        action={
          <Button onClick={() => setAdding(true)}>
            <Plus size={16} /> Добавить
          </Button>
        }
      />

      {rules.length === 0 ? (
        <EmptyState
          icon={<Zap size={26} />}
          title="Нет правил"
          description="Создайте первое правило автоматизации."
          action={
            <Button onClick={() => setAdding(true)}>
              <Plus size={16} /> Создать правило
            </Button>
          }
        />
      ) : (
        <div className="space-y-2.5">
          {rules.map((r) => {
            const am = ACTION_META[r.actionType] ?? { label: r.actionType, Icon: Zap };
            return (
              <Card key={r.id} className="flex items-center gap-3 p-3.5">
                <span
                  className={cn(
                    'flex h-10 w-10 shrink-0 items-center justify-center rounded-xl',
                    r.isActive ? 'bg-accent-weak text-accent' : 'bg-surface-2 text-fg-hint',
                  )}
                >
                  <Zap size={18} />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-semibold text-fg">{r.name}</p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    <Badge tone="violet">{TRIGGER_LABELS[r.triggerType] ?? r.triggerType}</Badge>
                    {r.triggerType === 'keyword' && r.keyword && <Badge tone="neutral">«{r.keyword}»</Badge>}
                    <Badge tone="success">
                      <am.Icon size={10} /> {am.label}
                    </Badge>
                  </div>
                  <p className="mt-1 truncate text-2xs text-fg-hint" title={r.actionPayload}>
                    {r.actionPayload}
                  </p>
                </div>
                <div className="flex flex-col items-end gap-2">
                  <button
                    onClick={() => toggle.mutate(r.id)}
                    disabled={toggle.isPending}
                    className={cn('relative h-6 w-11 rounded-full transition-colors', r.isActive ? 'bg-accent' : 'bg-surface-2')}
                    title={r.isActive ? 'Выключить' : 'Включить'}
                  >
                    <span className={cn('absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all', r.isActive ? 'left-[22px]' : 'left-0.5')} />
                  </button>
                  <Button size="icon" variant="ghost" onClick={() => setDeleteConfirm(r)} title="Удалить" className="h-7 w-7">
                    <Trash2 size={14} />
                  </Button>
                </div>
              </Card>
            );
          })}
        </div>
      )}

      {/* Create sheet */}
      <Sheet
        open={adding}
        onClose={() => setAdding(false)}
        title="Новое правило"
        description="Если случится событие → выполнить действие"
        footer={
          <>
            <Button variant="secondary" onClick={() => setAdding(false)}>
              Отмена
            </Button>
            <Button onClick={() => create.mutate()} disabled={!valid} loading={create.isPending}>
              Создать
            </Button>
          </>
        }
      >
        <div className="space-y-3 py-1">
          <div>
            <Label>Название</Label>
            <Input value={form.name} onChange={(e) => set('name', e.target.value)} placeholder="Приветствие новых" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Триггер</Label>
              <Select value={form.triggerType} onChange={(e) => { set('triggerType', e.target.value); set('keyword', ''); }}>
                <option value="message_received">Любое сообщение</option>
                <option value="keyword">Ключевое слово</option>
                <option value="user_joined">Новый пользователь</option>
              </Select>
            </div>
            <div>
              <Label>Действие</Label>
              <Select value={form.actionType} onChange={(e) => set('actionType', e.target.value)}>
                <option value="send_message">Отправить сообщение</option>
                <option value="add_tag">Добавить тег</option>
                <option value="webhook">Вызвать вебхук</option>
              </Select>
            </div>
          </div>
          {form.triggerType === 'keyword' && (
            <div>
              <Label>Ключевое слово</Label>
              <Input value={form.keyword} onChange={(e) => set('keyword', e.target.value)} placeholder="цена" />
            </div>
          )}
          <div>
            <Label>{payloadLabel}</Label>
            {form.actionType === 'send_message' ? (
              <Textarea value={form.actionPayload} onChange={(e) => set('actionPayload', e.target.value)} placeholder="Текст сообщения…" />
            ) : (
              <Input value={form.actionPayload} onChange={(e) => set('actionPayload', e.target.value)} placeholder={form.actionType === 'add_tag' ? 'interested' : 'https://example.com/webhook'} />
            )}
          </div>
        </div>
      </Sheet>

      {/* Delete confirm */}
      <Sheet
        open={!!deleteConfirm}
        onClose={() => setDeleteConfirm(null)}
        size="sm"
        title="Удалить правило?"
        description={`«${deleteConfirm?.name}» будет удалено. Действие необратимо.`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeleteConfirm(null)}>
              Отмена
            </Button>
            <Button variant="danger" onClick={() => deleteConfirm && remove.mutate(deleteConfirm.id)} loading={remove.isPending}>
              Удалить
            </Button>
          </>
        }
      />
    </div>
  );
}
