import clsx, { type ClassValue } from 'clsx';

/** Tiny className combiner used across the UI kit. */
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs);
}
