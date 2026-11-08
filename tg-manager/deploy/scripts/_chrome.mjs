// Где лежат Chromium И PLAYWRIGHT для проверок мини-аппа.
//
// Список путей жил в ЧЕТЫРЁХ копиях, а две проверки — обход вкладок модулей и
// рабочее место виртуального админа — его не имели вовсе и полагались только
// на CHROME_BIN. В контейнере эта переменная не задана, поэтому Playwright шёл
// в свой кэш загрузок, которого тут нет, и падал на «Looks like Playwright was
// just installed». В CI переменная есть, значит обе проверки были зелёными там
// и НЕ ЗАПУСКАЛИСЬ локально: цепочка проверки перед пушем молча короче, чем
// кажется, а узнаёшь об этом из красного прогона.
import { createRequire } from 'module';

const require = createRequire(import.meta.url);
const fs = require('fs');

// Путь к Playwright жил в ЧЕТЫРЁХ копиях — тем же способом, каким до этого
// файла жил список путей к Chromium, — и в пятом месте
// (`check_va_workspace.mjs`) его не было вовсе: там стояло
// `await import('playwright')`, то есть проверка рабочего места виртуального
// админа локально не запускалась НИКОГДА без заданной переменной. Теперь
// список один.
// Найденный пакет импортируем по ПОЛНОМУ пути, а не по имени: require.resolve
// видит пакет через NODE_PATH, а import('playwright') — нет (ESM NODE_PATH не
// читает). В контейнере с глобальным NODE_PATH проверка падала на
// ERR_MODULE_NOT_FOUND, хотя Playwright установлен.
export const PLAYWRIGHT = process.env.PLAYWRIGHT_JS
  || (p => { try { return require.resolve(p); } catch { return p; } })(
       ['playwright', '/opt/node22/lib/node_modules/playwright/index.mjs'].find(p => {
         try { require.resolve(p); return true; } catch { return p.startsWith('/'); } }));

// Готовый chromium: Playwright отдаёт его то из `chromium`, то из
// `default.chromium` — в зависимости от того, как разрешился путь.
export async function chromiumOf() {
  const m = await import(PLAYWRIGHT);
  return m.chromium || (m.default && m.default.chromium);
}

export const CHROME = process.env.CHROME_BIN || [
  '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
  '/opt/pw-browsers/chromium/chrome-linux/chrome',
].find(p => { try { fs.accessSync(p); return true; } catch { return false; } });

// Готовые аргументы для chromium.launch(): пустой объект, если путь не нашёлся,
// — тогда Playwright пробует свой кэш, как и раньше.
export const LAUNCH = CHROME ? { executablePath: CHROME } : {};
