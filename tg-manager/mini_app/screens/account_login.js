// Подключение аккаунта: телефон, QR и tdata.
//
// Вынесено из инлайн-скрипта mini_app/index.html (он вырос до 29895 строк, из
// них 22452 — один <script>; CLAUDE.md держит это как техдолг и просит
// выносить экраны в mini_app/screens/*.js). Поведение при переносе не
// менялось: тела функций перенесены дословно вместе с комментариями.
//
// Здесь три способа подключить аккаунт и всё, что к ним прилипло:
//   * телефон — старт, код, двухфакторный пароль, повтор кода и
//     авто-подтягивание кода из другого аккаунта;
//   * QR — старт, опрос состояния, двухфакторный пароль, остановка опроса;
//   * tdata и файл сессии — загрузка.
//
// Файл подключается <script src> в конце index.html, то есть ПОСЛЕ
// инлайн-скрипта. Это важно и безопасно: внутри только объявления функций
// (они поднимаются), а общие помощники инлайн-скрипта — api, toast, esc,
// errRu, showModal — вызываются в момент клика, когда оба скрипта уже
// отработали.

function accStopQrPoll(){ if(_accQrTimer){ clearInterval(_accQrTimer); _accQrTimer=null; } }

function accSetLoginMethod(m){
  accStopQrPoll(); accPhoneAutoStop(); _accErr('');
  document.getElementById('accImportResult').style.display='none';
  ['string','phone','qr','file','tdata'].forEach(k=>{
    const sec=document.getElementById('accMethod-'+k); if(sec) sec.style.display = (k===m)?'block':'none';
    const tab=document.getElementById('accTab-'+k); if(tab) tab.classList.toggle('btn-p', k===m);
  });
  // сброс шагов
  ['accPhoneStep-code','accPhoneStep-2fa','accQrStep-2fa'].forEach(id=>{const el=document.getElementById(id); if(el) el.style.display='none';});
  const ps=document.getElementById('accPhoneStep-phone'); if(ps) ps.style.display='block';
  // Админ-поле темпа автоповтора показываем только админу и только для входа по номеру.
  const adm=document.getElementById('accPhoneResendAdmin');
  if(adm){
    if(m==='phone' && window.IS_ADMIN){
      adm.style.display='block';
      const inp=document.getElementById('accPhoneRpm');
      try{ const v=localStorage.getItem('acc_resend_rpm'); if(inp&&v) inp.value=v; }catch(_){}
    } else adm.style.display='none';
  }
  const qb=document.getElementById('accQrBox'); if(qb) qb.innerHTML='<button class="btn btn-p" id="accQrStartBtn" onclick="accQrStart()" style="width:100%">🔲 Показать QR-код</button>';
  const qh=document.getElementById('accQrHint'); if(qh) qh.style.display='none';
  _accLoginPhone=''; _accLoginHash='';
}

async function accPhoneStart(){
  _accErr('');
  const phone=normalizePhone(document.getElementById('accPhoneNum').value);
  if(!phone){ _accErr('Введите номер телефона (7–15 цифр), например +71234567890'); return; }
  document.getElementById('accPhoneNum').value=phone;  // показать приведённый вид
  const b=document.getElementById('accPhoneStartBtn'); b.disabled=true; b.textContent='⏳ Отправляю…';
  try{
    const r=await api('/api/miniapp/account/add/phone/start',{method:'POST',body:JSON.stringify({phone,proxy_id:_accProxy().id})});
    _accLoginPhone=phone; _accLoginHash=r.phone_code_hash;
    document.getElementById('accPhoneStep-code').style.display='block';
    toast(r.hint||'Код отправлен');
  }catch(e){ _accErr(String(e&&e.message||e)); }
  finally{ b.disabled=false; b.textContent='📨 Отправить код'; }
}

