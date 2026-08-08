'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, useEffect, useRef } from 'react';
import { authApi } from '@/lib/api';
import { formatDistanceToNow } from 'date-fns';
import { ru } from 'date-fns/locale';
import { Send, User, Clock, CheckCircle, MessageSquare } from 'lucide-react';
import clsx from 'clsx';
import io from 'socket.io-client';

export default function InboxPage() {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [text, setText] = useState('');
  const [filter, setFilter] = useState('OPEN');
  const bottomRef = useRef<HTMLDivElement>(null);

  const { data: conversations } = useQuery({
    queryKey: ['conversations', filter],
    queryFn: () => authApi.get(`/conversations?status=${filter}&limit=50`),
    refetchInterval: 10_000,
  });

  const { data: conv } = useQuery({
    queryKey: ['conversation', selectedId],
    queryFn: () => authApi.get(`/conversations/${selectedId}`),
    enabled: !!selectedId,
    refetchInterval: 5_000,
  });

  const sendMsg = useMutation({
    mutationFn: (t: string) => authApi.post(`/conversations/${selectedId}/messages`, { text: t }),
    onSuccess: () => { setText(''); qc.invalidateQueries({ queryKey: ['conversation', selectedId] }); },
  });

  const resolve = useMutation({
    mutationFn: () => authApi.patch(`/conversations/${selectedId}/status`, { status: 'RESOLVED' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['conversations'] }),
  });

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [conv?.messages]);

  // WebSocket for real-time
  useEffect(() => {
    const token = localStorage.getItem('token');
    if (!token) return;
    const socket = io(`${process.env.NEXT_PUBLIC_WS_URL}/inbox`, { auth: { token } });
    socket.on('message.new', () => {
      qc.invalidateQueries({ queryKey: ['conversations'] });
      if (selectedId) qc.invalidateQueries({ queryKey: ['conversation', selectedId] });
    });
    return () => { socket.disconnect(); };
  }, [selectedId, qc]);

  const items = conversations?.items ?? [];
  const STATUS_COLORS: Record<string, string> = { OPEN: 'bg-green-100 text-green-700', PENDING: 'bg-yellow-100 text-yellow-700', RESOLVED: 'bg-slate-100 text-slate-500' };

  return (
    <div className="flex h-full">
      {/* Conversation list */}
      <div className="w-80 border-r border-slate-200 bg-white flex flex-col">
        <div className="p-4 border-b border-slate-100">
          <h1 className="font-semibold text-slate-800 mb-3">Входящие</h1>
          <div className="flex gap-1">
            {['OPEN','PENDING','RESOLVED'].map((s) => (
              <button key={s} onClick={() => setFilter(s)}
                className={clsx('flex-1 text-xs py-1 rounded font-medium',
                  filter === s ? 'bg-sky-500 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200')}>
                {s === 'OPEN' ? 'Открытые' : s === 'PENDING' ? 'Ожидание' : 'Решено'}
              </button>
            ))}
          </div>
        </div>
        <div className="flex-1 overflow-y-auto divide-y divide-slate-100">
          {items.map((c: any) => (
            <button key={c.id} onClick={() => setSelectedId(c.id)}
              className={clsx('w-full p-4 text-left hover:bg-slate-50 transition-colors',
                selectedId === c.id && 'bg-sky-50 border-r-2 border-sky-500')}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium text-slate-800 truncate">
                  {c.user?.username ? `@${c.user.username}` : c.user?.firstName ?? 'Аноним'}
                </span>
                <span className="text-xs text-slate-400">
                  {c.lastMessageAt ? formatDistanceToNow(new Date(c.lastMessageAt), { locale: ru, addSuffix: true }) : ''}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-xs text-slate-500 truncate">{c.bot?.username ? `@${c.bot.username}` : c.bot?.firstName}</span>
                <span className={clsx('text-xs px-1.5 py-0.5 rounded font-medium', STATUS_COLORS[c.status] ?? '')}>{c.status}</span>
              </div>
              {c.messages?.[0] && (
                <p className="text-xs text-slate-400 mt-1 truncate">{c.messages[0].text}</p>
              )}
            </button>
          ))}
          {items.length === 0 && (
            <div className="p-8 text-center text-slate-400 text-sm">Нет диалогов</div>
          )}
        </div>
      </div>

      {/* Chat area */}
      {selectedId && conv ? (
        <div className="flex-1 flex flex-col">
          {/* Header */}
          <div className="p-4 border-b border-slate-200 bg-white flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 bg-sky-100 rounded-full flex items-center justify-center">
                <User size={16} className="text-sky-600" />
              </div>
              <div>
                <p className="font-medium text-slate-800 text-sm">
                  {conv.user?.username ? `@${conv.user.username}` : conv.user?.firstName ?? 'Аноним'}
                </p>
                <p className="text-xs text-slate-400">{conv.bot?.username ? `@${conv.bot.username}` : conv.bot?.firstName}</p>
              </div>
            </div>
            <div className="flex gap-2">
              <button onClick={() => resolve.mutate()}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-green-50 text-green-700 text-xs font-medium rounded-lg hover:bg-green-100">
                <CheckCircle size={13} /> Закрыть
              </button>
            </div>
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto p-4 space-y-3">
            {(conv.messages ?? []).map((m: any) => (
              <div key={m.id} className={clsx('flex', m.direction === 'OUTBOUND' ? 'justify-end' : 'justify-start')}>
                <div className={clsx(
                  'max-w-xs lg:max-w-md px-4 py-2.5 rounded-2xl text-sm',
                  m.direction === 'OUTBOUND'
                    ? 'bg-sky-500 text-white rounded-tr-sm'
                    : 'bg-white border border-slate-200 text-slate-800 rounded-tl-sm'
                )}>
                  {m.text && <p className="whitespace-pre-wrap break-words">{m.text}</p>}
                  {!m.text && m.type && <p className="italic opacity-70">[{m.type}]</p>}
                  <p className={clsx('text-xs mt-1', m.direction === 'OUTBOUND' ? 'text-sky-200' : 'text-slate-400')}>
                    {new Date(m.sentAt).toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' })}
                    {m.direction === 'OUTBOUND' && <span> · {m.senderType === 'OPERATOR' ? 'Оператор' : 'Бот'}</span>}
                  </p>
                </div>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>

          {/* Input */}
          <div className="p-4 border-t border-slate-200 bg-white">
            <div className="flex gap-2">
              <textarea
                value={text} onChange={e => setText(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (text.trim()) sendMsg.mutate(text.trim()); } }}
                placeholder="Напишите сообщение... (Enter — отправить)"
                rows={2}
                className="flex-1 px-4 py-2.5 border border-slate-200 rounded-xl text-sm outline-none focus:border-sky-500 resize-none"
              />
              <button onClick={() => text.trim() && sendMsg.mutate(text.trim())} disabled={!text.trim() || sendMsg.isPending}
                className="px-4 bg-sky-500 hover:bg-sky-600 text-white rounded-xl flex items-center justify-center disabled:opacity-40 transition-colors">
                <Send size={16} />
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="flex-1 flex items-center justify-center text-slate-400">
          <div className="text-center">
            <MessageSquare size={40} className="mx-auto mb-3 opacity-30" />
            <p className="text-sm">Выберите диалог</p>
          </div>
        </div>
      )}
    </div>
  );
}
