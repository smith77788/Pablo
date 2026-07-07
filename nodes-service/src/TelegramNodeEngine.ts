/**
 * TelegramNodeEngine — низкоуровневые MTProto-операции над Node-архитектурой.
 *
 * Реальные конструкторы схемы (никаких выдуманных методов/флагов):
 *   Node          = channels.CreateChannel(megagroup:true) + channels.ToggleForum
 *   Admin title   = channels.EditAdmin(rank) — ВИДИМЫЙ кастомный титул (не анонимизация)
 *   SubNode       = channels.CreateForumTopic  → id топика == message_thread_id
 *   Broadcast     = messages.SendMessage(replyTo=topicId)
 *   Invite        = messages.ExportChatInvite
 *   Teardown      = channels.DeleteChannel
 */
import { TelegramClient, Api } from "telegram";
import bigInt from "big-integer";
import { randomBytes } from "crypto";
import { NodeClientFactory } from "./NodeClientFactory.js";
import { AccountContext, TelegramNodeRecord, SubNodeRecord, TopicSpec } from "./types.js";
import { NodeApiError, classifyError, sleep } from "./errors.js";
import { pickSpintax } from "./spintax.js";

/** Уважающий flood-wait повтор: ждём ровно столько, сколько просит Telegram. */
async function withFloodRetry<T>(
  fn: () => Promise<T>,
  opts: { retries?: number; capSeconds?: number } = {},
): Promise<T> {
  const retries = opts.retries ?? 3;
  const cap = opts.capSeconds ?? 300;
  let attempt = 0;
  for (;;) {
    try {
      return await fn();
    } catch (e) {
      const err = classifyError(e);
      if (err.code === "FLOOD_WAIT" && attempt < retries) {
        const secs = Math.min(err.retryAfterSeconds ?? 5, cap);
        await sleep((secs + 1) * 1000); // +1с запас — именно ЖДЁМ, а не обходим
        attempt += 1;
        continue;
      }
      throw err;
    }
  }
}

function randomLong(): bigInt.BigInteger {
  // криптослучайный 63-битный id (random_id для CreateForumTopic и т.п.)
  const buf = randomBytes(8);
  buf[0] &= 0x7f;
  return bigInt(buf.toString("hex"), 16);
}

/** InputChannel для методов channels.* */
function channelInput(node: TelegramNodeRecord): Api.InputChannel {
  if (!node.accessHash)
    throw new NodeApiError("MISSING_ACCESS_HASH", "У узла нет access_hash", 500);
  return new Api.InputChannel({
    channelId: bigInt(node.workspaceTelegramId),
    accessHash: bigInt(node.accessHash),
  });
}

/** InputPeerChannel для messages.* (broadcast/invite) */
function channelPeer(node: TelegramNodeRecord): Api.InputPeerChannel {
  if (!node.accessHash)
    throw new NodeApiError("MISSING_ACCESS_HASH", "У узла нет access_hash", 500);
  return new Api.InputPeerChannel({
    channelId: bigInt(node.workspaceTelegramId),
    accessHash: bigInt(node.accessHash),
  });
}

/** Достаёт id созданного форум-топика из Updates. topic id == id сервис-сообщения. */
function extractForumTopicId(result: Api.TypeUpdates): number {
  const updates = (result as { updates?: unknown[] }).updates ?? [];
  for (const u of updates as Array<Record<string, unknown>>) {
    const m = u["message"] as { id?: number; className?: string; action?: unknown } | undefined;
    if (m && (m.className === "MessageService" || m.action) && typeof m.id === "number") {
      return m.id;
    }
    if (u["className"] === "UpdateMessageID" && typeof u["id"] === "number") {
      return u["id"] as number;
    }
  }
  // fallback: любое новое сообщение
  for (const u of updates as Array<Record<string, unknown>>) {
    const m = u["message"] as { id?: number } | undefined;
    if (m && typeof m.id === "number") return m.id;
  }
  throw new NodeApiError(
    "TOPIC_ID_PARSE_FAILED",
    "Не удалось определить id форум-топика из ответа MTProto",
    502,
  );
}

export interface CreatedWorkspace {
  workspaceTelegramId: string;
  accessHash: string;
  title: string;
  username: string | null;
}

export class TelegramNodeEngine {
  constructor(private readonly factory: NodeClientFactory) {}

  /**
   * Создать Node: мегагруппа + включённый форум (+опциональный публичный username).
   */
  async createNodeWorkspace(
    ctx: AccountContext,
    rawTitle: string,
    opts: { username?: string; description?: string; spintaxSeed?: number } = {},
  ): Promise<CreatedWorkspace> {
    const title = pickSpintax(rawTitle, opts.spintaxSeed).slice(0, 128);
    return this.factory.run(ctx, async (client) => {
      const created = (await withFloodRetry(() =>
        client.invoke(
          new Api.channels.CreateChannel({
            title,
            about: (opts.description ?? "").slice(0, 255),
            megagroup: true, // рабочая супергруппа (не broadcast-канал)
          }),
        ),
      )) as Api.TypeUpdates;

      const chat = (created as { chats?: Array<{ id: bigInt.BigInteger; accessHash?: bigInt.BigInteger }> })
        .chats?.[0];
      if (!chat)
        throw new NodeApiError("CREATE_FAILED", "MTProto не вернул созданный канал", 502);

      const node: TelegramNodeRecord = {
        id: "",
        workspaceTelegramId: chat.id.toString(),
        accessHash: chat.accessHash ? chat.accessHash.toString() : null,
        title,
        username: null,
        description: opts.description ?? null,
        creatorAccountId: ctx.accountId,
        status: "PROVISIONING" as TelegramNodeRecord["status"],
        createdAt: new Date(),
      };
      const input = channelInput(node);

      // Включаем форум-режим — это и есть «workspace с подканалами».
      await withFloodRetry(() =>
        client.invoke(new Api.channels.ToggleForum({ channel: input, enabled: true })),
      );

      // Публичный username (опционально; занятость — не фатальна).
      if (opts.username) {
        try {
          await withFloodRetry(() =>
            client.invoke(
              new Api.channels.UpdateUsername({ channel: input, username: opts.username! }),
            ),
          );
          node.username = opts.username;
        } catch (e) {
          const err = classifyError(e);
          if (err.code !== "USERNAME_UNAVAILABLE") throw err;
          // иначе оставляем приватным, username не установлен
        }
      }

      return {
        workspaceTelegramId: node.workspaceTelegramId,
        accessHash: node.accessHash ?? "",
        title,
        username: node.username,
      };
    });
  }