async function accPhoneCode(){
  _accErr(''); const code=(document.getElementById('accPhoneCode').value||'').trim();
  if(!code){ _accErr('Введите код из Telegram'); return; }
  const b=document.getElementById('accPhoneCodeBtn'); b.disabled=true; b.textContent='⏳ Проверяю…';
  try{
    const r=await api('/api/miniapp/account/add/phone/code',{method:'POST',body:JSON.stringify({phone:_accLoginPhone,code,phone_code_hash:_accLoginHash,proxy_id:_accProxy().id})});
    accPhoneAutoStop();  // код принят — автоповтор больше не нужен
    if(r.needs_2fa){ document.getElementById('accPhoneStep-2fa').style.display='block'; toast('❗ Требуется пароль 2FA'); }
    else _accDone(r);
  }catch(e){ _accErr(String(e&&e.message||e)); }
  finally{ b.disabled=false; b.textContent='✅ Подтвердить код'; }
}

async function accPhone2fa(){
  _accErr(''); const password=document.getElementById('accPhone2fa').value||'';
  if(!password){ _accErr('Введите пароль 2FA'); return; }
  const b=document.getElementById('accPhone2faBtn'); b.disabled=true; b.textContent='⏳ Проверяю…';
  try{
    const r=await api('/api/miniapp/account/add/phone/2fa',{method:'POST',body:JSON.stringify({phone:_accLoginPhone,password,proxy_id:_accProxy().id})});
    _accDone(r);
  }catch(e){ _accErr(String(e&&e.message||e)); }
  finally{ b.disabled=false; b.textContent='🔓 Подтвердить 2FA'; }
}

// Один повтор. Возвращает {flood}|{error}|{ok,timeout} — автоповтор по этому
// решает, крутиться дальше или остановиться.
async function accPhoneResend(){
  _accErr('');
  if(!_accLoginPhone||!_accLoginHash){ _accErr('Сессия входа истекла — отправьте код заново'); return {error:true}; }
  const b=document.getElementById('accPhoneResendBtn'); const prev=b?b.textContent:'';
  if(b){ b.disabled=true; b.textContent='⏳ Запрашиваю…'; }
  try{
    // proxy_id тут не нужен и не присылается: повторный запрос кода идёт тем
    // же самым клиентом Telethon, что просил код в первый раз (он лежит в
    // account_manager._pending по номеру), а значит и тем же прокси. Присылать
    // ключ, который сервер не читает, — повод однажды решить, что прокси
    // учтён, хотя учитывает его совсем другое место.
    const r=await api('/api/miniapp/account/add/phone/resend',{method:'POST',body:JSON.stringify({phone:_accLoginPhone,phone_code_hash:_accLoginHash})});
    if(r&&r.ok===false&&r.flood){
      _accResendNote(r.hint||'Telegram попросил паузу — подождите и попробуйте снова.');
      toast('⏳ '+(r.hint||'Поймали флуд — подождите'));
      return {flood:true, retry_after:(r.retry_after||0)};
    }
    if(r&&r.phone_code_hash) _accLoginHash=r.phone_code_hash;  // хэш после ResendCode меняется
    _accResendNote((r&&r.hint)?r.hint:'Код запрошен повторно. Введите его, как получите.');
    toast((r&&r.hint)?r.hint:'Код запрошен повторно');
    return {ok:true, timeout:((r&&r.timeout)||0)};
  }catch(e){
    _accErr(String(e&&e.message||e));
    return {error:true};
  }finally{ if(b){ b.disabled=false; b.textContent=prev||'🔁 Запросить ещё раз'; } }
}

function accPhoneAutoStop(){
  _accAutoRunning=false;
  if(_accAutoTimer){ clearTimeout(_accAutoTimer); _accAutoTimer=null; }
  const b=document.getElementById('accPhoneAutoBtn');
  if(b){ b.textContent='♾️ Повторять, пока не придёт'; b.classList.remove('btn-p'); b.classList.add('btn-s'); b.disabled=false; }
}

