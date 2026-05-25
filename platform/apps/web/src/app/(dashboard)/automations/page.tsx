'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { authApi } from '@/lib/api';
import { Zap, Plus, Trash2, ToggleLeft, ToggleRight, X } from 'lucide-react';

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

const ACTION_LABELS: Record<string, string> = {
  send_message: 'Отправить сообщение',
  add_tag: 'Добавить тег',
  webhook: 'Вебхук',
};

const MOCK_AUTOMATIONS: AutomationItem[] = [
  {
    id: 'mock-1',
    name: 'Приветствие новых',
    triggerType: 'user_joined',
    actionType: 'send_message',
    actionPayload: 'Добро пожаловать! Чем могу помочь?',
    isActive: true,
    createdAt: new Date().toISOString(),
  },
  {
    id: 'mock-2',
    name: 'Тег по ключевому слову',
    triggerType: 'keyword',
    keyword: 'цена',
    actionType: 'add_tag',
    actionPayload: 'interested',
    isActive: false,
    createdAt: new Date().toISOString(),
  },
  {
    id: 'mock-3',
    name: 'Уведомление на вебхук',
    triggerType: 'message_received',
    actionType: 'webhook',
    actionPayload: 'https://example.com/webhook',
    isActive: true,
    createdAt: new Date().toISOString(),
  },
];

