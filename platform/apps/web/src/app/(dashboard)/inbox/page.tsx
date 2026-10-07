'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useState, useEffect, useRef } from 'react';
import { authApi } from '@/lib/api';
import { formatDistanceToNow } from 'date-fns';
import { ru } from 'date-fns/locale';
import { Send, ChevronLeft, CheckCircle, MessageSquare } from 'lucide-react';
import io from 'socket.io-client';
import { cn, Button, Avatar, Segmented, StatusBadge } from '@/components/ui';
import { haptic } from '@/lib/telegram';

export default function InboxPage() {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [text, setText] = useState('');
  const [filter, setFilter] = useState<'OPEN' | 'PENDING' | 'RESOLVED'>('OPEN');
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
    onSuccess: () => {
      setText('');
      qc.invalidateQueries({ queryKey: ['conversation', selectedId] });
    },
  });

  const resolve = useMutation({
    mutationFn: () => authApi.patch(`/conversations/${selectedId}/status`, { status: 'RESOLVED' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['conversations'] });
      setSelectedId(null);
    },
  });

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [conv?.messages]);

  useEffect(() => {
    const token = localStorage.getItem('token');
    if (!token) return;
    const socket = io(`${process.env.NEXT_PUBLIC_WS_URL}/inbox`, { auth: { token } });
    socket.on('message.new', () => {
      qc.invalidateQueries({ queryKey: ['conversations'] });
      if (selectedId) qc.invalidateQueries({ queryKey: ['conversation', selectedId] });
    });
    return () => {
      socket.disconnect();
    };
  }, [selectedId, qc]);

  const items = conversations?.items ?? [];

  return (
    <div className="flex h-[calc(100dvh-164px)] md:h-[calc(100dvh-100px)]">
      {/* Conversation list */}
      <div
        className={cn(
          'flex w-full flex-col border-r border-line bg-surface md:w-80',
          selectedId && 'hidden md:flex',
        )}
      >
        <div className="border-b border-line p-3">
          <Segmented
            value={filter}
            onChange={setFilter}
            className="w-full"
            options={[
              { value: 'OPEN', label: 'Открытые' },
              { value: 'PENDING', label: 'Ожидание' },
              { value: 'RESOLVED', label: 'Решено' },
            ]}
          />
        </div>
        <div className="flex-1 divide-y divide-line overflow-y-auto">
          {items.map((c: any) => (
            <button
              key={c.id}
              onClick={() => {
                haptic('select');
                setSelectedId(c.id);
              }}
              className={cn(
                'flex w-full items-center gap-3 p-3 text-left transition-colors hover:bg-surface-2',
                selectedId === c.id && 'bg-accent-weak',
              )}
            >
              <Avatar name={c.user?.username ?? c.user?.firstName ?? '?'} size={40} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-semibold text-fg">
                    {c.user?.username ? `@${c.user.username}` : c.user?.firstName ?? 'Аноним'}
                  </span>
                  <span className="shrink-0 text-2xs text-fg-hint">
                    {c.lastMessageAt ? formatDistanceToNow(new Date(c.lastMessageAt), { locale: ru }) : ''}
                  </span>
                </div>
                <p className="truncate text-xs text-fg-hint">{c.messages?.[0]?.text ?? c.bot?.username ?? ''}</p>
              </div>
            </button>
          ))}
          {items.length === 0 && (
            <div className="p-10 text-center text-sm text-fg-hint">Нет диалогов</div>
          )}
        </div>
      </div>

      {/* Chat area */}
      {selectedId && conv ? (
        <div className="flex flex-1 flex-col bg-bg">
          <div className="flex items-center gap-3 border-b border-line bg-surface p-3">
            <button onClick={() => setSelectedId(null)} className="rounded-lg p-1 text-fg-muted hover:bg-surface-2 md:hidden">
              <ChevronLeft size={20} />
            </button>
            <Avatar name={conv.user?.username ?? conv.user?.firstName ?? '?'} size={38} />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-semibold text-fg">
                {conv.user?.username ? `@${conv.user.username}` : conv.user?.firstName ?? 'Аноним'}
              </p>
              <p className="truncate text-xs text-fg-hint">{conv.bot?.username ? `@${conv.bot.username}` : conv.bot?.firstName}</p>
            </div>
            <Button size="sm" variant="success" onClick={() => resolve.mutate()}>
              <CheckCircle size={14} /> Закрыть
            </Button>
          </div>

          <div className="flex-1 space-y-2.5 overflow-y-auto p-4">
            {(conv.messages ?? []).map((m: any) => (
              <div key={m.id} className={cn('flex', m.direction === 'OUTBOUND' ? 'justify-end' : 'justify-start')}>
                <div
                  className={cn(
                    'max-w-[78%] rounded-2xl px-3.5 py-2 text-sm',
                    m.direction === 'OUTBOUND'
                      ? 'rounded-tr-sm bg-accent text-accent-fg'
                      : 'rounded-tl-sm border border-line bg-surface text-fg',
                  )}
                >
                  {m.text ? <p className="whitespace-pre-wrap break-words">{m.text}</p> : <p className="italic opacity-70">[{m.type}]</p>}
                  <p className={cn('mt-1 text-2xs', m.direction === 'OUTBOUND' ? 'text-accent-fg/70' : 'text-fg-hint')}>
                    {new Date(m.sentAt).toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' })}
                    {m.direction === 'OUTBOUND' && ` · ${m.senderType === 'OPERATOR' ? 'Оператор' : 'Бот'}`}
                  </p>
                </div>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>

          <div className="border-t border-line bg-surface p-3 pb-safe">
            <div className="flex items-end gap-2">
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    if (text.trim()) sendMsg.mutate(text.trim());
                  }
                }}
                placeholder="Сообщение… (Enter — отправить)"
                rows={1}
                className="max-h-32 min-h-[44px] flex-1 resize-none rounded-xl border border-line bg-surface-2 px-3.5 py-2.5 text-sm text-fg outline-none focus:border-accent focus:bg-surface focus:ring-2 focus:ring-[var(--ring)]"
              />
              <Button size="icon" onClick={() => text.trim() && sendMsg.mutate(text.trim())} disabled={!text.trim() || sendMsg.isPending} className="h-11 w-11">
                <Send size={17} />
              </Button>
            </div>
          </div>
        </div>
      ) : (
        <div className="hidden flex-1 items-center justify-center text-fg-hint md:flex">
          <div className="text-center">
            <MessageSquare size={40} className="mx-auto mb-3 opacity-30" />
            <p className="text-sm">Выберите диалог</p>
          </div>
        </div>
      )}
    </div>
  );
}
