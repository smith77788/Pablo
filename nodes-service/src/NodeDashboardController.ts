/**
 * NodeDashboardController — REST-слой под UI-тайлы дашборда.
 *
 * Маршруты 1-в-1 соответствуют действиям UI:
 *   POST   /api/nodes/create
 *   POST   /api/nodes/:id/toggle
 *   DELETE /api/nodes/:id
 *   POST   /api/nodes/:id/broadcast
 *   POST   /api/nodes/:id/invite
 *   GET    /api/nodes/:id/logs
 */
import { Router, Request, Response } from "express";
import { NodeRepository } from "./NodeRepository.js";
import { AccountProvider } from "./AccountProvider.js";
import { TelegramNodeEngine } from "./TelegramNodeEngine.js";
import { NodeApiError, classifyError } from "./errors.js";
import { NodeStatus, SubNodeType, TopicSpec, TelegramNodeRecord } from "./types.js";

export interface ControllerDeps {
  repo: NodeRepository;
  accounts: AccountProvider;
  engine: TelegramNodeEngine;
}

function sendError(res: Response, e: unknown): void {
  const err = e instanceof NodeApiError ? e : classifyError(e);
  res.status(err.httpStatus).json(err.toJSON());
}

/** Запускает engine-операцию; при фатальной для аккаунта ошибке двигает статус узла. */
async function guardNode<T>(
  repo: NodeRepository,
  node: TelegramNodeRecord,
  fn: () => Promise<T>,
): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    const err = e instanceof NodeApiError ? e : classifyError(e);
    if (err.accountStatus) {
      await repo
        .updateNode(node.id, { status: err.accountStatus })
        .catch(() => undefined);
      await repo
        .addLog(node.id, "ERROR", `${err.code}: ${err.message}`, {
          httpStatus: err.httpStatus,
        })
        .catch(() => undefined);
    }
    throw err;
  }
}

function parseSubNodeSpecs(raw: unknown): TopicSpec[] {
  if (!Array.isArray(raw)) return [];
  const out: TopicSpec[] = [];
  for (const item of raw) {
    const title = String((item as { title?: unknown })?.title ?? "").trim();
    if (!title) continue;
    const typeRaw = String((item as { type?: unknown })?.type ?? "LOGS").toUpperCase();
    const type = (Object.values(SubNodeType) as string[]).includes(typeRaw)
      ? (typeRaw as SubNodeType)
      : SubNodeType.LOGS;
    out.push({ title, type });
  }
  return out;
}

