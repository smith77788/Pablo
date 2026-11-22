// Сетка ботов: кто с кем связан, задачи сетки и их повтор.
//
// Вынесено из index.html без изменения поведения. Обычный классический
// скрипт, грузится после основного блока и делит с ним глобальную область.

// Приёмная половина Bot Mesh была написана целиком, а НАЧАТЬ цепочку было
// нечем: `bot_mesh.make_envelope`/`create_task` не вызывал никто. Поэтому
// таблица всегда пустая, и экран честно показывал «Задач между ботами нет» —
// и всегда бы показывал. Отсюда цепочка запускается.
async function meshBots() {
  if (!BOTS || !BOTS.length) { const d = await api('/api/miniapp/bots'); BOTS = d.bots||[]; }
  return (BOTS||[]).filter(b => b.bot_id);
}

function meshBotLabel(b) {
  return b.username ? '@'+b.username : (b.first_name || ('бот #'+b.bot_id));
}

async function openBotMeshMore() { MESH_OFFSET = MESH_ROWS.length; await openBotMesh(true); }

async function openBotMesh(keep) {
  if (!keep) { MESH_ROWS = []; MESH_OFFSET = 0; push('s-botmesh'); }
  if (!keep) txt('botmeshList','<div class="spin-wrap"><div class="spin"></div></div>');
  try {
    const d = await api('/api/miniapp/mesh/tasks?limit=30&offset='+MESH_OFFSET);
    const _page = listOf(d,'tasks');
    MESH_ROWS = MESH_OFFSET ? MESH_ROWS.concat(_page) : _page;
    const tasks = MESH_ROWS;
    const total = totalOf(d, tasks);
    const cntLbl = total ? num(total)+' '+plural(total,'задача','задачи','задач') : '';
    txt('botmeshCount', cntLbl);
    txt('m-botmesh-val', cntLbl);
    if (!tasks.length) {
      txt('botmeshList', empty('🔗','Цепочек ещё не было',
        'Цепочка — это задача, которую боты передают друг другу по маршруту. Запустите первую и увидите здесь, где она сейчас.',
        {label:'+ Запустить цепочку', fn:'meshNewTask()'}));
      return;
    }
    txt('botmeshList', tasks.map(t=>{
      const [cls, lbl] = MESH_ST[t.status] || ['b-gy', t.status||''];
      const steps = (t.route||[]).length;
      const where = steps
        ? `шаг ${num(Math.min((t.step||0)+1, steps))} из ${num(steps)}`
        : 'маршрут пуст';
      const path = (t.route||[])
        .map(r=>esc(r.bot_name || ('бот #'+r.bot)))
        .join(' → ');
      const drop = t.drop_reason ? ' · '+esc(meshDropText(t.drop_reason)) : '';
      return `<div class="li tap" onclick="openBotMeshTask('${escJs(t.task_id)}')">
        <div class="ava ava-blue">🔗</div>
        <div class="li-body">
          <div class="li-name">${path || 'Задача без маршрута'}</div>
          <div class="li-sub">${esc(where)} · передач ${num(t.depth||0)}${drop}</div>
          <div class="li-sub" style="opacity:.7">${ago(t.created_at)}</div>
        </div>
        <span class="badge ${cls}">${lbl}</span>
      </div>`;
    }).join('') + moreBtn(tasks.length, total, 'openBotMeshMore()'));
  } catch(e) { txt('botmeshList', errHtml(errRu(e), 'openBotMesh()')); }
}

function retryBotMeshTask() { if (_MESH_TASK_ID) openBotMeshTask(_MESH_TASK_ID, true); }

