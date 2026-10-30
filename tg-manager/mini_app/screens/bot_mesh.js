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
      return `<div class="li tap" onclick="openBotMeshTask('${esc(t.task_id)}')">
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
      <div style="font-size:13px">Запустил: ${esc(d.origin_name || 'неизвестно')}</div>
      <div style="font-size:13px">Передач: ${num(d.depth||0)}</div>
      ${d.drop_reason?`<div style="font-size:13px;color:var(--orange)">Брошена: ${esc(meshDropText(d.drop_reason))}</div>`:''}
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
      return `<div class="li">
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
