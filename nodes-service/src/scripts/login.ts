/**
 * Локальный логин → печатает StringSession. Запускать ТОЛЬКО у себя.
 * Сессия — это полный доступ к аккаунту: не публикуйте и не вставляйте в чаты.
 *
 *   npm run build
 *   TG_API_ID=... TG_API_HASH=... node dist/scripts/login.js
 *   # для одноразового аккаунта на тестовом дата-центре Telegram:
 *   TG_API_ID=... TG_API_HASH=... node dist/scripts/login.js --test
 */
import { TelegramClient } from "telegram";
import { StringSession } from "telegram/sessions/index.js";
import { createInterface } from "node:readline/promises";
import { stdin, stdout, argv, env, exit } from "node:process";

async function main(): Promise<void> {
  const apiId = Number(env.TG_API_ID);
  const apiHash = env.TG_API_HASH ?? "";
  if (!apiId || !apiHash) {
    console.error("Задайте TG_API_ID и TG_API_HASH (https://my.telegram.org).");
    exit(1);
  }
  const testServers = argv.includes("--test"); // тестовый DC = одноразовый аккаунт
  const rl = createInterface({ input: stdin, output: stdout });

  const client = new TelegramClient(new StringSession(""), apiId, apiHash, {
    connectionRetries: 5,
    testServers,
  });

  await client.start({
    phoneNumber: async () => (await rl.question("Телефон (+…): ")).trim(),
    password: async () => (await rl.question("2FA пароль (Enter если нет): ")).trim(),
    phoneCode: async () => (await rl.question("Код из Telegram: ")).trim(),
    onError: (e) => console.error("Ошибка авторизации:", e.message),
  });

  const session = (client.session as StringSession).save();
  console.log("\n=== STRING SESSION (храните как пароль, никому не отправляйте) ===\n");
  console.log(session);
  console.log(
    "\nЖивой тест (у себя):\n" +
      `  TG_API_ID=${apiId} TG_API_HASH=*** NODE_TEST_SESSION='<session>' ` +
      `node dist/scripts/live-test.js${testServers ? " --test" : ""}\n`,
  );

  await client.disconnect();
  await client.destroy();
  rl.close();
  exit(0);
}

main().catch((e) => {
  console.error(e);
  exit(1);
});
