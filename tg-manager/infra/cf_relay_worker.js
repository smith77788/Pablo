/**
 * CF Relay Worker — WebSocket → TCP proxy для Telegram DC
 *
 * Маршрутизирует WebSocket соединения от BotMother серверов к Telegram DC серверам.
 * С точки зрения Telegram трафик приходит с Cloudflare edge IP, а не с Railway.
 *
 * Деплой:
 *   1. Создайте новый Worker на https://dash.cloudflare.com/
 *   2. Вставьте этот скрипт в редактор Worker
 *   3. Сохраните и скопируйте URL worker'а (например: https://tg-relay.your-name.workers.dev)
 *   4. Установите CF_RELAY_URL=https://tg-relay.your-name.workers.dev в Railway
 *
 * Формат URL запроса:
 *   wss://tg-relay.your-name.workers.dev/{dc_id}
 *   dc_id: 1-5 (номер Telegram DC)
 *
 * Безопасность:
 *   Опционально добавьте AUTH_TOKEN в Worker Secrets и проверяйте заголовок
 *   X-Relay-Token при каждом подключении (см. раздел AUTH ниже).
 *
 * Ограничения бесплатного плана Cloudflare Workers:
 *   - 100k запросов в день (каждое WebSocket соединение = 1 запрос)
 *   - CPU time: 10ms per request (достаточно для инициализации соединения)
 *   - Длительность соединения: до 30 секунд без активности (heartbeat обходит это)
 *   При необходимости перейдите на Cloudflare Workers Paid ($5/мес, неограниченно).
 */

// Telegram DC → IP маппинг (production DCs)
const DC_IPS = {
  1: "149.154.175.53",
  2: "149.154.167.51",
  3: "149.154.175.100",
  4: "149.154.167.91",
  5: "91.108.56.130",
};

// Порт Telegram (443 = TLS-like, стандартный)
const TG_PORT = 443;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Health check endpoint
    if (url.pathname === "/" || url.pathname === "/health") {
      return new Response(
        JSON.stringify({ status: "ok", relay: "cf-tg-relay" }),
        { headers: { "Content-Type": "application/json" } }
      );
    }

    // Только WebSocket upgrade
    const upgrade = request.headers.get("Upgrade");
    if (!upgrade || upgrade.toLowerCase() !== "websocket") {
      return new Response("WebSocket upgrade required", { status: 426 });
    }

    // Опциональная аутентификация через X-Relay-Token
    // Раскомментируйте если хотите защитить relay от посторонних
    /*
    const AUTH_TOKEN = env.RELAY_AUTH_TOKEN;
    if (AUTH_TOKEN) {
      const clientToken = request.headers.get("X-Relay-Token") || "";
      if (clientToken !== AUTH_TOKEN) {
        return new Response("Unauthorized", { status: 401 });
      }
    }
    */

    // Парсим DC ID из URL пути: /2 → dc_id=2
    const pathParts = url.pathname.split("/").filter(Boolean);
    const dcId = parseInt(pathParts[0] || "2", 10);
    const dcHost = DC_IPS[dcId] || DC_IPS[2];

    // Создаём WebSocket пару
    const [clientWS, serverWS] = Object.values(new WebSocketPair());
    serverWS.accept();

    // Подключаемся к Telegram DC через TCP (cloudflare:sockets API)
    let tcpSocket;
    try {
      const { connect } = await import("cloudflare:sockets");
      tcpSocket = connect({ hostname: dcHost, port: TG_PORT });
    } catch (err) {
      serverWS.close(1011, "Failed to connect to Telegram DC");
      return new Response(null, { status: 101, webSocket: clientWS });
    }

    const writer = tcpSocket.writable.getWriter();
    const reader = tcpSocket.readable.getReader();

    // WebSocket → TCP: данные от клиента идут в Telegram
    serverWS.addEventListener("message", async (event) => {
      try {
        const data =
          event.data instanceof ArrayBuffer
            ? new Uint8Array(event.data)
            : new TextEncoder().encode(event.data);
        await writer.write(data);
      } catch (_) {
        // TCP сокет закрылся
      }
    });

    // TCP → WebSocket: данные от Telegram идут клиенту
    (async () => {
      try {
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          serverWS.send(value);
        }
      } catch (_) {
        // соединение разорвано
      } finally {
        try {
          serverWS.close(1000, "Telegram DC closed connection");
        } catch (_) {}
      }
    })();

    // Закрытие WebSocket → закрываем TCP
    serverWS.addEventListener("close", async () => {
      try {
        await writer.close();
      } catch (_) {}
      try {
        tcpSocket.close();
      } catch (_) {}
    });

    serverWS.addEventListener("error", async () => {
      try {
        await writer.close();
      } catch (_) {}
      try {
        tcpSocket.close();
      } catch (_) {}
    });

    return new Response(null, { status: 101, webSocket: clientWS });
  },
};