async function openBotMeshTask(taskId, keep) {
  _MESH_TASK_ID = taskId;
  if (!keep) push('s-botmeshdetail');
  txt('botmeshDetail','<div class="spin-wrap"><div class="spin"></div></div>');
  try {
    const d = await api('/api/miniapp/mesh/task/'+encodeURIComponent(taskId));
    openBotMeshTask.last = d;
    const [cls, lbl] = MESH_ST[d.status] || ['b-gy', d.status||''];
    const hops = d.hops||[];
    txt('botmeshStop', d.status === 'running'
      ? '<button class="btn btn-d" style="padding:7px 14px;font-size:13px" onclick="meshStopTask()">Прервать</button>'
      : '');
    const head = `<div class="card" style="margin-bottom:10px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <span class="badge ${cls}">${lbl}</span>
        <span style="font-size:12px;color:var(--hint)">${ago(d.created_at)}</span>
      </div>
      <div style="font-size:13px">Запустил: ${d.origin_id
        ? `<span onclick="openBot(${Number(d.origin_id)})" role="button" tabindex="0"
             style="color:var(--accent);cursor:pointer">${esc(d.origin_name || ('бот #'+d.origin_id))}</span>`
        : esc(d.origin_name || 'неизвестно')}</div>
      <div style="font-size:13px">Передач: ${num(d.depth||0)}</div>
      ${d.drop_reason?`<div style="font-size:13px;color:var(--orange)">Брошена: ${esc(meshDropText(d.drop_reason))}</div>`:''}
      ${d.text?`<div style="font-size:12px;color:var(--hint);margin-top:4px;white-space:pre-wrap">«${esc(d.text)}»</div>`:''}
      ${meshCanRepeat(d)?'<button class="btn btn-s" style="margin-top:8px;font-size:13px" onclick="meshRepeatTask()">🔁 Повторить цепочку</button>':''}
    </div>`;
    if (!hops.length) {
      txt('botmeshDetail', head + empty('🔗','Передач ещё не было',
        'Задача создана, но первый шаг пока не выполнен'));
      return;
    }
    const OUT = {accepted:['b-bl','принято'], handled:['b-gr','выполнено'],
                 dropped:['b-or','отброшено'], failed:['b-rd','ошибка']};
    txt('botmeshDetail', head + '<div class="lst">' + hops.map(h=>{
      const [oc, ol] = OUT[h.outcome] || ['b-gy', h.outcome||''];
      // Шаг трассы был текстом: бота, который принял работу, открыть нельзя.
      // Дверь ведёт к исполнителю (to_id), а если его карточки нет — к тому,
      // кто передал: это единственные два бота, о которых шаг что-то знает.
      const goId = h.to_id || h.from_id || 0;
      const go = goId ? ` onclick="openBot(${Number(goId)})" role="button" tabindex="0" style="cursor:pointer"` : '';
      return `<div class="li"${go}>
        <div class="ava ava-purple">${num((h.step||0)+1)}</div>
        <div class="li-body">
          <div class="li-name">${esc(h.from_name)} → ${esc(h.to_name)}</div>
          <div class="li-sub">${h.capability?esc(h.capability)+' · ':''}${ago(h.created_at)}</div>
          ${h.reason?`<div class="li-sub" style="color:var(--orange)">${esc(meshDropText(h.reason))}</div>`:''}
        </div>
        <span class="badge ${oc}">${ol}</span>
      </div>`;
    }).join('') + '</div>');
  } catch(e) { txt('botmeshDetail', errHtml(errRu(e), 'retryBotMeshTask()')); }
}

// Повтор цепочки. Одинаковую цепочку собирали заново: бот-инициатор, каждый
// шаг маршрута по одному и текст — до десяти выборов ради уже известного
// ответа. Сервер отдаёт маршрут, только если все его боты — ваши.
function meshCanRepeat(d) {
  return !!(d && d.origin_id && Array.isArray(d.route_ids) && d.route_ids.length);
}

async function meshRepeatTask() {
  const d = openBotMeshTask.last;
  if (!meshCanRepeat(d)) return;
  const names = {};
  for (const h of d.hops || []) {
    if (h.from_id) names[h.from_id] = h.from_name;
    if (h.to_id) names[h.to_id] = h.to_name;
  }
  const path = d.route_ids.map(id => names[id] || ('бот #' + id)).join(' → ');
  const ok = await askConfirm('Запустить такую же цепочку ещё раз?\n\n' +
    (d.origin_name || ('бот #' + d.origin_id)) + ' → ' + path +
    (d.text ? '\n\nТекст: ' + d.text.slice(0, 200) : ''));
  if (!ok) return;
  try {
    const r = await api('/api/miniapp/mesh/task', {method: 'POST', body: JSON.stringify({
      origin_bot: Number(d.origin_id), route: d.route_ids.map(b => ({bot: Number(b)})), text: d.text || ''})});
    if (r.sent === false) toast('⚠️ ' + (r.error || 'Telegram не принял передачу'));
    else toast('✅ Цепочка запущена снова');
  } catch (e) { toast('⚠️ ' + errRu(e, 'Не удалось запустить')); return; }
  // Назад к списку без второго шага в истории: список уже лежит под этим
  // экраном, его нужно только перечитать с начала.
  back();
  MESH_ROWS = []; MESH_OFFSET = 0;
  openBotMesh(true);
}
