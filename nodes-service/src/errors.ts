/**
 * Классификация MTProto-ошибок в доменные ошибки API.
 *
 * Ключевой принцип: FLOOD_WAIT — не «баг, который надо обойти», а сигнал
 * платформы, который мы УВАЖАЕМ (ждём указанное время выше по стеку). Здесь
 * лишь распознаём тип и решаем, фатален ли он для аккаунта.
 */
import { NodeStatus } from "./types.js";

export class NodeApiError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly httpStatus = 400,
    /** нужно ли переводить хост-аккаунт в терминальный статус */
    public readonly accountStatus: NodeStatus | null = null,
    /** сколько секунд просит подождать Telegram (для FLOOD_WAIT) */
    public readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = "NodeApiError";
  }

  toJSON() {
    return {
      error: {
        code: this.code,
        message: this.message,
        ...(this.retryAfterSeconds !== null
          ? { retryAfterSeconds: this.retryAfterSeconds }
          : {}),
      },
    };
  }
}

/**
 * Разбирает ошибку gramjs/RPCError по имени класса и тексту.
 * gramjs выбрасывает типизированные ошибки (FloodWaitError и т.п.), но в разных
 * версиях они доступны по-разному — поэтому проверяем и класс, и errorMessage.
 */
export function classifyError(e: unknown): NodeApiError {
  if (e instanceof NodeApiError) return e;

  const err = e as {
    className?: string;
    errorMessage?: string;
    message?: string;
    seconds?: number;
    code?: number;
  };
  const name = err?.className ?? "";
  const msg = err?.errorMessage ?? err?.message ?? String(e);

  // FLOOD_WAIT_X — уважаем: сообщаем retryAfter, статус аккаунта временный.
  if (name === "FloodWaitError" || /FLOOD_WAIT_\d+/.test(msg) || err?.seconds) {
    const secs =
      err?.seconds ?? Number((msg.match(/FLOOD_WAIT_(\d+)/) ?? [])[1] ?? 0);
    return new NodeApiError(
      "FLOOD_WAIT",
      `Telegram flood-wait ${secs}s — операция отложена`,
      429,
      NodeStatus.RATE_LIMITED,
      secs || null,
    );
  }

  // Терминальные для аккаунта ошибки — переводим хост в DEAD.
  if (name === "AuthKeyDuplicatedError" || /AUTH_KEY_DUPLICATED/.test(msg)) {
    return new NodeApiError(
      "AUTH_KEY_DUPLICATED",
      "Сессия аккаунта аннулирована (auth key duplicated)",
      401,
      NodeStatus.DEAD,
    );
  }
  if (name === "UserDeactivatedError" || /USER_DEACTIVATED(_BAN)?/.test(msg)) {
    return new NodeApiError(
      "USER_DEACTIVATED",
      "Аккаунт деактивирован/забанен Telegram",
      403,
      NodeStatus.DEAD,
    );
  }
  if (/SESSION_REVOKED|AUTH_KEY_UNREGISTERED/.test(msg)) {
    return new NodeApiError(
      "SESSION_REVOKED",
      "Сессия отозвана — требуется повторная авторизация",
      401,
      NodeStatus.DEAD,
    );
  }

  // Прочие RPC-ошибки — не фатальны для аккаунта.
  if (/CHANNELS_TOO_MUCH/.test(msg)) {
    return new NodeApiError(
      "CHANNELS_LIMIT",
      "Аккаунт достиг лимита каналов Telegram",
      409,
      NodeStatus.RATE_LIMITED,
    );
  }
  if (/USERNAME_(OCCUPIED|INVALID|PURCHASE_AVAILABLE)/.test(msg)) {
    return new NodeApiError("USERNAME_UNAVAILABLE", `Username недоступен: ${msg}`, 409);
  }

  return new NodeApiError("MTPROTO_ERROR", msg || "Неизвестная ошибка MTProto", 502);
}

export const sleep = (ms: number): Promise<void> =>
  new Promise((r) => setTimeout(r, ms));