async function accPhoneAutoToggle(){
  if(_accAutoRunning){ accPhoneAutoStop(); toast('⏹ Автоповтор остановлен'); return; }
  if(!_accLoginPhone||!_accLoginHash){ _accErr('Сессия входа истекла — отправьте код заново'); return; }
  _accAutoRunning=true;
  const b=document.getElementById('accPhoneAutoBtn');
  if(b){ b.textContent='⏹ Остановить автоповтор'; b.classList.remove('btn-s'); b.classList.add('btn-p'); }
  const tick=async()=>{
    if(!_accAutoRunning) return;
    const t0=Date.now();
    const res=await accPhoneResend();
    if(!_accAutoRunning) return;                 // остановили во время запроса
    if(!res||res.flood||res.error){ accPhoneAutoStop(); return; }  // флуд/ошибка — стоп
    // Темп: админ-настройка «запросов в минуту» (если задана) или рекомендация
    // Telegram, но не реже раза в 15 с. Из паузы ВЫЧИТАЕМ время самого запроса —
    // так фактический темп держит заданное значение запросов/мин независимо от
    // того, сколько отвечал сервер (а не «интервал + время запроса»).
    const target=_accResendDelayMs(res.timeout);
    const wait=Math.max(target-(Date.now()-t0),0);
    const rpmShown=_accResendRpm()||(target>0?Math.round(60000/target):0);
    _accResendNote('Автоповтор включён. Темп ~'+rpmShown+' запросов/мин (ограничит только FloodWait). Введите код, как только он придёт.');
    _accAutoTimer=setTimeout(tick, wait);
  };
  tick();
}

// Вход по QR ────────────────────────────────────────────
async function accQrStart(){
  _accErr(''); const box=document.getElementById('accQrBox');
  box.innerHTML='⏳ Генерирую QR…';
  try{
    const r=await api('/api/miniapp/account/add/qr/start',{method:'POST',body:JSON.stringify({})});
    box.innerHTML='<img src="'+r.qr_png+'" alt="QR" style="width:200px;height:200px;border-radius:12px;background:#fff;padding:8px">';
    document.getElementById('accQrHint').style.display='block';
    accStopQrPoll();
    _accQrTimer=setInterval(accQrPoll, 3000);
  }catch(e){ _accErr(String(e&&e.message||e)); box.innerHTML='<button class="btn btn-p" onclick="accQrStart()" style="width:100%">↺ Повторить</button>'; }
}

async function accQrPoll(){
  try{
    const r=await api('/api/miniapp/account/add/qr/poll',{method:'POST',body:JSON.stringify({proxy_id:_accProxy().id})});
    if(r.status==='done'){ _accDone(r); }
    else if(r.status==='need_2fa'){ accStopQrPoll(); document.getElementById('accQrStep-2fa').style.display='block'; toast('❗ Требуется пароль 2FA'); }
    // pending → ждём следующий тик
  }catch(e){ accStopQrPoll(); _accErr(String(e&&e.message||e)); }
}

async function accQr2fa(){
  _accErr(''); const password=document.getElementById('accQr2fa').value||'';
  if(!password){ _accErr('Введите пароль 2FA'); return; }
  const b=document.getElementById('accQr2faBtn'); b.disabled=true; b.textContent='⏳ Проверяю…';
  try{
    const r=await api('/api/miniapp/account/add/qr/2fa',{method:'POST',body:JSON.stringify({password,proxy_id:_accProxy().id})});
    _accDone(r);
  }catch(e){ _accErr(String(e&&e.message||e)); }
  finally{ b.disabled=false; b.textContent='🔓 Подтвердить 2FA'; }
}

async function accFileUpload(){
  _accErr(''); const f=document.getElementById('accFileInput').files[0];
  if(!f){ _accErr('Выберите файл .session'); return; }
  const b=document.getElementById('accFileBtn'); b.disabled=true; b.textContent='⏳ Импортирую…';
  try{ _accDone(await _accUpload('/api/miniapp/account/add/session_file', f)); }
  catch(e){ _accErr(String(e&&e.message||e)); }
  finally{ b.disabled=false; b.textContent='📂 Загрузить и импортировать'; }
}

async function accTdataUpload(){
  _accErr(''); const f=document.getElementById('accTdataInput').files[0];
  if(!f){ _accErr('Выберите ZIP-архив tdata'); return; }
  if(f.size>50*1024*1024){ _accErr('Архив больше 50 МБ'); return; }
  const b=document.getElementById('accTdataBtn'); b.disabled=true; b.textContent='⏳ Конвертирую…';
  try{ _accDone(await _accUpload('/api/miniapp/account/add/tdata', f)); }
  catch(e){ _accErr(String(e&&e.message||e)); }
  finally{ b.disabled=false; b.textContent='📦 Загрузить и конвертировать'; }
}
