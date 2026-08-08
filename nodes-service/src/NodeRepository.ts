/**
 * Слой доступа к данным. Интерфейс NodeRepository изолирует контроллер от
 * конкретного ORM; ниже — реальная реализация на Prisma Client.
 */
import { Prisma, type PrismaClient } from "@prisma/client";
import {
  NodeStatus,
  SubNodeType,
  TelegramNodeRecord,
  SubNodeRecord,
  NodeActivityLogRecord,
} from "./types.js";

export interface CreateNodeInput {
  title: string;
  username: string | null;
  description: string | null;
  creatorAccountId: string;
}

export interface NodeRepository {
  createNode(input: CreateNodeInput): Promise<TelegramNodeRecord>;
  getNode(id: string): Promise<TelegramNodeRecord | null>;
  updateNode(
    id: string,
    patch: Partial<
      Pick<
        TelegramNodeRecord,
        "workspaceTelegramId" | "accessHash" | "username" | "status" | "title"
      >
    >,
  ): Promise<TelegramNodeRecord>;
  deleteNode(id: string): Promise<void>;
  addSubNodes(
    nodeId: string,
    subs: Array<{ topicTelegramId: string; title: string; type: SubNodeType }>,
  ): Promise<SubNodeRecord[]>;
  getSubNodes(nodeId: string): Promise<SubNodeRecord[]>;
  addLog(
    nodeId: string,
    eventType: string,
    message: string,
    payload?: unknown,
  ): Promise<void>;
  getLogs(nodeId: string, limit: number): Promise<NodeActivityLogRecord[]>;
}

function mapNode(row: {
  id: string;
  workspaceTelegramId: string;
  accessHash: string | null;
  title: string;
  username: string | null;
  description: string | null;
  creatorAccountId: string;
  status: string;
  createdAt: Date;
}): TelegramNodeRecord {
  return { ...row, status: row.status as NodeStatus };
}

export class PrismaNodeRepository implements NodeRepository {
  constructor(private readonly prisma: PrismaClient) {}

  async createNode(input: CreateNodeInput): Promise<TelegramNodeRecord> {
    const row = await this.prisma.telegramNode.create({
      data: {
        workspaceTelegramId: "",
        title: input.title,
        username: input.username,
        description: input.description,
        creatorAccountId: input.creatorAccountId,
        status: NodeStatus.PROVISIONING,
      },
    });
    return mapNode(row);
  }

  async getNode(id: string): Promise<TelegramNodeRecord | null> {
    const row = await this.prisma.telegramNode.findUnique({ where: { id } });
    return row ? mapNode(row) : null;
  }

  async updateNode(
    id: string,
    patch: Partial<
      Pick<
        TelegramNodeRecord,
        "workspaceTelegramId" | "accessHash" | "username" | "status" | "title"
      >
    >,
  ): Promise<TelegramNodeRecord> {
    const row = await this.prisma.telegramNode.update({ where: { id }, data: patch });
    return mapNode(row);
  }

  async deleteNode(id: string): Promise<void> {
    // подузлы и логи снимаются каскадом (onDelete: Cascade в схеме)
    await this.prisma.telegramNode.delete({ where: { id } });
  }

  async addSubNodes(
    nodeId: string,
    subs: Array<{ topicTelegramId: string; title: string; type: SubNodeType }>,
  ): Promise<SubNodeRecord[]> {
    const created: SubNodeRecord[] = [];
    for (const s of subs) {
      const row = await this.prisma.subNode.create({
        data: {
          nodeId,
          topicTelegramId: s.topicTelegramId,
          title: s.title,
          type: s.type,
        },
      });
      created.push({ ...row, type: row.type as SubNodeType });
    }
    return created;
  }

  async getSubNodes(nodeId: string): Promise<SubNodeRecord[]> {
    const rows = await this.prisma.subNode.findMany({
      where: { nodeId },
      orderBy: { createdAt: "asc" },
    });
    return rows.map((r) => ({ ...r, type: r.type as SubNodeType }));
  }

  async addLog(
    nodeId: string,
    eventType: string,
    message: string,
    payload: unknown = null,
  ): Promise<void> {
    await this.prisma.nodeActivityLog.create({
      data: {
        nodeId,
        eventType,
        message,
        payload:
          payload === undefined || payload === null
            ? Prisma.JsonNull
            : (payload as Prisma.InputJsonValue),
      },
    });
  }

  async getLogs(nodeId: string, limit: number): Promise<NodeActivityLogRecord[]> {
    const rows = await this.prisma.nodeActivityLog.findMany({
      where: { nodeId },
      orderBy: { createdAt: "desc" },
      take: Math.min(Math.max(limit, 1), 500),
    });
    return rows.map((r) => ({ ...r, payload: r.payload }));
  }
}
