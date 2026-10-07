'use client';
import { cn } from './cn';

export function Card({
  className,
  interactive,
  ...rest
}: React.HTMLAttributes<HTMLDivElement> & { interactive?: boolean }) {
  return (
    <div
      className={cn(
        'rounded-2xl border border-line bg-surface shadow-sm',
        interactive &&
          'transition-all duration-150 hover:shadow-md hover:border-line-strong active:scale-[0.995] cursor-pointer',
        className,
      )}
      {...rest}
    />
  );
}

export function CardHeader({
  title,
  subtitle,
  icon,
  action,
  className,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  icon?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('flex items-center gap-3 px-4 py-3.5 border-b border-line', className)}>
      {icon && (
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-accent-weak text-accent">
          {icon}
        </span>
      )}
      <div className="min-w-0 flex-1">
        <h2 className="truncate text-sm font-semibold text-fg">{title}</h2>
        {subtitle && <p className="truncate text-xs text-fg-hint">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}
