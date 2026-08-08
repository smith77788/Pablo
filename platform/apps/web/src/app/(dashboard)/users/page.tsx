'use client';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { authApi } from '@/lib/api';
import { Users, Search, Clock, Tag, ExternalLink } from 'lucide-react';
import Link from 'next/link';

interface UserTag {
  tagId: string;
  tag?: { name: string; color: string };
}

interface UserItem {
  id: string;
  telegramId: string;
  username: string | null;
  firstName: string;
  lastName: string | null;
  language: string | null;
  languageCode?: string | null;
  tags: string[];
  userTags?: UserTag[];
  lastSeen: string;
  lastSeenAt?: string;
}

const MOCK_USERS: UserItem[] = [
  {
    id: '1',
    telegramId: '123456789',
    username: '@aleksey_m',
    firstName: 'Алексей',
    lastName: 'Михайлов',
    language: 'ru',
    tags: ['vip', 'buyer'],
    lastSeen: new Date().toISOString(),
  },
  {
    id: '2',
    telegramId: '987654321',
    username: '@natasha_k',
    firstName: 'Наталья',
    lastName: 'Кузнецова',
    language: 'ru',
    tags: ['buyer'],
    lastSeen: new Date(Date.now() - 3600000).toISOString(),
  },
  {
    id: '3',
    telegramId: '555000111',
    username: null,
    firstName: 'John',
    lastName: 'Smith',
    language: 'en',
    tags: [],
    lastSeen: new Date(Date.now() - 86400000).toISOString(),
  },
];

// Deterministic palette for tag names
const TAG_COLORS: Record<string, { bg: string; text: string }> = {
  vip:     { bg: 'bg-amber-100',  text: 'text-amber-700' },
  buyer:   { bg: 'bg-emerald-100', text: 'text-emerald-700' },
  lead:    { bg: 'bg-sky-100',    text: 'text-sky-700' },
  support: { bg: 'bg-violet-100', text: 'text-violet-700' },
  blocked: { bg: 'bg-red-100',    text: 'text-red-600' },
};

const TAG_FALLBACK_PALETTE = [
  { bg: 'bg-blue-100',   text: 'text-blue-700' },
  { bg: 'bg-pink-100',   text: 'text-pink-700' },
  { bg: 'bg-teal-100',   text: 'text-teal-700' },
  { bg: 'bg-orange-100', text: 'text-orange-700' },
  { bg: 'bg-indigo-100', text: 'text-indigo-700' },
];

function tagColor(name: string): { bg: string; text: string } {
  if (TAG_COLORS[name.toLowerCase()]) return TAG_COLORS[name.toLowerCase()];
  // stable hash → palette index
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) & 0xffff;
  return TAG_FALLBACK_PALETTE[hash % TAG_FALLBACK_PALETTE.length];
}

function formatLastSeen(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const diffMin = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);
  if (diffMin < 1) return 'только что';
  if (diffMin < 60) return `${diffMin} мин назад`;
  if (diffHours < 24) return `${diffHours} ч назад`;
  return `${diffDays} дн назад`;
}

function isActive7d(iso: string): boolean {
  return Date.now() - new Date(iso).getTime() < 7 * 86400000;
}

function getUserTags(user: UserItem): string[] {
  if (user.tags && user.tags.length > 0) return user.tags;
  if (user.userTags && user.userTags.length > 0)
    return user.userTags.map((t) => t.tag?.name ?? t.tagId);
  return [];
}

function getUserLastSeen(user: UserItem): string {
  return user.lastSeen ?? user.lastSeenAt ?? '';
}

