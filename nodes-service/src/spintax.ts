/**
 * Разбор spintax вида {{Node Server|Cluster Node}} и {a|b|c}.
 *
 * Выбирает один статический вариант. Детерминированный при заданном seed
 * (для воспроизводимости), иначе — криптослучайный выбор.
 */
import { randomInt } from "crypto";

const GROUP_RE = /\{\{([^{}]*)\}\}|\{([^{}]*)\}/g;

/**
 * Простой mulberry32 PRNG для детерминированного выбора при заданном seed.
 */
function seededPicker(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Разворачивает spintax в один вариант.
 * @param raw шаблон
 * @param seed опциональный seed для детерминизма
 */
export function pickSpintax(raw: string, seed?: number): string {
  if (!raw) return "";
  const rnd = seed === undefined ? undefined : seededPicker(seed);
  const pick = (options: string[]): string => {
    if (options.length === 0) return "";
    if (options.length === 1) return options[0];
    const idx =
      rnd !== undefined
        ? Math.floor(rnd() * options.length)
        : randomInt(0, options.length);
    return options[idx];
  };

  // Разворачиваем внутренние группы, затем внешние — до стабилизации.
  let prev: string;
  let out = raw;
  let guard = 0;
  do {
    prev = out;
    out = out.replace(GROUP_RE, (_m, doubleBody, singleBody) => {
      const body = doubleBody !== undefined ? doubleBody : singleBody;
      const options = String(body)
        .split("|")
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
      return pick(options);
    });
    guard += 1;
  } while (out !== prev && guard < 20);

  return out.trim();
}
