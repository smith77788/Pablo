'use client';
import { useState } from 'react';
import { BarChart2, TrendingUp } from 'lucide-react';
import { PageHeader, Card, Stat, Segmented } from '@/components/ui';

const SERIES: Record<string, number[]> = {
  '7д': [62, 64, 61, 68, 72, 70, 75],
  '30д': [48, 52, 50, 55, 58, 61, 59, 63, 66, 64, 68, 72, 75],
  '90д': [30, 34, 38, 42, 40, 46, 50, 55, 58, 62, 66, 70, 75],
};

const LABELS: Record<string, string[]> = {
  '7д': ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'],
  '30д': ['', '', '', '', '', '', '', '', '', '', '', '', ''],
  '90д': ['', '', '', '', '', '', '', '', '', '', '', '', ''],
};

function AreaChart({ data, labels }: { data: number[]; labels: string[] }) {
  const w = 640;
  const h = 200;
  const pad = 16;
  const max = Math.max(...data) * 1.1;
  const min = Math.min(...data) * 0.85;
  const range = Math.max(1, max - min);
  const x = (i: number) => pad + (i / (data.length - 1)) * (w - pad * 2);
  const y = (v: number) => h - pad - ((v - min) / range) * (h - pad * 2);
  const line = data.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const area = `${pad},${h - pad} ${line} ${w - pad},${h - pad}`;

  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-48 w-full" preserveAspectRatio="none">
      <defs>
        <linearGradient id="vg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.28" />
          <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
        </linearGradient>
      </defs>
      {[0.25, 0.5, 0.75].map((g) => (
        <line key={g} x1={pad} x2={w - pad} y1={pad + g * (h - pad * 2)} y2={pad + g * (h - pad * 2)} stroke="var(--border)" strokeWidth={1} />
      ))}
      <polygon points={area} fill="url(#vg)" />
      <polyline points={line} fill="none" stroke="var(--accent)" strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round" />
      {data.map((v, i) => (
        <circle key={i} cx={x(i)} cy={y(v)} r={i === data.length - 1 ? 4 : 0} fill="var(--accent)" />
      ))}
    </svg>
  );
}

export default function TrendsPage() {
  const [period, setPeriod] = useState<'7д' | '30д' | '90д'>('7д');
  const data = SERIES[period];
  const current = data[data.length - 1];
  const first = data[0];
  const growth = Math.round(((current - first) / first) * 100);

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader
        title="Тренды"
        subtitle="Индекс видимости вашей сети во времени"
        action={
          <Segmented
            value={period}
            onChange={setPeriod}
            options={[
              { value: '7д', label: '7 дней' },
              { value: '30д', label: '30 дней' },
              { value: '90д', label: '90 дней' },
            ]}
          />
        }
      />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Stat label="Индекс сейчас" value={current} icon={<BarChart2 size={17} />} tone="accent" />
        <Stat label={`Рост за ${period}`} value={`${growth >= 0 ? '+' : ''}${growth}%`} icon={<TrendingUp size={17} />} tone={growth >= 0 ? 'success' : 'danger'} />
        <Stat label="Пик" value={Math.max(...data)} tone="violet" className="col-span-2 sm:col-span-1" />
      </div>

      <Card className="p-4">
        <div className="mb-3 flex items-center gap-2">
          <TrendingUp size={16} className="text-accent" />
          <h2 className="text-sm font-semibold text-fg">Индекс видимости</h2>
        </div>
        <AreaChart data={data} labels={LABELS[period]} />
        {period === '7д' && (
          <div className="mt-1 flex justify-between px-2 text-2xs text-fg-hint">
            {LABELS['7д'].map((l, i) => (
              <span key={i}>{l}</span>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
