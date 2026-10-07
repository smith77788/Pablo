'use client';
import { useState } from 'react';
import { Key, Plus, Trash2, Copy, Check, AlertTriangle, Moon, Sun, Monitor, Bell } from 'lucide-react';
import {
  PageHeader,
  Button,
  Card,
  CardHeader,
  Sheet,
  Input,
  Label,
  EmptyState,
  Segmented,
  useToast,
  cn,
} from '@/components/ui';

interface ApiKeyItem {
  id: string;
  name: string;
  prefix: string;
  createdAt: string;
  expiresAt: string | null;
}

const MOCK_KEYS: ApiKeyItem[] = [
  { id: '1', name: 'Production', prefix: 'sk_prod_a', createdAt: new Date().toISOString(), expiresAt: null },
  { id: '2', name: 'Development', prefix: 'sk_dev_b1', createdAt: new Date(Date.now() - 8.64e7).toISOString(), expiresAt: '2026-12-31' },
];

const fmt = (iso: string) => new Date(iso).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });

function applyTheme(mode: 'system' | 'light' | 'dark') {
  const root = document.documentElement;
  if (mode === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', mode);
  try {
    localStorage.setItem('ig_theme', mode);
  } catch {
    /* noop */
  }
}

export default function SettingsPage() {
  const toast = useToast();
  const [keys, setKeys] = useState<ApiKeyItem[]>(MOCK_KEYS);
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState('');
  const [createdKey, setCreatedKey] = useState<{ name: string; key: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<ApiKeyItem | null>(null);
  const [theme, setTheme] = useState<'system' | 'light' | 'dark'>('system');

  function create() {
    if (!newName.trim()) return;
    const key = Array.from(crypto.getRandomValues(new Uint8Array(32)))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');
    setKeys((p) => [
      { id: Date.now().toString(), name: newName.trim(), prefix: key.slice(0, 8), createdAt: new Date().toISOString(), expiresAt: null },
      ...p,
    ]);
    setCreatedKey({ name: newName.trim(), key });
    setNewName('');
    setAdding(false);
  }

  function revoke(k: ApiKeyItem) {
    setKeys((p) => p.filter((x) => x.id !== k.id));
    setRevokeTarget(null);
    toast('Ключ отозван', 'success');
  }

  function copy(text: string) {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      toast('Скопировано', 'success');
      setTimeout(() => setCopied(false), 2000);
    });
  }

  function setThemeMode(m: 'system' | 'light' | 'dark') {
    setTheme(m);
    applyTheme(m);
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5 p-4 sm:p-6">
      <PageHeader title="Настройки" subtitle="Аккаунт, оформление и интеграции" />

      {/* Appearance */}
      <Card>
        <CardHeader title="Оформление" subtitle="Тема вне Telegram (в Mini App берётся из клиента)" icon={<Monitor size={16} />} />
        <div className="p-4">
          <Segmented
            value={theme}
            onChange={setThemeMode}
            options={[
              { value: 'system', label: '◐ Система' },
              { value: 'light', label: '☀ Светлая' },
              { value: 'dark', label: '☾ Тёмная' },
            ]}
          />
        </div>
      </Card>

      {/* Notifications (visual toggles) */}
      <Card>
        <CardHeader title="Уведомления" subtitle="Что присылать в Telegram" icon={<Bell size={16} />} />
        <div className="divide-y divide-line">
          {[
            { k: 'ops', label: 'Статусы операций', on: true },
            { k: 'health', label: 'Падение health / flood-wait', on: true },
            { k: 'ranks', label: 'Изменения позиций', on: false },
          ].map((n) => (
            <Toggle key={n.k} label={n.label} defaultOn={n.on} onChange={(v) => toast(v ? 'Уведомление включено' : 'Уведомление выключено', 'info')} />
          ))}
        </div>
      </Card>

      {/* API Keys */}
      <Card>
        <CardHeader
          title="API ключи"
          subtitle="Доступ к API из внешних приложений"
          icon={<Key size={16} />}
          action={
            <Button size="sm" onClick={() => setAdding(true)}>
              <Plus size={14} /> Создать
            </Button>
          }
        />
        {keys.length === 0 ? (
          <div className="p-4">
            <EmptyState icon={<Key size={24} />} title="Нет ключей" description="Создайте первый API-ключ." className="border-0" />
          </div>
        ) : (
          <div className="divide-y divide-line">
            {keys.map((k) => (
              <div key={k.id} className="flex items-center gap-3 px-4 py-3.5">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-semibold text-fg">{k.name}</p>
                  <p className="mt-0.5 text-xs text-fg-hint">
                    <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono">{k.prefix}…</code> · создан {fmt(k.createdAt)} ·{' '}
                    {k.expiresAt ? <span className="text-warning">до {fmt(k.expiresAt)}</span> : 'бессрочный'}
                  </p>
                </div>
                <Button size="icon" variant="ghost" onClick={() => setRevokeTarget(k)} title="Отозвать">
                  <Trash2 size={16} />
                </Button>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Create key sheet */}
      <Sheet
        open={adding}
        onClose={() => {
          setAdding(false);
          setNewName('');
        }}
        title="Новый API ключ"
        footer={
          <>
            <Button variant="secondary" onClick={() => setAdding(false)}>
              Отмена
            </Button>
            <Button onClick={create} disabled={!newName.trim()}>
              Создать
            </Button>
          </>
        }
      >
        <div className="py-1">
          <Label>Название ключа</Label>
          <Input value={newName} onChange={(e) => setNewName(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && create()} placeholder="Production" autoFocus />
        </div>
      </Sheet>

      {/* Show created key */}
      <Sheet
        open={!!createdKey}
        onClose={() => {
          setCreatedKey(null);
          setCopied(false);
        }}
        title="API ключ создан"
        footer={
          <Button
            variant="secondary"
            onClick={() => {
              setCreatedKey(null);
              setCopied(false);
            }}
          >
            Закрыть
          </Button>
        }
      >
        <div className="space-y-3 py-1">
          <div className="flex gap-3 rounded-xl bg-warning-weak px-4 py-3">
            <AlertTriangle size={18} className="mt-0.5 shrink-0 text-warning" />
            <p className="text-sm text-warning">
              <b>Сохраните ключ — он показывается один раз.</b> После закрытия восстановить его нельзя.
            </p>
          </div>
          <div>
            <Label>Ключ для «{createdKey?.name}»</Label>
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 select-all break-all rounded-xl bg-surface-2 px-3.5 py-3 font-mono text-xs text-fg">
                {createdKey?.key}
              </code>
              <Button size="icon" variant={copied ? 'success' : 'primary'} onClick={() => createdKey && copy(createdKey.key)} className="h-11 w-11">
                {copied ? <Check size={16} /> : <Copy size={16} />}
              </Button>
            </div>
          </div>
        </div>
      </Sheet>

      {/* Revoke confirm */}
      <Sheet
        open={!!revokeTarget}
        onClose={() => setRevokeTarget(null)}
        size="sm"
        title="Отозвать ключ?"
        description={`«${revokeTarget?.name}» будет деактивирован немедленно. Приложения с этим ключом потеряют доступ.`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRevokeTarget(null)}>
              Отмена
            </Button>
            <Button variant="danger" onClick={() => revokeTarget && revoke(revokeTarget)}>
              Отозвать
            </Button>
          </>
        }
      />
    </div>
  );
}

function Toggle({ label, defaultOn, onChange }: { label: string; defaultOn?: boolean; onChange?: (v: boolean) => void }) {
  const [on, setOn] = useState(!!defaultOn);
  return (
    <div className="flex items-center justify-between px-4 py-3.5">
      <span className="text-sm text-fg">{label}</span>
      <button
        onClick={() => {
          const v = !on;
          setOn(v);
          onChange?.(v);
        }}
        className={cn('relative h-6 w-11 rounded-full transition-colors', on ? 'bg-accent' : 'bg-surface-2')}
        aria-pressed={on}
      >
        <span
          className={cn(
            'absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all',
            on ? 'left-[22px]' : 'left-0.5',
          )}
        />
      </button>
    </div>
  );
}