export default function UsersPage() {
  const [search, setSearch] = useState('');

  const { data, isError } = useQuery<{ items: UserItem[]; total: number } | UserItem[]>({
    queryKey: ['users'],
    queryFn: () => authApi.get('/users?limit=200'),
  });

  let rawUsers: UserItem[];
  if (isError || !data) {
    rawUsers = MOCK_USERS;
  } else if (Array.isArray(data)) {
    rawUsers = data;
  } else {
    rawUsers = (data as { items: UserItem[] }).items ?? MOCK_USERS;
  }

  const useMock = isError || !data;

  const q = search.trim().toLowerCase();
  const filtered = rawUsers.filter((u) => {
    if (!q) return true;
    const full = `${u.firstName ?? ''} ${u.lastName ?? ''} ${u.username ?? ''}`.toLowerCase();
    return full.includes(q);
  });

  // Metrics
  const totalCount = rawUsers.length;
  const activeCount = rawUsers.filter((u) => isActive7d(getUserLastSeen(u))).length;
  const withTagsCount = rawUsers.filter((u) => getUserTags(u).length > 0).length;

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-xl font-bold text-slate-800">👤 Пользователи</h1>
        <p className="text-sm text-slate-400 mt-0.5">Telegram-пользователи вашего тенанта</p>
      </div>

      {/* Mock data notice */}
      {useMock && (
        <div className="bg-orange-50 border border-orange-200 rounded-lg px-4 py-2 text-sm text-orange-600">
          API недоступен — показаны демо-данные
        </div>
      )}

      {/* Metric cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="bg-white rounded-xl border border-slate-200 p-5 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="text-sm text-slate-500 font-medium">Всего пользователей</span>
            <span className="w-9 h-9 rounded-lg flex items-center justify-center bg-sky-50 text-sky-600">
              <Users size={17} />
            </span>
          </div>
          <p className="text-3xl font-bold text-slate-800">{totalCount}</p>
        </div>

        <div className="bg-white rounded-xl border border-slate-200 p-5 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="text-sm text-slate-500 font-medium">Активных (7 дней)</span>
            <span className="w-9 h-9 rounded-lg flex items-center justify-center bg-green-50 text-green-600">
              <Clock size={17} />
            </span>
          </div>
          <p className="text-3xl font-bold text-slate-800">{activeCount}</p>
        </div>

        <div className="bg-white rounded-xl border border-slate-200 p-5 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="text-sm text-slate-500 font-medium">С тегами</span>
            <span className="w-9 h-9 rounded-lg flex items-center justify-center bg-violet-50 text-violet-600">
              <Tag size={17} />
            </span>
          </div>
          <p className="text-3xl font-bold text-slate-800">{withTagsCount}</p>
        </div>
      </div>

      {/* Search */}
      <div className="relative">
        <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Поиск по имени или username..."
          className="w-full pl-9 pr-4 py-2.5 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500 bg-white"
        />
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
        {/* Table header */}
        <div className="grid grid-cols-[2fr_1.2fr_0.6fr_1.4fr_1.1fr_auto] gap-4 px-5 py-3 border-b border-slate-100 text-xs font-medium text-slate-400 uppercase tracking-wide">
          <span>Пользователь</span>
          <span>Telegram ID</span>
          <span>Язык</span>
          <span>Теги</span>
          <span>Последняя активность</span>
          <span></span>
        </div>

        {/* Table rows */}
        <div className="divide-y divide-slate-50">
          {filtered.map((user) => {
            const tags = getUserTags(user);
            const lastSeen = getUserLastSeen(user);
            const initials = (
              (user.firstName?.[0] ?? user.username?.[1] ?? '?')
            ).toUpperCase();
            const displayName = [user.firstName, user.lastName].filter(Boolean).join(' ') || user.username || '—';
            const displayUsername = user.username
              ? user.username.startsWith('@') ? user.username : `@${user.username}`
              : null;

            return (
              <div
                key={user.id}
                className="grid grid-cols-[2fr_1.2fr_0.6fr_1.4fr_1.1fr_auto] gap-4 px-5 py-3.5 items-center hover:bg-slate-50 transition-colors"
              >
                {/* User */}
                <div className="flex items-center gap-2.5 min-w-0">
                  <div className="w-8 h-8 bg-sky-100 rounded-full flex items-center justify-center text-xs font-semibold text-sky-700 shrink-0">
                    {initials}
                  </div>
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-slate-800 truncate">{displayName}</p>
                    {displayUsername && (
                      <p className="text-xs text-slate-400 truncate">{displayUsername}</p>
                    )}
                  </div>
                </div>

                {/* Telegram ID */}
                <span className="text-sm text-slate-500 font-mono truncate">{user.telegramId}</span>

                {/* Language */}
                <span className="text-sm text-slate-500 uppercase">
                  {user.language ?? user.languageCode ?? '—'}
                </span>

                {/* Tags */}
                <div className="flex flex-wrap gap-1">
                  {tags.length > 0 ? (
                    tags.map((tag) => {
                      const { bg, text } = tagColor(tag);
                      return (
                        <span
                          key={tag}
                          className={`text-xs px-2 py-0.5 rounded-full font-medium ${bg} ${text}`}
                        >
                          {tag}
                        </span>
                      );
                    })
                  ) : (
                    <span className="text-xs text-slate-300">—</span>
                  )}
                </div>

                {/* Last seen */}
                <span className="text-xs text-slate-400 whitespace-nowrap">
                  {lastSeen ? formatLastSeen(lastSeen) : '—'}
                </span>

                {/* Action */}
                <Link
                  href={`/users/${user.id}`}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs text-sky-600 font-medium bg-sky-50 rounded-lg hover:bg-sky-100 transition-colors whitespace-nowrap shrink-0"
                >
                  <ExternalLink size={13} /> Открыть
                </Link>
              </div>
            );
          })}

          {filtered.length === 0 && (
            <div className="text-center py-12 text-slate-400">
              <Users size={36} className="mx-auto mb-3 opacity-30" />
              <p className="text-sm">
                {search ? 'Нет пользователей по вашему запросу' : 'Нет пользователей'}
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
