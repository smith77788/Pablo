'use client';
import { useMemo, useState } from 'react';
import { Search, Plus, TrendingUp, TrendingDown, Minus } from 'lucide-react';
import {
  PageHeader,
  Button,
  Card,
  Badge,
  Sheet,
  Input,
  Select,
  Label,
  Segmented,
  EmptyState,
  useToast,
} from '@/components/ui';

interface Keyword {
  id: string;
  keyword: string;
  language: string;
  group: string;
  position: number;
  delta: number;
  lastChecked: string;
}

const INITIAL: Keyword[] = [
  { id: '1', keyword: 'telegram каналы каталог', language: 'RU', group: 'Каталог', position: 2, delta: 3, lastChecked: '1 ч' },
  { id: '2', keyword: 'telegram прокси', language: 'RU', group: 'Прокси', position: 1, delta: 5, lastChecked: '1 ч' },
  { id: '3', keyword: 'telegram bot api python', language: 'EN', group: 'API', position: 3, delta: 2, lastChecked: '2 ч' },
  { id: '4', keyword: 'telegram channel analytics', language: 'EN', group: 'Аналитика', position: 4, delta: -1, lastChecked: '2 ч' },
  { id: '5', keyword: 'telegram automation', language: 'EN', group: 'Автоматизация', position: 2, delta: 6, lastChecked: '3 ч' },
  { id: '6', keyword: 'telegram crm', language: 'RU', group: 'CRM', position: 8, delta: -3, lastChecked: '3 ч' },
  { id: '7', keyword: 'telegram рассылка сервис', language: 'RU', group: 'Рассылки', position: 12, delta: -5, lastChecked: '4 ч' },
  { id: '8', keyword: 'telegram scheduler', language: 'EN', group: 'Планировщик', position: 15, delta: 0, lastChecked: '5 ч' },
];

function Delta({ delta }: { delta: number }) {
  if (delta > 0)
    return (
      <span className="flex items-center gap-0.5 text-xs font-semibold text-success">
        <TrendingUp size={12} /> +{delta}
      </span>
    );
  if (delta < 0)
    return (
      <span className="flex items-center gap-0.5 text-xs font-semibold text-danger">
        <TrendingDown size={12} /> {delta}
      </span>
    );
  return (
    <span className="flex items-center gap-0.5 text-xs text-fg-hint">
      <Minus size={12} /> 0
    </span>
  );
}

export default function KeywordsPage() {
  const toast = useToast();
  const [list, setList] = useState<Keyword[]>(INITIAL);
  const [adding, setAdding] = useState(false);
  const [search, setSearch] = useState('');
  const [lang, setLang] = useState<'ALL' | 'RU' | 'EN' | 'DE' | 'UA'>('ALL');
  const [form, setForm] = useState({ keyword: '', language: 'RU', group: '' });

  const filtered = useMemo(
    () =>
      list.filter(
        (k) =>
          (lang === 'ALL' || k.language === lang) &&
          (!search || k.keyword.toLowerCase().includes(search.toLowerCase())),
      ),
    [list, lang, search],
  );

  function add() {
    if (!form.keyword) return;
    setList((p) => [
      {
        id: String(Date.now()),
        keyword: form.keyword,
        language: form.language,
        group: form.group || '—',
        position: 0,
        delta: 0,
        lastChecked: 'только что',
      },
      ...p,
    ]);
    setForm({ keyword: '', language: 'RU', group: '' });
    setAdding(false);
    toast('Ключевое слово добавлено в отслеживание', 'success');
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader
        title="Ключевые слова"
        subtitle="Отслеживание позиций в поиске"
        action={
          <Button onClick={() => setAdding(true)}>
            <Plus size={16} /> Добавить
          </Button>
        }
      />

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-fg-hint" />
          <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Поиск…" className="pl-9" />
        </div>
        <Segmented
          value={lang}
          onChange={setLang}
          options={[
            { value: 'ALL', label: 'Все' },
            { value: 'RU', label: 'RU' },
            { value: 'EN', label: 'EN' },
            { value: 'DE', label: 'DE' },
          ]}
        />
      </div>

      {filtered.length === 0 ? (
        <EmptyState icon={<Search size={26} />} title="Ничего не найдено" description="Измените запрос или добавьте новое ключевое слово." />
      ) : (
        <div className="space-y-2.5">
          {filtered.map((kw) => (
            <Card key={kw.id} className="flex items-center gap-3 p-3.5">
              <span
                className="flex h-9 w-11 shrink-0 items-center justify-center rounded-lg text-sm font-bold tabular-nums"
                style={{
                  background: kw.position && kw.position <= 3 ? 'var(--success-weak)' : 'var(--surface-2)',
                  color: kw.position && kw.position <= 3 ? 'var(--success)' : 'var(--fg-muted)',
                }}
              >
                {kw.position ? `#${kw.position}` : '—'}
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-semibold text-fg">{kw.keyword}</p>
                <div className="mt-1 flex items-center gap-2 text-2xs text-fg-hint">
                  <Badge tone="neutral">{kw.language}</Badge>
                  <span>{kw.group}</span>
                  <span>· {kw.lastChecked}</span>
                </div>
              </div>
              <Delta delta={kw.delta} />
            </Card>
          ))}
        </div>
      )}

      <Sheet
        open={adding}
        onClose={() => setAdding(false)}
        title="Новое ключевое слово"
        footer={
          <>
            <Button variant="secondary" onClick={() => setAdding(false)}>
              Отмена
            </Button>
            <Button onClick={add} disabled={!form.keyword}>
              Добавить
            </Button>
          </>
        }
      >
        <div className="space-y-3 py-1">
          <div>
            <Label>Ключевое слово</Label>
            <Input value={form.keyword} onChange={(e) => setForm((f) => ({ ...f, keyword: e.target.value }))} placeholder="telegram bot api" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Язык</Label>
              <Select value={form.language} onChange={(e) => setForm((f) => ({ ...f, language: e.target.value }))}>
                <option value="RU">RU</option>
                <option value="EN">EN</option>
                <option value="DE">DE</option>
                <option value="UA">UA</option>
              </Select>
            </div>
            <div>
              <Label>Группа</Label>
              <Input value={form.group} onChange={(e) => setForm((f) => ({ ...f, group: e.target.value }))} placeholder="Каталог" />
            </div>
          </div>
        </div>
      </Sheet>
    </div>
  );
}
