// Поле меняется только после действия владельца: неизвестное не значит «выкл».
const BOT_SETTINGS = {id:0, sequence:0, fields:[], changed:{}, invite:''};
async function openBotSettings(id, applied=false) {
  const state=BOT_SETTINGS, sequence=++state.sequence;
  state.id=id; state.fields=[]; state.changed={}; state.invite='';
  document.getElementById('botSettingsSave').disabled=true;
  document.getElementById('botSettingsStatus').textContent='';
  document.getElementById('botSettingsFields').innerHTML='<div class="spin-wrap"><div class="spin"></div></div>';
  document.getElementById('botSettingsIdentity').textContent='Загрузка настроек…';
  document.getElementById('botSettingsInviteBlock').style.display='none';
  showModal('mo-bot-settings');
  try {
    const data=await api('/api/miniapp/bot/'+encodeURIComponent(id)+'/settings');
    if(sequence!==state.sequence) return;
    state.fields=data.fields||[];
    document.getElementById('botSettingsIdentity').textContent=[data.bot?.first_name,...[...new Set([data.bot?.username,...(data.bot?.active_usernames||[])].filter(Boolean))].map(x=>'@'+x)].filter(Boolean).join(' · ')||'Бот';
    if(/^https:\/\/t\.me\/botinvite\/[A-Za-z0-9_]+$/.test(data.invite_link||'')) {
      state.invite=data.invite_link;
      const link=document.getElementById('botSettingsInvite'); link.href=state.invite; document.getElementById('botSettingsInviteBlock').style.display='block';
    }
    document.getElementById('botSettingsFields').innerHTML=state.fields.map((field,index)=>botSettingsField(field,index,(data.settings||{})[field.key])).join('');
    document.getElementById('botSettingsSave').disabled=false;
  } catch(error) {
    if(sequence!==state.sequence) return;
    document.getElementById('botSettingsFields').textContent='';
    document.getElementById('botSettingsIdentity').textContent='Настройки Telegram';
    document.getElementById('botSettingsStatus').textContent=(applied?'Настройки применены, но перечитать их не удалось. ':'')+errRu(error,'Не удалось загрузить настройки. Повторите проверку.');
  }
}
function botSettingsField(field,index,value) {
  const known=value!==null&&value!==undefined;
  const id='botSetting_'+index;
  const change='botSettingsChange('+index+',this.value)';
  let input;
  if(field.type==='boolean'||field.type==='bool') {
    input=`<select id="${esc(id)}" class="inp" onchange="${esc(change)}"><option value="">Не менять${known?'':' · значение неизвестно'}</option><option value="true">Включить</option><option value="false">Выключить</option></select>`;
  } else if(Array.isArray(field.choices)) {
    const labels={default:'Обычный',auto:'Автоматически',inline:'Без сообщения',0:'Не получать',1:'Все',10:'Каждое десятое',100:'Каждое сотое',1000:'Каждое тысячное'};
    input=`<select id="${esc(id)}" class="inp" onchange="${esc(change)}"><option value="">Не менять</option>${field.choices.map(x=>`<option value="${esc(x)}">${esc(labels[x]??x)}</option>`).join('')}</select>`;
  } else {
    input=`<input id="${esc(id)}" class="inp" maxlength="1024" value="${esc(known?value:'')}" oninput="${esc(change)}" placeholder="${esc(known?'':'Telegram не сообщил значение')}">`;
  }
  const current=known?botSettingsValue(field,value):'неизвестно';
  return `<div class="field"><label class="field-label" for="${esc(id)}">${esc(field.label||field.key)}</label><div class="field-note">Сейчас: ${esc(current)}</div>${input}</div>`;
}
function botSettingsValue(field,value) {
  if(typeof value==='boolean') return value?'включено':'выключено';
  if(field.key==='callback_query_mode') return {default:'обычный',auto:'автоматический',inline:'без сообщения'}[value]||String(value);
  return String(value)||'по умолчанию';
}
function botSettingsChange(index,value) {
  const field=BOT_SETTINGS.fields[index]; if(!field) return;
  if(value===''&&(field.type==='boolean'||field.type==='bool'||Array.isArray(field.choices))) delete BOT_SETTINGS.changed[field.key];
  else BOT_SETTINGS.changed[field.key]=(field.type==='boolean'||field.type==='bool')?value==='true':(field.type==='integer'||field.type==='int')?Number(value):value;
  document.getElementById('botSettingsStatus').textContent='Изменено полей: '+Object.keys(BOT_SETTINGS.changed).length;
}
async function saveBotSettings() {
  const state=BOT_SETTINGS, id=state.id, sequence=state.sequence, patch={...state.changed};
  if(!Object.keys(patch).length) { toast('Выберите настройки для изменения'); return; }
  const lines=state.fields.filter(x=>Object.hasOwn(patch,x.key)).map(x=>(x.label||x.key)+': '+botSettingsValue(x,patch[x.key]));
  if(!await askConfirm('Изменить настройки бота?\n\n'+lines.join('\n'))) return;
  if(sequence!==state.sequence) return;
  document.getElementById('botSettingsSave').disabled=true;
  try {
    await api('/api/miniapp/bot/'+encodeURIComponent(id)+'/settings',{method:'POST',body:JSON.stringify({settings:patch})});
    if(sequence!==state.sequence) return;
    toast('Настройки применены');
    await openBotSettings(id,true);
  } catch(error) {
    if(sequence!==state.sequence) return;
    document.getElementById('botSettingsStatus').textContent=errRu(error,'Не удалось изменить настройки');
    document.getElementById('botSettingsSave').disabled=false;
  }
}
function copyBotInvite() { if(BOT_SETTINGS.invite) copyText(BOT_SETTINGS.invite); }
