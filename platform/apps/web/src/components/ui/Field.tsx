'use client';
import { forwardRef } from 'react';
import { cn } from './cn';

const base =
  'w-full rounded-xl border border-line bg-surface-2 px-3.5 text-sm text-fg placeholder:text-fg-hint ' +
  'outline-none transition-all focus:border-accent focus:ring-2 focus:ring-[var(--ring)] focus:bg-surface ' +
  'disabled:opacity-50';

export function Label({ className, ...rest }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label className={cn('mb-1.5 block text-xs font-medium text-fg-muted', className)} {...rest} />
  );
}

export const Input = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return <input ref={ref} className={cn(base, 'h-11', className)} {...rest} />;
  },
);

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ className, ...rest }, ref) {
  return <textarea ref={ref} className={cn(base, 'py-2.5 min-h-[96px] resize-y', className)} {...rest} />;
});

export const Select = forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, children, ...rest }, ref) {
    return (
      <select ref={ref} className={cn(base, 'h-11 appearance-none pr-9', className)} {...rest}>
        {children}
      </select>
    );
  },
);

export function FormRow({
  label,
  htmlFor,
  children,
  className,
}: {
  label: string;
  htmlFor?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
    </div>
  );
}
