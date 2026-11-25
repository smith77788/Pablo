// 🗄 Предложение подключить Хранилище диалогов — на видном месте.
//
// РАЗРЫВ. Хранилище — главное умение продукта (бот хранит личную переписку,
// включая удалённое собеседником), но предлагал его только копайлот
// «Что дальше» с приоритетом 72. Экран показывает ПЯТЬ подсказок, а выше
// семьдесят второго приоритета их больше десяти, поэтому у владельца с живым
// флотом предложение не попадало в выдачу ни разу. В боте кнопка была только
// в чеклисте быстрого старта — то есть исчезала после активации.
//
// Полоса ни с чем не соревнуется: она видна ровно тем, у кого хранилище не
// подключено, и уходит сама, как только подключение появилось. «Позже»
// прячет её на неделю на этом устройстве — не навсегда, потому что
// подключение занимает минуту и возвращаться к нему стоит.
const VAULT_PROMO_SNOOZE_KEY = 'infragram_vault_promo_until';
const VAULT_PROMO_SNOOZE_DAYS = 7;

function vaultPromoSnoozed() {
  try {
    const until = Number(localStorage.getItem(VAULT_PROMO_SNOOZE_KEY) || 0);
    return until > Date.now();
  } catch (_) { return false; }
}

function vaultPromoLater() {
  try {
    localStorage.setItem(VAULT_PROMO_SNOOZE_KEY,
                         String(Date.now() + VAULT_PROMO_SNOOZE_DAYS * 86400000));
  } catch (_) {}
  const box = document.getElementById('vaultPromo');
  if (box) { box.style.display = 'none'; box.innerHTML = ''; }
}

// Статус спрашиваем сами и тихо: сбой не должен ни ломать главный экран, ни
// показывать предложение тому, у кого хранилище уже работает (ответ неизвестен
// — значит, ничего не показываем).
async function vaultPromoHome() {
  const box = document.getElementById('vaultPromo');
  if (!box || vaultPromoSnoozed()) return;
  let st;
  try { st = await api('/api/miniapp/vault/status'); } catch (_) { return; }
  if (!st || st.connected || st.read_only) { box.style.display = 'none'; return; }
  const bu = st.bot_username ? '@' + esc(st.bot_username) : 'нашего бота';
  box.style.display = '';
  box.innerHTML = `<div class="ui-promo">
      <div class="ui-promo-ico" aria-hidden="true">🗄</div>
      <div class="ui-promo-body">
        <div class="ui-promo-title">Подключите Хранилище диалогов</div>
        <div class="ui-promo-sub">Бот сохранит ваши личные переписки — даже то,
          что собеседник удалил или исправил, — и сообщит об этом. Подключение
          ${bu} занимает минуту.</div>
        <div class="ui-promo-act">
          <button class="btn btn-p" onclick="openVaultConnect()">🔌 Подключить</button>
          <button class="btn btn-s" onclick="vaultPromoLater()">Позже</button>
        </div>
      </div>
    </div>`;
}
