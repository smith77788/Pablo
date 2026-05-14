/** 🔧 Code Fixer — Immune System | Patches bot.js and api.js based on code-level issues */
'use strict';

const { Agent, readFile, logAgent, tgSend } = require('./lib/base');
const path = require('path');
const fs   = require('fs');

const BOT_PATH = path.join(__dirname, '../bot.js');
const API_PATH = path.join(__dirname, '../routes/api.js');
const DB_PATH  = path.join(__dirname, '../database.js');

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** Count non-overlapping occurrences of needle in haystack */
function countOccurrences(haystack, needle) {
  let count = 0;
  let pos = 0;
  while ((pos = haystack.indexOf(needle, pos)) !== -1) { count++; pos += needle.length; }
  return count;
}

/** Extract all Express route handler bodies from source for analysis */
function findRoutesMissingTryCatch(src) {
  const issues = [];
  // Match router.get/post/put/delete/patch with async handlers
  const routeRe = /router\.(get|post|put|delete|patch)\s*\(\s*['"`][^'"`]+['"`]\s*(?:,\s*\S+\s*)*,\s*async\s*\(req,\s*res(?:,\s*next)?\)\s*=>\s*\{([^]*?)\n\s*\}\s*\)/g;
  let m;
  while ((m = routeRe.exec(src)) !== null) {
    const body = m[2];
    const hasTryCatch = /\btry\s*\{/.test(body);
    const hasNext     = /\bnext\s*\(/.test(body);
    if (!hasTryCatch && !hasNext) {
      // Find the approximate line number
      const lineNum = src.slice(0, m.index).split('\n').length;
      issues.push({ method: m[1], line: lineNum, snippet: m[0].slice(0, 80) });
    }
  }
  return issues;
}

/** Find Express routes that call res.json/res.send without try/catch */
function findExpressRoutesNoCatch(src) {
  const issues = [];
  // Simpler heuristic: async route handler lines without try
  const lines = src.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    // Look for router.METHOD( definitions
    if (/router\.(get|post|put|delete|patch)\s*\(/.test(line) && /async\s+\(req,\s*res/.test(line)) {
      // Look ahead for try/catch
      let hasTryCatch = false;
      // Scan up to 60 lines ahead for try block
      for (let j = i; j < Math.min(i + 60, lines.length); j++) {
        if (/\btry\s*\{/.test(lines[j])) { hasTryCatch = true; break; }
        // If we hit another route definition, stop
        if (j !== i && /router\.(get|post|put|delete|patch)\s*\(/.test(lines[j])) break;
      }
      if (!hasTryCatch) {
        issues.push({ line: i + 1, snippet: line.trim().slice(0, 80) });
      }
    }
  }
  return issues;
}

/** Find callback_query handlers that don't call answerCallbackQuery */
function findCallbacksMissingAck(src) {
  // Check if the main callback_query handler has answerCallbackQuery
  const hasGlobalAck = /answerCallbackQuery\s*\(\s*q\.id\s*\)/.test(src);
  return { hasGlobalAck };
}

/** Find hardcoded strings that should come from settings/env */
function findHardcodedStrings(src) {
  const suspects = [];
  // Phone-like patterns not in env/settings access
  const phoneRe = /(['"`])\+?[\d\s\-\(\)]{10,}['"`]/g;
  let m;
  while ((m = phoneRe.exec(src)) !== null) {
    const lineNum = src.slice(0, m.index).split('\n').length;
    suspects.push({ type: 'phone-like', line: lineNum, value: m[0] });
  }
  return suspects;
}

// ─── Patches ──────────────────────────────────────────────────────────────────

/**
 * Wraps an async Express route handler that has no try/catch.
 * Adds: try { ... } catch (e) { next(e); }
 * Returns patched source or null if no change was made.
 */
function wrapRouteWithTryCatch(src, routeSnippetLine) {
  // This is a best-effort line-based patcher.
  // We look for "router.X('/path', async (req, res) => {" lines without try/catch.
  const lines = src.split('\n');
  let patched = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!/router\.(get|post|put|delete|patch)\s*\(/.test(line)) continue;
    if (!/async\s+\(req,\s*res/.test(line)) continue;

    // Check if this handler already has try/catch in next ~60 lines
    let hasTryCatch = false;
    let closingBrace = -1;
    let depth = 0;
    let started = false;

    for (let j = i; j < Math.min(i + 80, lines.length); j++) {
      if (/\btry\s*\{/.test(lines[j])) { hasTryCatch = true; break; }
      // Track brace depth to find the end of the handler
      for (const ch of lines[j]) {
        if (ch === '{') { depth++; started = true; }
        else if (ch === '}') {
          if (started) depth--;
          if (started && depth === 0) { closingBrace = j; break; }
        }
      }
      if (closingBrace !== -1) break;
      // Stop if we hit another route
      if (j !== i && /router\.(get|post|put|delete|patch)\s*\(/.test(lines[j])) break;
    }

    if (hasTryCatch || closingBrace === -1) continue;

    // Find the opening brace of the handler body (first '{' on or after line i)
    let openBrace = -1;
    for (let j = i; j <= closingBrace; j++) {
      if (lines[j].includes('{')) { openBrace = j; break; }
    }
    if (openBrace === -1) continue;

    // Extract the handler body (between openBrace '{' and closingBrace '}')
    // Indent: detect from openBrace line
    const indent = lines[openBrace].match(/^(\s*)/)?.[1] || '  ';
    const bodyLines = lines.slice(openBrace + 1, closingBrace);

    // Wrap body in try/catch(e) { next(e); }
    const wrappedBody = [
      `${indent}  try {`,
      ...bodyLines.map(bl => `  ${bl}`),
      `${indent}  } catch (e) { next(e); }`,
    ];

    // Check handler signature — ensure next is a param
    // If handler is "(req, res) =>" add ", next" to signature
    lines[line.indexOf('async')] = lines[i]; // no-op, just reference
    const sigFixed = lines[i].replace(
      /async\s+\(req,\s*res\)\s*=>/,
      'async (req, res, next) =>'
    );
    lines[i] = sigFixed;

    lines.splice(openBrace + 1, closingBrace - openBrace - 1, ...wrappedBody);
    patched = true;
    break; // patch one at a time for safety
  }

  return patched ? { src: lines.join('\n'), patched: true } : { src, patched: false };
}

// ─── Agent class ──────────────────────────────────────────────────────────────

class CodeFixer extends Agent {
  constructor() {
    super({
      id:    'CF',
      name:  'Code Fixer',
      organ: 'Immune System',
      emoji: '🔧',
      focus: 'Patch bot.js and api.js — missing try/catch, answerCallbackQuery, hardcoded strings',
    });
    this._pendingFixes = []; // { file, type, description, applyFn }
  }

  async analyze() {
    // ── 1. Analyze bot.js ────────────────────────────────────────────────────
    const botSrc = readFile(BOT_PATH);
    if (!botSrc) {
      this.addFinding('HIGH', 'bot.js не найден или пуст');
    } else {
      // 1a. answerCallbackQuery acknowledgment
      const { hasGlobalAck } = findCallbacksMissingAck(botSrc);
      if (hasGlobalAck) {
        this.addFinding('OK', 'bot.js: callback_query имеет глобальный answerCallbackQuery(q.id)');
      } else {
        this.addFinding('HIGH',
          'bot.js: callback_query handler не вызывает answerCallbackQuery(q.id) глобально — кнопки будут "висеть"',
          true
        );
        this._pendingFixes.push({
          file: BOT_PATH,
          type: 'answerCallbackQuery',
          description: 'Добавить answerCallbackQuery(q.id) в начало callback_query handler',
          applyFn: () => this._fixCallbackAck(botSrc),
        });
      }

      // 1b. Hardcoded sensitive strings
      const hardcoded = findHardcodedStrings(botSrc);
      const phoneMatches = hardcoded.filter(h => h.type === 'phone-like');
      if (phoneMatches.length > 0) {
        this.addFinding('LOW',
          `bot.js: ${phoneMatches.length} возможных хардкод-строк (телефоны и т.п.) — рассмотрите использование getSetting()`
        );
      } else {
        this.addFinding('OK', 'bot.js: явных хардкод-контактов не обнаружено');
      }

      // 1c. try/catch coverage in async handlers
      const botLines = botSrc.split('\n');
      let asyncFnCount   = 0;
      let coveredCount   = 0;
      let uncoveredLines = [];

      for (let i = 0; i < botLines.length; i++) {
        const line = botLines[i];
        if (/^async function \w+/.test(line.trim())) {
          asyncFnCount++;
          // Check if first non-empty line of body is try {
          let firstBody = '';
          for (let j = i + 1; j < Math.min(i + 5, botLines.length); j++) {
            const t = botLines[j].trim();
            if (t) { firstBody = t; break; }
          }
          if (firstBody.startsWith('try {') || firstBody.startsWith('try{')) {
            coveredCount++;
          } else {
            uncoveredLines.push(i + 1);
          }
        }
      }

      if (uncoveredLines.length > 0) {
        this.addFinding('MEDIUM',
          `bot.js: ${uncoveredLines.length} из ${asyncFnCount} async-функций не начинаются с try/catch (строки: ${uncoveredLines.slice(0, 5).join(', ')}${uncoveredLines.length > 5 ? '…' : ''})`
        );
      } else if (asyncFnCount > 0) {
        this.addFinding('OK', `bot.js: все ${asyncFnCount} async-функций имеют try/catch`);
      }
    }

    // ── 2. Analyze routes/api.js ─────────────────────────────────────────────
    const apiSrc = readFile(API_PATH);
    if (!apiSrc) {
      this.addFinding('HIGH', 'routes/api.js не найден или пуст');
    } else {
      const routeIssues = findExpressRoutesNoCatch(apiSrc);
      if (routeIssues.length > 0) {
        this.addFinding('HIGH',
          `routes/api.js: ${routeIssues.length} route handler(ов) без try/catch — необработанные ошибки завалят сервер`,
          true
        );
        // Register auto-fix for the first one (safest approach — one at a time)
        this._pendingFixes.push({
          file: API_PATH,
          type: 'missing-try-catch',
          description: `Обернуть ${routeIssues.length} route handler(ов) в try/catch/next(e)`,
          applyFn: () => this._fixApiTryCatch(apiSrc),
        });
      } else {
        this.addFinding('OK', 'routes/api.js: все route handlers имеют try/catch или next(e)');
      }

      // Check /api/config route — it responds without try/catch
      const configRoute = apiSrc.match(/router\.get\s*\(\s*['"`]\/config['"`]\s*,\s*\(req,\s*res\)\s*=>/);
      if (configRoute) {
        this.addFinding('LOW',
          'routes/api.js: GET /config использует синхронный handler (ok, но рассмотрите добавление try/catch для единообразия)'
        );
      }
    }

    // ── 3. Analyze database.js ───────────────────────────────────────────────
    const dbSrc = readFile(DB_PATH);
    if (!dbSrc) {
      this.addFinding('MEDIUM', 'database.js не найден');
    } else {
      // Check generateOrderNumber for collision resistance
      if (/generateOrderNumber/.test(dbSrc)) {
        this.addFinding('OK', 'database.js: generateOrderNumber присутствует');
      } else {
        this.addFinding('HIGH', 'database.js: generateOrderNumber не найден — создание заявок через бот сломано');
      }

      // Check for missing error handling in exported functions
      const exportedFns = (dbSrc.match(/^function \w+/gm) || []).length;
      const tryCatchCount = countOccurrences(dbSrc, 'try {');
      if (exportedFns > 0 && tryCatchCount < exportedFns / 2) {
        this.addFinding('MEDIUM',
          `database.js: только ${tryCatchCount} try/catch на ${exportedFns} функций — возможны необработанные DB-ошибки`
        );
      } else {
        this.addFinding('OK', `database.js: достаточное покрытие try/catch (${tryCatchCount}/${exportedFns})`);
      }
    }

    // ── 4. Auto-apply fixable issues ─────────────────────────────────────────
    if (this._pendingFixes.length > 0) {
      await this.fix();
    }
  }

  /** Apply all registered code fixes */
  async fix() {
    for (const pending of this._pendingFixes) {
      try {
        const result = pending.applyFn();
        if (result && result.applied) {
          fs.writeFileSync(pending.file, result.src, 'utf8');
          const shortPath = path.relative(path.join(__dirname, '..'), pending.file);
          console.log(`[CodeFixer] Applied fix "${pending.type}" to ${shortPath}`);
          this.addFixed(`${shortPath}: ${pending.description}`);
        } else {
          this.addFinding('INFO', `Пропущен fix "${pending.type}": ${result && result.reason ? result.reason : 'патч не применим к текущему состоянию файла'}`);
        }
      } catch (e) {
        this.addFinding('HIGH', `Ошибка применения fix "${pending.type}": ${e.message}`);
      }
    }
    this._pendingFixes = [];
  }

  // ─── Individual fix implementations ──────────────────────────────────────

  /** Fix: add answerCallbackQuery(q.id) at start of callback_query handler */
  _fixCallbackAck(src) {
    // Find the callback_query handler and add answerCallbackQuery if missing
    const handlerRe = /bot\.on\s*\(\s*['"`]callback_query['"`]\s*,\s*async\s*\(q\)\s*=>\s*\{/;
    const match = handlerRe.exec(src);
    if (!match) {
      return { applied: false, reason: 'callback_query handler не найден по ожидаемому паттерну' };
    }

    // Check if answerCallbackQuery is already there (in case readFile is stale)
    if (/answerCallbackQuery\s*\(\s*q\.id\s*\)/.test(src)) {
      return { applied: false, reason: 'answerCallbackQuery(q.id) уже присутствует' };
    }

    // Find the position right after the opening brace of the handler body
    const afterBrace = match.index + match[0].length;
    const lines = src.split('\n');
    let insertLine = -1;
    let charCount = 0;
    for (let i = 0; i < lines.length; i++) {
      charCount += lines[i].length + 1; // +1 for \n
      if (charCount > afterBrace) { insertLine = i + 1; break; }
    }

    if (insertLine === -1) {
      return { applied: false, reason: 'Не удалось определить строку для вставки' };
    }

    // Detect indentation from surrounding lines
    const indent = '    ';
    lines.splice(insertLine, 0, `${indent}try { await bot.answerCallbackQuery(q.id); } catch {}`);
    return { applied: true, src: lines.join('\n') };
  }

  /** Fix: wrap Express route handlers missing try/catch */
  _fixApiTryCatch(src) {
    const lines = src.split('\n');
    let patchCount = 0;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      // Match simple synchronous route handlers: router.METHOD('/path', (req, res) => {
      // that are NOT async (async ones should use try/catch differently)
      // Target: async handlers without try/catch
      if (!/router\.(get|post|put|delete|patch)\s*\(/.test(line)) continue;
      if (!/async\s+\(req,\s*res\)\s*=>/.test(line)) continue;

      // Look ahead for try/catch
      let hasTryCatch = false;
      let endLine = -1;
      let depth = 0;
      let started = false;

      for (let j = i; j < Math.min(i + 80, lines.length); j++) {
        if (/\btry\s*\{/.test(lines[j])) { hasTryCatch = true; break; }
        for (const ch of lines[j]) {
          if (ch === '{') { depth++; started = true; }
          else if (ch === '}' && started) {
            depth--;
            if (depth === 0) { endLine = j; break; }
          }
        }
        if (endLine !== -1) break;
        if (j !== i && /router\.(get|post|put|delete|patch)\s*\(/.test(lines[j])) break;
      }

      if (hasTryCatch || endLine === -1) continue;

      // Find opening brace line
      let openLine = -1;
      for (let j = i; j <= endLine; j++) {
        if (lines[j].includes('{')) { openLine = j; break; }
      }
      if (openLine === -1) continue;

      // Get indentation of handler
      const handlerIndent = (lines[openLine].match(/^(\s*)/) || ['', ''])[1];
      const bodyIndent    = handlerIndent + '  ';

      // Fix signature: (req, res) => needs next for next(e)
      lines[i] = lines[i].replace(
        /async\s+\(req,\s*res\)\s*=>/,
        'async (req, res, next) =>'
      );

      // Extract body lines (openLine+1 .. endLine-1)
      const bodyLines = lines.slice(openLine + 1, endLine);

      // Build wrapped body
      const wrappedLines = [
        `${bodyIndent}try {`,
        ...bodyLines.map(bl => `  ${bl}`),
        `${bodyIndent}} catch (e) { next(e); }`,
      ];

      // Replace in-place
      lines.splice(openLine + 1, endLine - openLine - 1, ...wrappedLines);
      patchCount++;

      // Adjust loop index after splice
      const delta = wrappedLines.length - bodyLines.length;
      i += delta; // skip over newly inserted lines

      // Patch one handler per invocation to be safe
      break;
    }

    if (patchCount === 0) {
      return { applied: false, reason: 'Нет подходящих route handlers для патча (все уже имеют try/catch или next)' };
    }

    return { applied: true, src: lines.join('\n') };
  }
}

// ─── Run as standalone ────────────────────────────────────────────────────────
if (require.main === module) {
  new CodeFixer().run().then(() => process.exit(0)).catch(e => {
    console.error('[CodeFixer] Fatal:', e.message);
    process.exit(1);
  });
}

module.exports = CodeFixer;
