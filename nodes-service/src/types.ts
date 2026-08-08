/**
 * Доменные типы модуля Telegram Nodes.
 *
 * "Node" в этой реализации — это Telegram-мегагруппа с включённым форумом
 * (реальная MTProto-архитектура), а "SubNode" — форум-топик. Никаких выдуманных
 * API/флагов: всё маппится на существующие конструкторы схемы.
 */

export enum NodeStatus {
  PROVISIONING = "PROVISIONING",
  ACTIVE = "ACTIVE",
  PAUSED = "PAUSED",
  RATE_LIMITED = "RATE_LIMITED",
  DEAD = "DEAD",
}

export enum SubNodeType {
  LOGS = "LOGS",
  METRICS = "METRICS",
  COMMANDS = "COMMANDS",
}

/**
 * Контекст аккаунта для загрузки MTProto-клиента.
 *
 * ВАЖНО (осознанное решение): авторизация — только через StringSession,
 * полученный штатным логином пользователя в его СОБСТВЕННЫЙ аккаунт. Парсинг
 * сырых tdata auth-key буферов в пул намеренно НЕ реализован — это паттерн
 * массовых закупленных/собранных аккаунтов, вне рамок легитимного управления
 * своими каналами.
 */
export interface AccountContext {
  accountId: string;
  apiId: number;
  apiHash: string;
  /** StringSession-строка. Хранить зашифрованной в БД (см. AccountProvider.decrypt). */
  session: string;
  /** Опциональный SOCKS5/MTProxy для сетевой изоляции аккаунта. */
  proxy?: {
    ip: string;
    port: number;
    username?: string;
    password?: string;
    socksType?: 4 | 5;
    secret?: string; // для MTProxy
  };
}

export interface TelegramNodeRecord {
  id: string;
  workspaceTelegramId: string; // channel id
  accessHash: string | null; // нужен для повторных invoke по InputChannel/InputPeer
  title: string;
  username: string | null;
  description: string | null;
  creatorAccountId: string;
  status: NodeStatus;
  createdAt: Date;
}

export interface SubNodeRecord {
  id: string;
  nodeId: string;
  topicTelegramId: string; // id топика == message_thread_id
  title: string;
  type: SubNodeType;
  createdAt: Date;
}

export interface NodeActivityLogRecord {
  id: string;
  nodeId: string;
  eventType: string;
  message: string;
  payload: unknown;
  createdAt: Date;
}

export interface TopicSpec {
  title: string;
  type: SubNodeType;
  /** цвет иконки топика (Telegram палитра), опционально */
  iconColor?: number;
}
