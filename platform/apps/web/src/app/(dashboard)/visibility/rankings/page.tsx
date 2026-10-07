'use client';
import { TrendingUp, TrendingDown } from 'lucide-react';
import { PageHeader, Card, Badge } from '@/components/ui';

interface Row {
  keyword: string;
  position: number;
  delta: number;
  history: number[];
}

const ROWS: Row[] = [
  { keyword: 'telegram прокси', position: 1, delta: 5, history: [6, 5, 4, 3, 2, 1, 1] },
  { keyword: 'telegram каналы каталог', position: 2, delta: 3, history: [5, 5, 4, 4, 3, 2, 2] },
  { keyword: 'telegram bot api', position: 3, delta: 2, history: [5, 4, 4, 3, 3, 3, 3] },
  { keyword: 'telegram automation', position: 2, delta: 6, history: [8, 7, 6, 5, 4, 3, 2] },
  { keyword: 'telegram crm', position: 8, delta: -3, history: [5, 5, 6, 6, 7, 8, 8] },
  { keyword: 'telegram рассылка', position: 12, delta: -5, history: [7, 8, 9, 10, 11, 12, 12] },
];

/** Tiny inline sparkline: lower position = higher line. */
function Spark({ data }: { data: number[] }) {
  const max = Math.max(...data);
  const min = Math.min(...data);
  const range = Math.max(1, max - min);
  const w = 72;
  const h = 26;
  const pts = data
    .map((v, i) => {
      const x = (i / (data.length - 1)) * w;
      const y = ((v - min) / range) * (h - 4) + 2; // higher position number → lower on screen
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  const rising = data[data.length - 1] <= data[0];
  return (
    <svg width={w} height={h} className="shrink-0">
      <polyline
        points={pts}
        fill="none"
        stroke={rising ? 'var(--success)' : 'var(--danger)'}
        strokeWidth={1.75}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default function RankingsPage() {
  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader title="Позиции" subtitle="История и динамика позиций за 7 дней" />

      <Card>
        <div className="divide-y divide-line">
          {ROWS.map((r) => (
            <div key={r.keyword} className="flex items-center gap-3 px-4 py-3.5">
              <span
                className="flex h-9 w-11 shrink-0 items-center justify-center rounded-lg text-sm font-bold tabular-nums"
                style={{
                  background: r.position <= 3 ? 'var(--success-weak)' : 'var(--surface-2)',
                  color: r.position <= 3 ? 'var(--success)' : 'var(--fg-muted)',
                }}
              >
                #{r.position}
              </span>
              <p className="min-w-0 flex-1 truncate text-sm font-medium text-fg">{r.keyword}</p>
              <Spark data={r.history} />
              <Badge tone={r.delta >= 0 ? 'success' : 'danger'}>
                {r.delta >= 0 ? <TrendingUp size={11} /> : <TrendingDown size={11} />}
                {r.delta >= 0 ? `+${r.delta}` : r.delta}
              </Badge>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