export default function AutomationsPage() {
  const qc = useQueryClient();

  const [adding, setAdding] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState<AutomationItem | null>(null);

  // Form state
  const [formName, setFormName] = useState('');
  const [formTriggerType, setFormTriggerType] = useState('message_received');
  const [formKeyword, setFormKeyword] = useState('');
  const [formActionType, setFormActionType] = useState('send_message');
  const [formActionPayload, setFormActionPayload] = useState('');

  const { data: automations, isError } = useQuery<AutomationItem[]>({
    queryKey: ['automations'],
    queryFn: () => authApi.get('/automations'),
  });

  const displayAutomations: AutomationItem[] = isError
    ? MOCK_AUTOMATIONS
    : (automations ?? []);

  const createAutomation = useMutation({
    mutationFn: () =>
      authApi.post('/automations', {
        name: formName,
        triggerType: formTriggerType,
        keyword: formTriggerType === 'keyword' ? formKeyword : undefined,
        actionType: formActionType,
        actionPayload: formActionPayload,
      }),
    onSuccess: () => {
      setFormName('');
      setFormTriggerType('message_received');
      setFormKeyword('');
      setFormActionType('send_message');
      setFormActionPayload('');
      setAdding(false);
      qc.invalidateQueries({ queryKey: ['automations'] });
    },
  });

  const toggleAutomation = useMutation({
    mutationFn: (id: string) => authApi.patch(`/automations/${id}/toggle`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['automations'] });
    },
  });

  const deleteAutomation = useMutation({
    mutationFn: (id: string) => authApi.delete(`/automations/${id}`),
    onSuccess: () => {
      setDeleteConfirm(null);
      qc.invalidateQueries({ queryKey: ['automations'] });
    },
  });

  const actionPayloadPlaceholder: Record<string, string> = {
    send_message: 'Текст сообщения...',
    add_tag: 'Название тега',
    webhook: 'https://example.com/webhook',
  };

  const isFormValid =
    formName.trim() &&
    formActionPayload.trim() &&
    (formTriggerType !== 'keyword' || formKeyword.trim());

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-800">Автоматизации</h1>
          <p className="text-sm text-slate-400 mt-0.5">Правила автоматических действий на события</p>
        </div>
        <button
          onClick={() => setAdding(true)}
          className="flex items-center gap-2 px-4 py-2 bg-sky-500 text-white text-sm font-medium rounded-lg hover:bg-sky-600 transition-colors"
        >
          <Plus size={15} /> Добавить правило
        </button>
      </div>

      {/* Add Form */}
      {adding && (
        <div className="bg-white border border-slate-200 rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold text-slate-700 text-sm">Новое правило</h2>
            <button onClick={() => setAdding(false)} className="text-slate-400 hover:text-slate-600">
              <X size={16} />
            </button>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {/* Name */}
            <div className="sm:col-span-2">
              <label className="block text-xs font-medium text-slate-500 mb-1">Название</label>
              <input
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                placeholder="Например: Приветствие новых пользователей"
                className="w-full px-4 py-2 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
              />
            </div>

            {/* Trigger Type */}
            <div>
              <label className="block text-xs font-medium text-slate-500 mb-1">Триггер</label>
              <select
                value={formTriggerType}
                onChange={(e) => {
                  setFormTriggerType(e.target.value);
                  setFormKeyword('');
                }}
                className="w-full px-4 py-2 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500 bg-white"
              >
                <option value="message_received">Любое сообщение</option>
                <option value="keyword">Ключевое слово</option>
                <option value="user_joined">Новый пользователь</option>
              </select>
            </div>

            {/* Keyword (conditional) */}
            {formTriggerType === 'keyword' && (
              <div>
                <label className="block text-xs font-medium text-slate-500 mb-1">Ключевое слово</label>
                <input
                  value={formKeyword}
                  onChange={(e) => setFormKeyword(e.target.value)}
                  placeholder="Например: цена"
                  className="w-full px-4 py-2 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                />
              </div>
            )}

            {/* Action Type */}
            <div>
              <label className="block text-xs font-medium text-slate-500 mb-1">Действие</label>
              <select
                value={formActionType}
                onChange={(e) => setFormActionType(e.target.value)}
                className="w-full px-4 py-2 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500 bg-white"
              >
                <option value="send_message">Отправить сообщение</option>
                <option value="add_tag">Добавить тег</option>
                <option value="webhook">Вызвать вебхук</option>
              </select>
            </div>

            {/* Action Payload */}
            <div className="sm:col-span-2">
              <label className="block text-xs font-medium text-slate-500 mb-1">
                {formActionType === 'send_message'
                  ? 'Текст сообщения'
                  : formActionType === 'add_tag'
                  ? 'Название тега'
                  : 'URL вебхука'}
              </label>
              <textarea
                value={formActionPayload}
                onChange={(e) => setFormActionPayload(e.target.value)}
                placeholder={actionPayloadPlaceholder[formActionType]}
                rows={formActionType === 'send_message' ? 3 : 1}
                className="w-full px-4 py-2 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500 resize-none"
              />
            </div>
          </div>

          {createAutomation.isError && (
            <p className="text-red-500 text-sm">Ошибка при создании правила. Попробуйте снова.</p>
          )}

          <div className="flex gap-3 justify-end pt-1">
            <button
              onClick={() => setAdding(false)}
              className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
            >
              Отмена
            </button>
            <button
              onClick={() => createAutomation.mutate()}
              disabled={!isFormValid || createAutomation.isPending}
              className="px-4 py-2 bg-sky-500 text-white text-sm font-medium rounded-lg disabled:opacity-40 hover:bg-sky-600 transition-colors"
            >
              {createAutomation.isPending ? 'Сохранение...' : 'Создать'}
            </button>
          </div>
        </div>
      )}

      {/* Mock data notice */}
      {isError && (
        <div className="bg-orange-50 border border-orange-200 rounded-lg px-4 py-2 text-sm text-orange-600">
          API недоступен — показаны демо-данные
        </div>
      )}

      {/* Table */}
      <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-100 bg-slate-50">
              <th className="text-left px-5 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">
                Название
              </th>
              <th className="text-left px-5 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">
                Триггер
              </th>
              <th className="text-left px-5 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">
                Действие
              </th>
              <th className="text-left px-5 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">
                Статус
              </th>
              <th className="px-5 py-3" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-50">
            {displayAutomations.map((rule) => (
              <tr key={rule.id} className="hover:bg-slate-50 transition-colors">
                {/* Name */}
                <td className="px-5 py-3.5">
                  <div className="flex items-center gap-2">
                    <span className="w-7 h-7 bg-sky-50 rounded-lg flex items-center justify-center shrink-0">
                      <Zap size={13} className="text-sky-500" />
                    </span>
                    <div>
                      <p className="font-medium text-slate-800">{rule.name}</p>
                      {rule.triggerType === 'keyword' && rule.keyword && (
                        <p className="text-xs text-slate-400">«{rule.keyword}»</p>
                      )}
                    </div>
                  </div>
                </td>

                {/* Trigger */}
                <td className="px-5 py-3.5">
                  <span className="text-xs px-2 py-0.5 rounded-full font-medium bg-violet-50 text-violet-700">
                    {TRIGGER_LABELS[rule.triggerType] ?? rule.triggerType}
                  </span>
                </td>

                {/* Action */}
                <td className="px-5 py-3.5">
                  <div>
                    <span className="text-xs px-2 py-0.5 rounded-full font-medium bg-emerald-50 text-emerald-700">
                      {ACTION_LABELS[rule.actionType] ?? rule.actionType}
                    </span>
                    <p className="text-xs text-slate-400 mt-1 max-w-[200px] truncate" title={rule.actionPayload}>
                      {rule.actionPayload}
                    </p>
                  </div>
                </td>

                {/* Status */}
                <td className="px-5 py-3.5">
                  <span
                    className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                      rule.isActive
                        ? 'bg-green-100 text-green-700'
                        : 'bg-slate-100 text-slate-500'
                    }`}
                  >
                    {rule.isActive ? 'Активно' : 'Выключено'}
                  </span>
                </td>

                {/* Actions */}
                <td className="px-5 py-3.5">
                  <div className="flex items-center gap-1 justify-end">
                    <button
                      onClick={() => toggleAutomation.mutate(rule.id)}
                      disabled={toggleAutomation.isPending}
                      title={rule.isActive ? 'Выключить' : 'Включить'}
                      className={`p-1.5 rounded-lg transition-colors disabled:opacity-40 ${
                        rule.isActive
                          ? 'text-green-600 hover:bg-green-50'
                          : 'text-slate-400 hover:bg-slate-100'
                      }`}
                    >
                      {rule.isActive ? <ToggleRight size={20} /> : <ToggleLeft size={20} />}
                    </button>
                    <button
                      onClick={() => setDeleteConfirm(rule)}
                      title="Удалить"
                      className="p-1.5 text-slate-400 hover:text-red-500 rounded-lg hover:bg-red-50 transition-colors"
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {displayAutomations.length === 0 && (
          <div className="text-center py-12 text-slate-400">
            <Zap size={36} className="mx-auto mb-3 opacity-30" />
            <p>Нет правил автоматизации. Создайте первое.</p>
          </div>
        )}
      </div>

      {/* Delete Confirmation Modal */}
      {deleteConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-2xl shadow-xl p-6 w-full max-w-sm mx-4 space-y-4">
            <div className="flex items-start justify-between">
              <h2 className="font-semibold text-slate-800 text-base">Удалить правило?</h2>
              <button onClick={() => setDeleteConfirm(null)} className="text-slate-400 hover:text-slate-600">
                <X size={18} />
              </button>
            </div>
            <p className="text-sm text-slate-500">
              Вы уверены, что хотите удалить правило{' '}
              <span className="font-medium text-slate-700">«{deleteConfirm.name}»</span>? Это
              действие необратимо.
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setDeleteConfirm(null)}
                className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
              >
                Отмена
              </button>
              <button
                onClick={() => deleteAutomation.mutate(deleteConfirm.id)}
                disabled={deleteAutomation.isPending}
                className="px-4 py-2 text-sm text-white bg-red-500 rounded-lg hover:bg-red-600 disabled:opacity-40 transition-colors"
              >
                {deleteAutomation.isPending ? 'Удаление...' : 'Удалить'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