  /**
   * Кастомный админ-титул внутри узла через channels.EditAdmin(rank).
   *
   * ЯВНО: это ВИДИМЫЙ бейдж (напр. "Admin [Node]"), а не сокрытие личности.
   * anonymous=false — атрибуция действий остаётся прозрачной. Мы сознательно НЕ
   * включаем анонимный постинг.
   */
  async setNodeAdminTitle(
    ctx: AccountContext,
    node: TelegramNodeRecord,
    targetUserId: string | number,
    rank: string,
  ): Promise<void> {
    const input = channelInput(node);
    await this.factory.run(ctx, async (client) => {
      const user = await client.getInputEntity(targetUserId);
      await withFloodRetry(() =>
        client.invoke(
          new Api.channels.EditAdmin({
            channel: input,
            userId: user,
            adminRights: new Api.ChatAdminRights({
              changeInfo: true,
              postMessages: true,
              editMessages: true,
              deleteMessages: true,
              banUsers: true,
              inviteUsers: true,
              pinMessages: true,
              addAdmins: false,
              anonymous: false, // атрибуция видима — НЕ анонимизируем
              manageCall: true,
              other: true,
              manageTopics: true,
            }),
            rank: rank.slice(0, 16), // лимит Telegram на кастомный титул — 16 символов
          }),
        ),
      );
    });
  }

  /**
   * Создать подузлы как форум-топики (Logs/Metrics/Commands).
   * Один connect/disconnect на всю пачку + микро-паузы между RPC (хороший гражданин).
   */
  async provisionSubNodes(
    ctx: AccountContext,
    node: TelegramNodeRecord,
    topics: TopicSpec[],
  ): Promise<Array<{ topicTelegramId: string; title: string; type: TopicSpec["type"] }>> {
    const input = channelInput(node);
    const out: Array<{ topicTelegramId: string; title: string; type: TopicSpec["type"] }> = [];
    await this.factory.run(ctx, async (client) => {
      for (const t of topics) {
        const res = (await withFloodRetry(() =>
          client.invoke(
            new Api.channels.CreateForumTopic({
              channel: input,
              title: t.title.slice(0, 128),
              iconColor: t.iconColor,
              randomId: randomLong(),
            }),
          ),
        )) as Api.TypeUpdates;
        out.push({
          topicTelegramId: String(extractForumTopicId(res)),
          title: t.title,
          type: t.type,
        });
        await sleep(1500); // пауза между созданием топиков
      }
    });
    return out;
  }

  /**
   * Системная рассылка по всем подузлам (топикам) через replyTo=message_thread_id.
   */
  async broadcast(
    ctx: AccountContext,
    node: TelegramNodeRecord,
    subNodes: SubNodeRecord[],
    message: string,
  ): Promise<Array<{ subNodeId: string; messageId: number }>> {
    const peer = channelPeer(node);
    const results: Array<{ subNodeId: string; messageId: number }> = [];
    await this.factory.run(ctx, async (client) => {
      for (const sn of subNodes) {
        const sent = await withFloodRetry(() =>
          client.sendMessage(peer, {
            message,
            replyTo: Number(sn.topicTelegramId), // targeting топика форума
          }),
        );
        results.push({ subNodeId: sn.id, messageId: (sent as { id: number }).id });
        await sleep(1200);
      }
    });
    return results;
  }

  /** Сгенерировать/ротировать инвайт-ссылку рабочего узла. */
  async exportInvite(
    ctx: AccountContext,
    node: TelegramNodeRecord,
    opts: { expireSeconds?: number; usageLimit?: number; title?: string } = {},
  ): Promise<{ link: string }> {
    const peer = channelPeer(node);
    return this.factory.run(ctx, async (client) => {
      const res = (await withFloodRetry(() =>
        client.invoke(
          new Api.messages.ExportChatInvite({
            peer,
            expireDate: opts.expireSeconds
              ? Math.floor(Date.now() / 1000) + opts.expireSeconds
              : undefined,
            usageLimit: opts.usageLimit,
            title: opts.title,
          }),
        ),
      )) as Api.ChatInviteExported;
      return { link: res.link };
    });
  }

  /** Полный снос узла: DeleteChannel в Telegram (подузлы-топики уходят вместе с ним). */
  async deleteNode(ctx: AccountContext, node: TelegramNodeRecord): Promise<void> {
    const input = channelInput(node);
    await this.factory.run(ctx, async (client) => {
      await withFloodRetry(() =>
        client.invoke(new Api.channels.DeleteChannel({ channel: input })),
      );
    });
  }
}
