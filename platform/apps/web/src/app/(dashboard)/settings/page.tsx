'use client';
import { useState } from 'react';
import { Key, Plus, Trash2, X, Copy, Check, AlertTriangle } from 'lucide-react';

interface ApiKeyItem {
  id: string;
  name: string;
  prefix: string;
  createdAt: string;
  expiresAt: string | null;
}

const MOCK_KEYS: ApiKeyItem[] = [
  { id: '1', name: 'Production', prefix: 'sk_prod_a', createdAt: new Date().toISOString(), expiresAt: null },
  { id: '2', name: 'Development', prefix: 'sk_dev_b1', createdAt: new Date(Date.now() - 86400000).toISOString(), expiresAt: '2026-12-31' },
];

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

export default function SettingsPage() {
  const [keys, setKeys] = useState<ApiKeyItem[]>(MOCK_KEYS);

  // Create form state
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState('');

  // Modal state for showing newly created key
  const [createdKey, setCreatedKey] = useState<{ name: string; key: string } | null>(null);
  const [copied, setCopied] = useState(false);

  // Revoke confirmation state
  const [revokeTarget, setRevokeTarget] = useState<ApiKeyItem | null>(null);

  function handleCreate() {
    if (!newName.trim()) return;

    // Generate a mock key for demonstration (in production, this comes from the API)
    const mockKey = Array.from(crypto.getRandomValues(new Uint8Array(32)))
      .map(b => b.toString(16).padStart(2, '0'))
      .join('');
    const prefix = mockKey.slice(0, 8);

    const newItem: ApiKeyItem = {
      id: Date.now().toString(),
      name: newName.trim(),
      prefix,
      createdAt: new Date().toISOString(),
      expiresAt: null,
    };

    setKeys(prev => [newItem, ...prev]);
    setCreatedKey({ name: newItem.name, key: mockKey });
    setNewName('');
    setAdding(false);
  }

  function handleRevoke(key: ApiKeyItem) {
    setKeys(prev => prev.filter(k => k.id !== key.id));
    setRevokeTarget(null);
  }

  function handleCopy(text: string) {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <div className="p-6 space-y-6">
      {/* Page Header */}
      <div>
        <h1 className="text-xl font-bold text-slate-800">Настройки</h1>
        <p className="text-sm text-slate-400 mt-0.5">Управление настройками аккаунта и интеграциями</p>
      </div>

      {/* General settings placeholder */}
      <div className="bg-white border border-slate-200 rounded-xl p-6 text-slate-500 text-sm">
        Управление настройками аккаунта, операторами и интеграциями.
      </div>

      {/* ─── API Keys Section ─────────────────────────────────────────────────── */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-base font-semibold text-slate-800">API ключи</h2>
            <p className="text-sm text-slate-400 mt-0.5">Ключи для доступа к API платформы из внешних приложений</p>
          </div>
          <button
            onClick={() => setAdding(true)}
            className="flex items-center gap-2 px-4 py-2 bg-sky-500 text-white text-sm font-medium rounded-lg hover:bg-sky-600 transition-colors"
          >
            <Plus size={15} /> Создать API ключ
          </button>
        </div>

        {/* Create Form */}
        {adding && (
          <div className="bg-white border border-slate-200 rounded-xl p-5 space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold text-slate-700 text-sm">Новый API ключ</h3>
              <button
                onClick={() => { setAdding(false); setNewName(''); }}
                className="text-slate-400 hover:text-slate-600"
              >
                <X size={16} />
              </button>
            </div>
            <div className="flex gap-3">
              <input
                value={newName}
                onChange={e => setNewName(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && handleCreate()}
                placeholder="Название ключа (например: Production)"
                className="flex-1 px-4 py-2 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                autoFocus
              />
              <button
                onClick={handleCreate}
                disabled={!newName.trim()}
                className="px-4 py-2 bg-sky-500 text-white text-sm font-medium rounded-lg disabled:opacity-40 hover:bg-sky-600 transition-colors"
              >
                Создать
              </button>
            </div>
          </div>
        )}

        {/* Keys Table */}
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
          {keys.length === 0 ? (
            <div className="text-center py-12 text-slate-400">
              <Key size={36} className="mx-auto mb-3 opacity-30" />
              <p>Нет API ключей. Создайте первый ключ.</p>
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50">
                  <th className="text-left px-5 py-3 font-medium text-slate-500">Название</th>
                  <th className="text-left px-5 py-3 font-medium text-slate-500">Prefix</th>
                  <th className="text-left px-5 py-3 font-medium text-slate-500">Создан</th>
                  <th className="text-left px-5 py-3 font-medium text-slate-500">Истекает</th>
                  <th className="px-5 py-3" />
                </tr>
              </thead>
              <tbody>
                {keys.map((k, i) => (
                  <tr
                    key={k.id}
                    className={`border-b border-slate-100 last:border-0 hover:bg-slate-50 transition-colors ${i % 2 === 0 ? '' : 'bg-slate-50/40'}`}
                  >
                    <td className="px-5 py-3.5 font-medium text-slate-800">{k.name}</td>
                    <td className="px-5 py-3.5">
                      <code className="bg-slate-100 text-slate-600 px-2 py-0.5 rounded text-xs font-mono">
                        {k.prefix}…
                      </code>
                    </td>
                    <td className="px-5 py-3.5 text-slate-500">{formatDate(k.createdAt)}</td>
                    <td className="px-5 py-3.5 text-slate-500">
                      {k.expiresAt ? (
                        <span className="text-orange-600">{formatDate(k.expiresAt)}</span>
                      ) : (
                        <span className="text-slate-400">Бессрочный</span>
                      )}
                    </td>
                    <td className="px-5 py-3.5 text-right">
                      <button
                        onClick={() => setRevokeTarget(k)}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-red-500 bg-red-50 rounded-lg hover:bg-red-100 transition-colors ml-auto"
                      >
                        <Trash2 size={13} /> Отозвать
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* ─── Modal: Show New Key ──────────────────────────────────────────────── */}
      {createdKey && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-2xl shadow-xl p-6 w-full max-w-lg mx-4 space-y-4">
            <div className="flex items-start justify-between">
              <h2 className="font-semibold text-slate-800 text-base">API ключ создан</h2>
              <button
                onClick={() => { setCreatedKey(null); setCopied(false); }}
                className="text-slate-400 hover:text-slate-600"
              >
                <X size={18} />
              </button>
            </div>

            {/* Warning */}
            <div className="flex gap-3 bg-amber-50 border border-amber-200 rounded-lg px-4 py-3">
              <AlertTriangle size={18} className="text-amber-500 shrink-0 mt-0.5" />
              <p className="text-sm text-amber-700">
                <span className="font-semibold">Сохраните ключ — он показывается один раз.</span>{' '}
                После закрытия этого окна ключ нельзя будет восстановить.
              </p>
            </div>

            <div>
              <p className="text-xs text-slate-500 mb-1.5">Ключ для «{createdKey.name}»</p>
              <div className="flex items-center gap-2">
                <code className="flex-1 bg-slate-100 text-slate-700 px-4 py-3 rounded-lg text-xs font-mono break-all select-all">
                  {createdKey.key}
                </code>
                <button
                  onClick={() => handleCopy(createdKey.key)}
                  className={`shrink-0 flex items-center gap-1.5 px-3 py-2.5 text-sm font-medium rounded-lg transition-colors ${
                    copied
                      ? 'bg-green-500 text-white'
                      : 'bg-sky-500 text-white hover:bg-sky-600'
                  }`}
                >
                  {copied ? <Check size={15} /> : <Copy size={15} />}
                  {copied ? 'Скопировано' : 'Скопировать'}
                </button>
              </div>
            </div>

            <div className="flex justify-end">
              <button
                onClick={() => { setCreatedKey(null); setCopied(false); }}
                className="px-4 py-2 text-sm font-medium text-white bg-slate-700 rounded-lg hover:bg-slate-800 transition-colors"
              >
                Закрыть
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ─── Modal: Revoke Confirmation ───────────────────────────────────────── */}
      {revokeTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-2xl shadow-xl p-6 w-full max-w-sm mx-4 space-y-4">
            <div className="flex items-start justify-between">
              <h2 className="font-semibold text-slate-800 text-base">Отозвать ключ?</h2>
              <button onClick={() => setRevokeTarget(null)} className="text-slate-400 hover:text-slate-600">
                <X size={18} />
              </button>
            </div>
            <p className="text-sm text-slate-500">
              Ключ{' '}
              <span className="font-medium text-slate-700">«{revokeTarget.name}»</span>{' '}
              будет немедленно деактивирован. Все приложения, использующие этот ключ, потеряют доступ.
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setRevokeTarget(null)}
                className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
              >
                Отмена
              </button>
              <button
                onClick={() => handleRevoke(revokeTarget)}
                className="px-4 py-2 text-sm text-white bg-red-500 rounded-lg hover:bg-red-600 transition-colors"
              >
                Отозвать
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
