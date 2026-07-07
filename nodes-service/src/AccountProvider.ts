/**
 * AccountProvider — источник авторизованных сессий из вашего пула аккаунтов.
 *
 * Реализация читает StringSession из таблицы account_sessions (см. Prisma-схему).
 * Сессия должна храниться зашифрованной; расшифровка делегируется функции
 * `decrypt`, чтобы этот модуль не знал про ваш KMS/секрет. Никакого парсинга
 * сырых tdata-буферов здесь нет — сессия получается штатным логином аккаунта.
 */
import type { PrismaClient } from "@prisma/client";
import { AccountContext } from "./types.js";
import { NodeApiError } from "./errors.js";

export interface AccountProvider {
  getContext(accountId: string): Promise<AccountContext>;
}

export interface PrismaAccountProviderOptions {
  /** расшифровка StringSession (ENC → plaintext). По умолчанию — identity. */
  decrypt?: (enc: string) => string | Promise<string>;
  /** глобальные api_id/api_hash, если не заданы на уровне записи аккаунта */
  fallbackApiId?: number;
  fallbackApiHash?: string;
}

export class PrismaAccountProvider implements AccountProvider {
  constructor(
    private readonly prisma: PrismaClient,
    private readonly opts: PrismaAccountProviderOptions = {},
  ) {}

  async getContext(accountId: string): Promise<AccountContext> {
    const row = await this.prisma.accountSession.findUnique({
      where: { accountId },
    });
    if (!row) {
      throw new NodeApiError("ACCOUNT_NOT_FOUND", `Аккаунт ${accountId} не найден в пуле`, 404);
    }
    if (row.isActive === false) {
      throw new NodeApiError("ACCOUNT_INACTIVE", `Аккаунт ${accountId} неактивен/мёртв`, 409);
    }

    const decrypt = this.opts.decrypt ?? ((s: string) => s);
    const session = await decrypt(row.stringSession);
    const apiId = row.apiId ?? this.opts.fallbackApiId;
    const apiHash = row.apiHash ?? this.opts.fallbackApiHash;
    if (!apiId || !apiHash) {
      throw new NodeApiError(
        "ACCOUNT_MISSING_API_CREDENTIALS",
        `У аккаунта ${accountId} нет api_id/api_hash`,
        500,
      );
    }

    return {
      accountId,
      apiId,
      apiHash,
      session,
      proxy: row.proxyIp
        ? {
            ip: row.proxyIp,
            port: row.proxyPort ?? 1080,
            username: row.proxyUser ?? undefined,
            password: row.proxyPass ?? undefined,
            socksType: 5,
          }
        : undefined,
    };
  }
}
