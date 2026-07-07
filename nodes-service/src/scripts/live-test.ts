/**
 * Живой smoke полного цикла против Telegram. Запускать у себя, на своём аккаунте
 * (лучше — одноразовом на тестовом DC, см. login.js --test).
 *
 *   TG_API_ID=... TG_API_HASH=... NODE_TEST_SESSION='<session>' node dist/scripts/live-test.js
 *
 * Проходит: createNodeWorkspace → provisionSubNodes → exportInvite → broadcast
 * → deleteNode. НИЧЕГО не пишет в БД — только движок и MTProto. В конце узел
 * удаляется, чтобы не оставлять мусора.
 */
import { NodeClientFactory } from "../NodeClientFactory.js";
import { TelegramNodeEngine } from "../TelegramNodeEngine.js";
import {
  AccountContext,
  TelegramNodeRecord,
  SubNodeRecord,
  NodeStatus,
  SubNodeType,
} from "../types.js";
import { env, exit } from "node:process";

async function main(): Promise<void> {
  const apiId = Number(env.TG_API_ID);
  const apiHash = env.TG_API_HASH ?? "";
  const session = env.NODE_TEST_SESSION ?? "";
  if (!apiId || !apiHash || !session) {
    console.error("Нужны TG_API_ID, TG_API_HASH, NODE_TEST_SESSION.");
    exit(1);
  }

  const ctx: AccountContext = { accountId: "live-test", apiId, apiHash, session };
  const engine = new TelegramNodeEngine(new NodeClientFactory({ minDelayMs: 2000, maxDelayMs: 3000 }));

  console.log("1) createNodeWorkspace …");
  const ws = await engine.createNodeWorkspace(ctx, "{{Node Server|Cluster Node}} — LIVE TEST", {
    description: "smoke test — будет удалён",
  });
  console.log("   →", ws);

  const node: TelegramNodeRecord = {
    id: "live",
    workspaceTelegramId: ws.workspaceTelegramId,
    accessHash: ws.accessHash,
    title: ws.title,
    username: ws.username,
    description: null,
    creatorAccountId: ctx.accountId,
    status: NodeStatus.ACTIVE,
    createdAt: new Date(),
  };

  console.log("2) provisionSubNodes (Logs/Metrics/Commands) …");
  const subs = await engine.provisionSubNodes(ctx, node, [
    { title: "Logs", type: SubNodeType.LOGS },
    { title: "Metrics", type: SubNodeType.METRICS },
    { title: "Commands", type: SubNodeType.COMMANDS },
  ]);
  console.log("   →", subs);

  console.log("3) exportInvite …");
  console.log("   →", await engine.exportInvite(ctx, node));

  console.log("4) broadcast во все подузлы …");
  const subRecords: SubNodeRecord[] = subs.map((s, i) => ({
    id: String(i),
    nodeId: node.id,
    topicTelegramId: s.topicTelegramId,
    title: s.title,
    type: s.type,
    createdAt: new Date(),
  }));
  console.log("   →", await engine.broadcast(ctx, node, subRecords, "✅ live smoke"));

  console.log("5) deleteNode (уборка) …");
  await engine.deleteNode(ctx, node);
  console.log("   → удалён. Полный цикл ПРОЙДЕН.");
  exit(0);
}

main().catch((e) => {
  console.error("LIVE TEST FAIL:", e);
  exit(1);
});
