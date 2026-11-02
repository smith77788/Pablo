// 📲 Мини-апп на главном экране телефона (Telegram Bot API 8.0).
//
// Лидеры (ManyChat, amoCRM) живут на главном экране как приложение: открыл
// иконку — и ты в работе. Мини-апп до сих пор открывался только через бот:
// найти чат, нажать «Открыть». Telegram с версии 8.0 умеет положить ярлык
// мини-аппа на главный экран (tg.addToHomeScreen) и сказать, лежит ли он
// уже там (tg.checkHomeScreenStatus).
//
// Строка в настройках показывает честное состояние: уже добавлено, можно
// добавить, или клиент Telegram этого не умеет (тогда — что обновить).
function _hsSupported() {
  try { return !!(tg && tg.isVersionAtLeast && tg.isVersionAtLeast('8.0') && tg.addToHomeScreen); }
  catch (_) { return false; }
}
function _hsRender(status) {
  const lbl = document.getElementById('set-home-status');
  const btn = document.getElementById('set-home-btn');
  if (!lbl || !btn) return;
  if (!_hsSupported()) {
    lbl.textContent = 'Обновите Telegram: ярлык доступен с версии 8.0';
    btn.style.display = 'none';
    return;
  }
  if (status === 'added') {
    lbl.textContent = 'Уже на главном экране';
    btn.style.display = 'none';
  } else if (status === 'unsupported') {
    lbl.textContent = 'Это устройство не поддерживает ярлыки';
    btn.style.display = 'none';
  } else {
    lbl.textContent = 'Открывать одним касанием, без чата с ботом';
    btn.style.display = '';
  }
}
function loadHomeScreenStatus() {
  _hsRender('unknown');
  if (!_hsSupported() || !tg.checkHomeScreenStatus) return;
  try { tg.checkHomeScreenStatus(st => _hsRender(st)); } catch (_) {}
}
function addAppToHomeScreen() {
  if (!_hsSupported()) { _hsRender('unknown'); return; }
  try { tg.addToHomeScreen(); } catch (_) { toast('⚠️ Telegram не открыл добавление ярлыка'); }
}
try {
  if (tg && tg.onEvent) {
    tg.onEvent('homeScreenAdded', () => { _hsRender('added'); toast('📲 Ярлык добавлен на главный экран'); });
    tg.onEvent('homeScreenChecked', e => _hsRender(e && e.status));
  }
} catch (_) {}
