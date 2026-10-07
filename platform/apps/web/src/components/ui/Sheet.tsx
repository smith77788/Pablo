'use client';
import { useEffect } from 'react';
import { X } from 'lucide-react';
import { cn } from './cn';

/**
 * Mobile-first modal: slides up as a bottom sheet on phones, becomes a
 * centered dialog on larger screens. Used for all create/edit/confirm flows.
 */
export function Sheet({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
}: {
  open: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  description?: React.ReactNode;
  children?: React.ReactNode;
  footer?: React.ReactNode;
  size?: 'sm' | 'md' | 'lg';
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('keydown', onKey);
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = '';
    };
  }, [open, onClose]);

  if (!open) return null;

  const maxW = size === 'sm' ? 'sm:max-w-sm' : size === 'lg' ? 'sm:max-w-2xl' : 'sm:max-w-lg';

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center sm:items-center">
      <div className="absolute inset-0 bg-black/50 animate-fade-in" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        className={cn(
          'relative w-full rounded-t-3xl bg-surface shadow-lg',
          'max-h-[92vh] overflow-hidden flex flex-col',
          'animate-sheet-up sm:animate-pop-in sm:rounded-3xl sm:w-full',
          maxW,
        )}
      >
        {/* drag handle (mobile affordance) */}
        <div className="pt-2.5 pb-1 flex justify-center sm:hidden">
          <span className="h-1.5 w-10 rounded-full bg-line-strong" />
        </div>

        {(title || description) && (
          <div className="flex items-start gap-3 px-5 pt-2 pb-3 sm:pt-5">
            <div className="min-w-0 flex-1">
              {title && <h2 className="text-base font-semibold text-fg">{title}</h2>}
              {description && <p className="mt-0.5 text-sm text-fg-muted">{description}</p>}
            </div>
            <button
              onClick={onClose}
              className="-mr-1 rounded-lg p-1.5 text-fg-hint hover:bg-surface-2 hover:text-fg"
              aria-label="Закрыть"
            >
              <X size={18} />
            </button>
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-2">{children}</div>

        {footer && (
          <div className="border-t border-line px-5 py-3.5 pb-safe flex gap-2.5 justify-end">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
