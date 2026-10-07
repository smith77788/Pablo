'use client';
import { cn } from './cn';

const PALETTE = [
  ['#2f8fed', '#1e6fd0'],
  ['#8b5cf6', '#6d3fd6'],
  ['#16a34a', '#0f7d38'],
  ['#d97706', '#b45309'],
  ['#e5484d', '#c2363b'],
  ['#0ea5e9', '#0284c7'],
];

function hash(s: string) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h << 5) - h + s.charCodeAt(i);
  return Math.abs(h);
}

export function Avatar({
  name,
  size = 40,
  className,
}: {
  name: string;
  size?: number;
  className?: string;
}) {
  const clean = name.replace(/^@/, '').trim() || '?';
  const initials = clean.slice(0, 2).toUpperCase();
  const [from, to] = PALETTE[hash(clean) % PALETTE.length];
  return (
    <span
      className={cn('inline-flex shrink-0 items-center justify-center rounded-full font-semibold text-white', className)}
      style={{
        width: size,
        height: size,
        fontSize: size * 0.36,
        background: `linear-gradient(135deg, ${from}, ${to})`,
      }}
    >
      {initials}
    </span>
  );
}
