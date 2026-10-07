'use client';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import Link from 'next/link';
import { authApi } from '@/lib/api';
import { Users, Search, Clock, Tag, ChevronRight } from 'lucide-react';
import { PageHeader, Card, Stat, Badge, Avatar, Input, EmptyState, type Tone } from '@/components/ui';

interface UserTag {
  tagId: string;
  tag?: { name: string };
}
interface UserItem {
  id: string;
  telegramId: string;
  username: string | null;
  firstName: string;
  lastName: string | null;
  language?: string | null;
  languageCode?: string | null;
  tags?: string[];
  userTags?: UserTag[];
  lastSeen?: string;
  lastSeenAt?: string;
}

const MOCK: UserItem[] = [
  { id: '1', telegramId: '123456789', username: '@aleksey_m', firstName: 'Алексей', lastName: 'Михайлов', language: 'ru', tags: ['vip', 'buyer'], lastSeen: new Date().toISOString() },
  { id: '2', telegramId: '987654321', username: '@natasha_k', firstName: 'Наталья', lastName: 'Кузнецова', language: 'ru', tags: ['buyer'], lastSeen: new Date(Date.now() - 3.6e6).toISOString() },
  { id: '3', telegramId: '555000111', username: null, firstName: 'John', lastName: 'Smith', language: 'en', tags: [], lastSeen: new Date(Date.now() - 8.64e7).toISOString() },
];

const TAG_TONE: Record<string, Tone> = { vip: 'warning', buyer: 'success', lead: 'accent', support: 'violet', blocked: 'danger' };

const getTags = (u: UserItem) => u.tags?.length ? u.tags : u.userTags?.map((t) => t.tag?.name ?? t.tagId) ?? [];
const getLastSeen = (u: UserItem) => u.lastSeen ?? u.lastSeenAt ?? '';

function ago(iso: string) {
  if (!iso) return '—';
  const min = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (min < 1) return 'только что';
  if (min < 60) return `${min} мин`;
  if (min < 1440) return `${Math.floor(min / 60)} ч`;
  return `${Math.floor(min / 1440)} дн`;
}

export default function UsersPage() {
  const [search, setSearch] = useState('');
  const { data, isError } = useQuery<{ items: UserItem[] } | UserItem[]>({
    queryKey: ['users'],
    queryFn: () => authApi.get('/users?limit=200'),
  });

  const users: UserItem[] = isError || !data ? MOCK : Array.isArray(data) ? data : data.items ?? MOCK;

  const q = search.trim().toLowerCase();
  const filtered = useMemo(
    () =>
      users.filter((u) =>
        !q ? true : `${u.firstName ?? ''} ${u.lastName ?? ''} ${u.username ?? ''}`.toLowerCase().includes(q),
      ),
    [users, q],
  );

  const active = users.filter((u) => Date.now() - new Date(getLastSeen(u)).getTime() < 7 * 8.64e7).length;
  const withTags = users.filter((u) => getTags(u).length > 0).length;

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader title="Пользователи" subtitle="Аудитория ваших ботов" />

      <div className="grid grid-cols-3 gap-3">
        <Stat label="Всего" value={users.length} icon={<Users size={17} />} tone="accent" />
        <Stat label="Активных 7д" value={active} icon={<Clock size={17} />} tone="success" />
        <Stat label="С тегами" value={withTags} icon={<Tag size={17} />} tone="violet" />
      </div>

      <div className="relative">
        <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-fg-hint" />
        <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Поиск по имени или username…" className="pl-9" />
      </div>

      {filtered.length === 0 ? (
        <EmptyState icon={<Users size={26} />} title="Нет пользователей" description={search ? 'Ничего не найдено по запросу.' : 'Пользователи появятся после первых диалогов.'} />
      ) : (
        <div className="space-y-2.5">
          {filtered.map((u) => {
            const name = [u.firstName, u.lastName].filter(Boolean).join(' ') || u.username || '—';
            const uname = u.username ? (u.username.startsWith('@') ? u.username : `@${u.username}`) : null;
            const tags = getTags(u);
            return (
              <Link key={u.id} href={`/users/${u.id}`}>
                <Card interactive className="flex items-center gap-3 p-3.5">
                  <Avatar name={name} size={42} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <p className="truncate text-sm font-semibold text-fg">{name}</p>
                      <span className="text-2xs uppercase text-fg-hint">{u.language ?? u.languageCode ?? ''}</span>
                    </div>
                    <p className="truncate text-xs text-fg-hint">
                      {uname ?? `ID ${u.telegramId}`} · {ago(getLastSeen(u))} назад
                    </p>
                    {tags.length > 0 && (
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {tags.map((t) => (
                          <Badge key={t} tone={TAG_TONE[t.toLowerCase()] ?? 'neutral'}>
                            {t}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </div>
                  <ChevronRight size={18} className="shrink-0 text-fg-hint" />
                </Card>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
