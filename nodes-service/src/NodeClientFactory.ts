/**
 * NodeClientFactory — динамическая загрузка MTProto-клиентов (gramjs) под аккаунт.
 *
 * Жизненный цикл каждой операции: connect → выполнить работу → disconnect,
 * чтобы не копить висящие сокеты и утечки памяти.
 *
 * Конкурентность: операции одного аккаунта СЕРИАЛИЗУЮТСЯ (последовательная
 * очередь на accountId) + троттлинг 3000–5000мс между операциями. Это делает
 * нас «хорошим гражданином» API и снижает риск САМИМ спровоцировать FLOOD_WAIT
 * при одновременных триггерах из дашборда. Это НЕ обход лимитов — сам flood-wait
 * мы всегда уважаем (см. withFloodRetry).
 */
import { TelegramClient } from "telegram";
import { StringSession } from "telegram/sessions/index.js";
import type { ProxyInterface } from "telegram/network/connection/TCPMTProxy.js";
import { AccountContext } from "./types.js";
import { NodeApiError, sleep } from "./errors.js";

export interface FactoryOptions {
  minDelayMs?: number;
  maxDelayMs?: number;
  connectionRetries?: number;
}

export class NodeClientFactory {
  private readonly queues = new Map<string, Promise<unknown>>();
  private readonly lastRun = new Map<string, number>();
  private readonly minDelayMs: number;
  private readonly maxDelayMs: number;
  private readonly connectionRetries: number;

  constructor(opts: FactoryOptions = {}) {
    this.minDelayMs = opts.minDelayMs ?? 3000;
    this.maxDelayMs = opts.maxDelayMs ?? 5000;
    this.connectionRetries = opts.connectionRetries ?? 5;
  }

  /**
   * Выполнить `fn` с живым авторизованным клиентом под данный аккаунт.
   * Операции одного аккаунта не пересекаются (mutex через цепочку промисов).
   */
  async run<T>(
    ctx: AccountContext,
    fn: (client: TelegramClient) => Promise<T>,
  ): Promise<T> {
    const key = ctx.accountId;
    const prev = this.queues.get(key) ?? Promise.resolve();
    // следующий в очереди стартует только после завершения предыдущего
    const task = prev
      .catch(() => undefined)
      .then(() => this.execThrottled(ctx, key, fn));
    // в очереди держим «проглоченную» версию, чтобы одна ошибка не рвала цепочку
    this.queues.set(
      key,
      task.catch(() => undefined),
    );
    return task;
  }

  private async execThrottled<T>(
    ctx: AccountContext,
    key: string,
    fn: (client: TelegramClient) => Promise<T>,
  ): Promise<T> {
    await this.throttle(key);
    const client = await this.boot(ctx);
    try {
      return await fn(client);
    } finally {
      // строгий teardown: рвём сокет и освобождаем ресурсы
      try {
        await client.disconnect();
      } catch {
        /* already down */
      }
      try {
        await client.destroy();
      } catch {
        /* ignore */
      }
      this.lastRun.set(key, Date.now());
    }
  }

  private async throttle(key: string): Promise<void> {
    const jitter =
      this.minDelayMs +
      Math.floor(Math.random() * Math.max(1, this.maxDelayMs - this.minDelayMs));
    const last = this.lastRun.get(key) ?? 0;
    const wait = last + jitter - Date.now();
    if (wait > 0) await sleep(wait);
  }

  private buildProxy(ctx: AccountContext): ProxyInterface | undefined {
    if (!ctx.proxy) return undefined;
    const p = ctx.proxy;
    if (p.secret) {
      // MTProxy
      return { ip: p.ip, port: p.port, MTProxy: true, secret: p.secret };
    }
    return {
      ip: p.ip,
      port: p.port,
      socksType: p.socksType ?? 5,
      username: p.username,
      password: p.password,
    };
  }

  private async boot(ctx: AccountContext): Promise<TelegramClient> {
    const client = new TelegramClient(
      new StringSession(ctx.session),
      ctx.apiId,
      ctx.apiHash,
      {
        connectionRetries: this.connectionRetries,
        proxy: this.buildProxy(ctx),
        autoReconnect: false, // операция короткая, авто-реконнект не нужен
      },
    );
    await client.connect();
    const authorized = await client.checkAuthorization();
    if (!authorized) {
      try {
        await client.disconnect();
      } catch {
        /* ignore */
      }
      throw new NodeApiError(
        "SESSION_UNAUTHORIZED",
        "StringSession не авторизована — требуется повторный логин аккаунта",
        401,
      );
    }
    return client;
  }
}