export function createNodeRouter(deps: ControllerDeps): Router {
  const { repo, accounts, engine } = deps;
  const router = Router();

  // ── CREATE: полный конвейер провижининга ────────────────────────────────
  router.post("/api/nodes/create", async (req: Request, res: Response) => {
    let nodeId: string | null = null;
    try {
      const { title, username, description, accountId, subNodes } = req.body ?? {};
      if (!title || !accountId) {
        throw new NodeApiError("BAD_REQUEST", "Обязательны поля title и accountId", 422);
      }

      const ctx = await accounts.getContext(String(accountId));
      const topics = parseSubNodeSpecs(subNodes);

      // 1) черновик в БД (PROVISIONING)
      const draft = await repo.createNode({
        title: String(title),
        username: username ? String(username) : null,
        description: description ? String(description) : null,
        creatorAccountId: String(accountId),
      });
      nodeId = draft.id;

      // 2) создаём мегагруппу+форум в Telegram
      const ws = await guardNode(repo, draft, () =>
        engine.createNodeWorkspace(ctx, String(title), {
          username: username ? String(username) : undefined,
          description: description ? String(description) : undefined,
        }),
      );
      let node = await repo.updateNode(draft.id, {
        workspaceTelegramId: ws.workspaceTelegramId,
        accessHash: ws.accessHash,
        username: ws.username,
        title: ws.title,
      });

      // 3) провижиним подузлы (форум-топики)
      let subs: Array<{ topicTelegramId: string; title: string; type: SubNodeType }> = [];
      if (topics.length > 0) {
        const created = await guardNode(repo, node, () =>
          engine.provisionSubNodes(ctx, node, topics),
        );
        subs = created;
        await repo.addSubNodes(node.id, created);
      }

      // 4) ACTIVE + лог
      node = await repo.updateNode(node.id, { status: NodeStatus.ACTIVE });
      await repo.addLog(node.id, "CREATE", `Node «${node.title}» создан`, {
        workspaceTelegramId: node.workspaceTelegramId,
        subNodes: subs.length,
      });

      res.status(201).json({ node, subNodes: subs });
    } catch (e) {
      if (nodeId) {
        const err = e instanceof NodeApiError ? e : classifyError(e);
        await repo
          .updateNode(nodeId, {
            status: err.accountStatus ?? NodeStatus.DEAD,
          })
          .catch(() => undefined);
      }
      sendError(res, e);
    }
  });

  // ── TOGGLE: ACTIVE ⇄ PAUSED (состояние прослушивания хуков) ──────────────
  router.post("/api/nodes/:id/toggle", async (req: Request, res: Response) => {
    try {
      const node = await repo.getNode(req.params.id);
      if (!node) throw new NodeApiError("NOT_FOUND", "Node не найден", 404);
      if (node.status === NodeStatus.DEAD) {
        throw new NodeApiError("NODE_DEAD", "Мёртвый узел нельзя переключать", 409);
      }
      const next =
        node.status === NodeStatus.ACTIVE ? NodeStatus.PAUSED : NodeStatus.ACTIVE;
      const updated = await repo.updateNode(node.id, { status: next });
      await repo.addLog(node.id, "TOGGLE", `Статус → ${next}`);
      res.json({ id: updated.id, status: updated.status });
    } catch (e) {
      sendError(res, e);
    }
  });

  // ── DELETE: снос в Telegram + каскадная очистка БД ──────────────────────
  router.delete("/api/nodes/:id", async (req: Request, res: Response) => {
    try {
      const node = await repo.getNode(req.params.id);
      if (!node) throw new NodeApiError("NOT_FOUND", "Node не найден", 404);
      const ctx = await accounts.getContext(node.creatorAccountId);

      if (node.workspaceTelegramId && node.accessHash) {
        try {
          await guardNode(repo, node, () => engine.deleteNode(ctx, node));
        } catch (e) {
          // если канал уже удалён в Telegram — продолжаем чистку БД
          const err = e instanceof NodeApiError ? e : classifyError(e);
          if (!/CHANNEL_INVALID|CHANNEL_PRIVATE|MTPROTO_ERROR/.test(err.code)) throw err;
        }
      }
      await repo.deleteNode(node.id);
      res.json({ id: node.id, deleted: true });
    } catch (e) {
      sendError(res, e);
    }
  });

  // ── BROADCAST: системное сообщение во все подузлы ───────────────────────
  router.post("/api/nodes/:id/broadcast", async (req: Request, res: Response) => {
    try {
      const message = String(req.body?.message ?? "").trim();
      if (!message) throw new NodeApiError("BAD_REQUEST", "Пустое сообщение", 422);
      const node = await repo.getNode(req.params.id);
      if (!node) throw new NodeApiError("NOT_FOUND", "Node не найден", 404);
      if (node.status !== NodeStatus.ACTIVE) {
        throw new NodeApiError("NODE_NOT_ACTIVE", `Node в статусе ${node.status}`, 409);
      }
      const ctx = await accounts.getContext(node.creatorAccountId);
      const subs = await repo.getSubNodes(node.id);
      if (subs.length === 0) {
        throw new NodeApiError("NO_SUBNODES", "У узла нет подузлов для рассылки", 409);
      }
      const results = await guardNode(repo, node, () =>
        engine.broadcast(ctx, node, subs, message),
      );
      await repo.addLog(node.id, "BROADCAST", `Разослано в ${results.length} подузлов`, {
        delivered: results.length,
      });
      res.json({ delivered: results.length, results });
    } catch (e) {
      sendError(res, e);
    }
  });

  // ── INVITE: генерация/ротация инвайт-ссылки ─────────────────────────────
  router.post("/api/nodes/:id/invite", async (req: Request, res: Response) => {
    try {
      const node = await repo.getNode(req.params.id);
      if (!node) throw new NodeApiError("NOT_FOUND", "Node не найден", 404);
      const ctx = await accounts.getContext(node.creatorAccountId);
      const { link } = await guardNode(repo, node, () =>
        engine.exportInvite(ctx, node, {
          expireSeconds: req.body?.expireSeconds,
          usageLimit: req.body?.usageLimit,
          title: req.body?.title,
        }),
      );
      await repo.addLog(node.id, "INVITE", "Сгенерирована инвайт-ссылка");
      res.json({ link });
    } catch (e) {
      sendError(res, e);
    }
  });

  // ── LOGS: история активности для UI-карточек ────────────────────────────
  router.get("/api/nodes/:id/logs", async (req: Request, res: Response) => {
    try {
      const node = await repo.getNode(req.params.id);
      if (!node) throw new NodeApiError("NOT_FOUND", "Node не найден", 404);
      const limit = Number(req.query.limit ?? 100);
      const logs = await repo.getLogs(node.id, Number.isFinite(limit) ? limit : 100);
      res.json({ nodeId: node.id, logs });
    } catch (e) {
      sendError(res, e);
    }
  });

  return router;
}
