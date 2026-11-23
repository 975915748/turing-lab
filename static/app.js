'use strict';
const $ = (selector) => document.querySelector(selector);
const app = $('#app');
const state = {me:null,room:null,roomId:null,selectedRound:null,ws:null,reconnect:null,tab:'login',legacyLogin:false,roomView:'match',drafts:{},typing:{},lastTyping:null,accounts:[],packetPanels:{}};
const esc = (v) => String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const shortDate = s => s ? new Date(s).toLocaleString('zh-CN',{hour12:false}) : '';
const val = (id) => document.getElementById(id)?.value??'';
const D = (txt='暂无记录') => `<p class="muted hint">${esc(txt)}</p>`;
let toastTimer;
function toast(message){const el=$('#toast');el.textContent=message;el.classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('visible'),4200)}
function setConnection(txt,online){const el=$('#connection');el.textContent=(online?'● ':'○ ')+txt;el.className='connection'+(online?' online':'')}
function applyPrefs(){let theme=localStorage.getItem('turing_theme')||'light';document.body.dataset.theme=theme;$('#themeBtn').textContent=theme==='light'?'☾ 深色':'☀ 日间';let f=Number(localStorage.getItem('turing_font')||15);if(f<12||f>22)f=15;document.documentElement.style.setProperty('--font',f+'px');$('#fontSize').value=f;$('#fontValue').textContent=f;}
$('#themeBtn').addEventListener('click',()=>{localStorage.setItem('turing_theme',document.body.dataset.theme==='light'?'dark':'light');applyPrefs()});
$('#fontSize').addEventListener('input',e=>{localStorage.setItem('turing_font',e.target.value);applyPrefs()});
$('#logoutBtn').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST'});}catch(e){toast(e.message)} state.me=null;state.room=null;state.tab='login';state.legacyLogin=false;stopWs();history.replaceState(null,'',location.pathname);await init()});
async function api(path,{method='GET',body,raw=false}={}){
 const opts={method,headers:{}};
 if(body!==undefined){opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body)}
 if(method!=='GET'&&state.me?.user?.csrf)opts.headers['X-CSRF-Token']=state.me.user.csrf;
 if(method!=='GET'&&state.room&&path.startsWith(`/api/rooms/${state.room.id}/`))opts.headers['X-Match-Number']=String(state.room.match_number);
 const response=await fetch(path,opts);
 if(raw){if(!response.ok)throw new Error('下载失败');return response.blob()}
 const data=await response.json().catch(()=>({detail:'网络连接错误'}));
 if(!response.ok){if(response.status===401&&state.me){state.me=null;state.room=null;state.roomId=null;stopWs();renderAuth();}throw new Error(typeof data.detail==='string'?data.detail:'请求失败');}
 return data;
}
function stopWs(){state.lastTyping=null;if(state.reconnect)clearTimeout(state.reconnect);state.reconnect=null; if(state.ws){const ws=state.ws;state.ws=null;ws.onclose=null;ws.close();}setConnection('未连接',false)}
function connectWs(roomId){stopWs();if(state.roomId!==roomId)return;
 const proto=location.protocol==='https:'?'wss:':'ws:';
 const ws=new WebSocket(`${proto}//${location.host}/ws/rooms/${roomId}`);state.ws=ws;
 ws.onopen=()=>{setConnection('实时同步中',true);sendTypingFromDraft()};
 ws.onmessage=async e=>{let m;try{m=JSON.parse(e.data)}catch{return}if(m.type==='room_updated' && state.roomId===roomId){await refreshRoom();}
 if(m.type==='typing_status'&&state.roomId===roomId){state.typing=m.round===state.room?.current_round&&m.match_number===state.room?.match_number?(m.states||{}):{};updateTypingBadges()}};
 ws.onclose=e=>{if(state.roomId!==roomId||state.ws!==ws)return;if(e.code===1008){state.roomId=null;state.room=null;state.typing={};history.replaceState(null,'',location.pathname);loadLobby().then(()=>toast(e.reason==='房间已删除'?'房间已删除':'房间访问权限已失效或你已被移出')).catch(err=>toast(err.message));return;}setConnection('重连中',false);state.reconnect=setTimeout(()=>connectWs(roomId),3000)};
 ws.onerror=()=>setConnection('连接异常',false);
}
async function refreshRoom(){if(!state.roomId)return;try{const r=await api(`/api/rooms/${state.roomId}`);if(!state.room||r.current_round!==state.room.current_round||r.match_number!==state.room.match_number){state.selectedRound=r.current_round;state.lastTyping=null;state.typing={};}
 if(state.room&&(r.match_number!==state.room.match_number||(!state.room.records_deleted&&r.records_deleted))){for(const key of Object.keys(state.drafts))if(key.startsWith(`${r.id}:`))delete state.drafts[key];const dialog=$('#judgeNotice');if(dialog?.open)dialog.close();state.roomView='match';}
 state.room=r;renderRoom();maybeNotifyJudge();}catch(e){if(/不属于这个房间/.test(e.message)){state.roomId=null;state.room=null;await loadLobby();toast('你已不在该房间');}else toast(e.message)}}
async function init(){applyPrefs();try{state.me=await api('/api/me');$('#logoutBtn').classList.remove('hide');const q=/^#room=(\d+)$/.exec(location.hash);if(q){await openRoom(Number(q[1]));}else renderLobby()}catch(e){state.me=null;state.roomId=null;$('#logoutBtn').classList.add('hide');renderAuth();}}
function renderAuth(){
 stopWs();$('#logoutBtn').classList.add('hide');setConnection('未登录',false);
 const reg=state.tab==='register',legacy=!!state.legacyLogin;
 app.innerHTML=`<div class="auth"><div class="intro"><div class="hero-mark">T.</div><div class="overline">HUMAN × AI / ANONYMOUS JUDGING</div><h1>在同一个房间里，<br>验证谁更像人类。</h1><p class="muted">支持真人裁判独立登录与 AI 裁判由房主手动转录；各裁判独立判断。AI 回答仍由房主手动录入；全部参赛者提交后统一公布回答。</p><div class="stat-row"><div class="stat">◈ 自由注册</div><div class="stat">◈ 匿名裁判包</div><div class="stat">◈ 完整轮次日志</div></div></div>
 <section class="card"><div class="overline">欢迎加入</div><h2 style="margin:7px 0 0">${reg?'创建你的账号':'登录图灵实验室'}</h2>
 <div class="tabs"><button data-action="tab-login" class="${reg?'':'active'}">已有账号登录</button><button data-action="tab-register" class="${reg?'active':''}">注册账号</button></div>
 <form data-form="${reg?'register':'login'}">
 ${reg?'<label class="field" for="authUsername">用户名</label><input id="authUsername" name="username" type="text" autocomplete="nickname" required maxlength="40" placeholder="请输入用户名。"><p class="hint">注册后平台会分配唯一 UID。</p>':
 `<label class="field" for="authIdentity">${legacy?'旧登录名':'UID'}</label><input id="authIdentity" name="${legacy?'username':'uid'}" type="text" autocomplete="username" required maxlength="${legacy?30:21}" spellcheck="false" placeholder="${legacy?'输入升级前使用的登录名':'例如 TL00000001'}">`}
 <label class="field" for="authPassword">密码${reg?'（至少 8 位）':''}</label><input id="authPassword" name="password" type="password" autocomplete="${reg?'new-password':'current-password'}" ${reg?'minlength="8"':''} required maxlength="200" placeholder="输入密码">
 <button class="primary" type="submit">${reg?'注册':'登录'}</button></form>
 ${reg?'<p class="hint spaced">注册无需邀请码。加入比赛时，再输入房主提供的房间邀请码。</p>':`<p class="hint spaced">${legacy?'仅升级前的账号支持旧登录名；登录后可在大厅查看 UID。':'旧账号也已分配 UID，可先用原登录名登录查看。'}</p><button class="small" data-action="toggle-legacy-login">${legacy?'使用 UID 登录':'使用旧登录名'}</button>`}
 </section></div>`;
}
function renderLobby(){
 stopWs();state.roomId=null;state.room=null;state.roomView='match';state.typing={};$('#logoutBtn').classList.remove('hide');setConnection('大厅',false);
 const isAdmin=!!state.me?.user?.is_admin;
 const completed=state.me.finished_rooms||[];
 app.innerHTML=`<div class="pagehead"><div><div class="overline">比赛大厅 / MATCH LOBBY</div><h1>欢迎，${esc(state.me.user.display_name)}</h1><p class="muted">${isAdmin?'管理员账号 · 可管理所有房间及用户账号':'创建自己的房间，或加入其他人的比赛'}</p></div><div class="buttonrow"><span class="pill">${isAdmin?'管理员':'人类玩家'}</span>${isAdmin?'<button data-action="manage-users">用户管理</button>':''}</div></div>
 <section class="notice" style="margin-bottom:20px"><div class="label-line"><div><div class="tiny muted">你的登录 UID</div><strong class="code" id="accountUid">${esc(state.me.user.uid)}</strong></div><button class="small" data-action="copy-uid" data-uid="${esc(state.me.user.uid)}">复制 UID</button></div><p class="hint" style="margin:8px 0 0">请保存此 UID，下次使用它和密码登录。</p></section>
 <div class="grid2"><section class="card"><h2>加入已有房间</h2><form data-form="join"><label class="field">邀请码</label><input name="code" maxlength="20" required type="text" placeholder="例如 A1B2C3D4E5"><div class="form-actions"><button class="primary" type="submit">加入比赛 →</button></div></form></section>
 <section class="card"><h2>创建一场图灵测试</h2><form data-form="room"><label class="field">比赛名称</label><input type="text" name="title" maxlength="80" required placeholder="例如 人机辨识实验 #1"><label class="field">旧版手工转录裁判名（多裁判模式不使用）</label><input type="text" name="judge" maxlength="40" value="裁判" required><div class="form-actions"><button class="primary" type="submit">＋ 创建房间</button></div></form></section></div>
 <section class="spaced"><div class="label-line"><h2>${isAdmin?'全部房间':'我的房间'}</h2><span class="pill">${state.me.rooms.length} 个</span></div><div class="roomcards">${state.me.rooms.length?state.me.rooms.map(r=>`<div class="card flat room-card"><div class="overline">ROOM ${r.id}</div><h3>${esc(r.title)}</h3><div class="muted hint">${r.status==='finished'?'比赛已结束':'第 '+r.current_round+' 轮进行中'}${r.role==='judge'?' · 裁判席':r.role==='owner'?' · 我是房主':''}</div><div class="buttonrow"><button data-action="open" data-id="${r.id}">进入房间 →</button>${r.can_manage?`<button class="small danger" data-action="delete-room" data-id="${r.id}" data-match="${r.match_number}">删除房间</button>`:''}</div></div>`).join(''):D('尚未加入任何房间，可以创建房间或输入邀请码。')}</div></section>
 ${completed.length?`<section class="spaced"><h2>已完赛房间记录</h2><p class="hint">任何已登录用户都可以清空这些房间的比赛记录。清空对所有人立即生效，房间本身保留。</p><div class="roomcards">${completed.map(r=>`<div class="card flat room-card"><div class="overline">ROOM ${r.id} · 第 ${r.match_number} 场</div><h3>${esc(r.title)}</h3><button class="danger small" data-action="delete-records" data-id="${r.id}" data-match="${r.match_number}">删除所有人的比赛记录</button></div>`).join('')}</div></section>`:''}`;
}
async function loadAccounts(){
 const data=await api('/api/admin/users');state.accounts=data.users;stopWs();state.roomId=null;state.room=null;history.replaceState(null,'',location.pathname);setConnection('用户管理',false);renderAccounts();
}
function renderAccounts(){
 app.innerHTML=`<div class="pagehead"><div><div class="overline">ADMIN / ACCOUNTS</div><h1>用户管理</h1><p class="muted">按唯一 UID 管理账号；修改密码后，该账号的所有登录会话立即失效。</p></div><button data-action="back">← 返回大厅</button></div>
 <div class="roomcards">${state.accounts.map(u=>`<section class="card flat" data-account-uid="${esc(u.uid)}"><div class="label-line"><h2>${esc(u.display_name)}</h2><span class="pill">${u.is_admin?'管理员':'用户'}</span></div><div class="code">${esc(u.uid)}</div><p class="hint">用户名：${esc(u.username)}</p><div class="buttonrow"><button class="small" data-action="toggle-password-reset" data-uid="${esc(u.uid)}">修改密码</button><button class="small danger" data-action="delete-account" data-uid="${esc(u.uid)}">删除账号</button></div>
 <form data-form="reset-password" data-uid="${esc(u.uid)}" class="hide spaced"><label class="field" for="password-${u.id}">新密码（至少 8 位）</label><input id="password-${u.id}" type="password" name="password" required minlength="8" maxlength="200" autocomplete="new-password"><div class="form-actions"><button type="submit">确认修改密码</button></div></form></section>`).join('')}</div>`;
}
function finishedActions(r){
 if(r.status!=='finished')return '';
 return `<div class="buttonrow spaced" style="margin-bottom:20px">${r.can_manage?'<button class="primary" data-action="restart-match">再次开赛</button>':''}${r.records_deleted?'':`<button class="danger" data-action="delete-records" data-id="${r.id}" data-match="${r.match_number}">删除所有人的比赛记录</button>`}</div>`;
}
function renderReadyRoom(r){
 app.innerHTML=`<div class="pagehead"><div><div class="overline">ROOM ${r.id} · 第 ${r.match_number} 场</div><h1>${esc(r.title)}</h1></div><button data-action="back">← 返回大厅</button></div><section class="card"><h2>${r.can_manage?'开始下一场比赛':'等待再次开赛'}</h2>${finishedActions(r)}</section>`;
}
async function openRoom(id){state.roomId=id;state.selectedRound=null;state.roomView='match';try{const r=await api(`/api/rooms/${id}`);state.room=r;state.typing={};state.selectedRound=r.current_round;history.replaceState(null,'',`#room=${id}`);renderRoom();connectWs(id);maybeNotifyJudge();}catch(e){state.roomId=null;state.room=null;history.replaceState(null,'',location.pathname);toast(e.message);await loadLobby()}}
async function loadLobby(){state.me=await api('/api/me');renderLobby()}
const myDraft=(name,defaultText='')=>esc(state.drafts[`${state.roomId}:${state.room?.match_number}:${state.selectedRound}:${name}`]??defaultText);
function sendTypingFromDraft(){
 const r=state.room;
 if(!r?.mine||r.status!=='open'||r.rounds.find(x=>x.number===r.current_round)?.status!=='collecting')return;
 const answer=state.drafts[`${r.id}:${r.match_number}:${r.current_round}:answer`]||'';
 sendTyping(answer.trim().length>0);
}
function sendTyping(active){
 if(!state.room?.mine||!state.ws||state.ws.readyState!==WebSocket.OPEN)return;
 if(state.lastTyping===!!active)return;
 state.lastTyping=!!active;
 state.ws.send(JSON.stringify({type:'typing',active:!!active,round:state.room.current_round,match_number:state.room.match_number}));
}
function updateTypingBadges(){
 const r=state.room;if(!r?.mine)return;
 const current=r.rounds.find(x=>x.number===state.selectedRound);
 if(!current)return;
 const submitted=new Set(current.submitted);
 for(const el of app.querySelectorAll('[data-typing-alias]')){
  const alias=el.dataset.typingAlias;
  const completed=submitted.has(alias);
  el.textContent=completed?'√':(current.status==='collecting'&&state.typing[alias]?'....':'X');
  el.title=completed?'已提交':(state.typing[alias]?'输入中':'未输入');
  el.classList.toggle('ok',completed);
 }
}
function maybeNotifyJudge(){
 const r=state.room;
 // 真人裁判可直接查看匿名回复；AI 裁判需主持人代录，因此主持人也要收到提醒。
 if(!r || r.records_deleted || (!r.is_judge && !r.can_manage))return;
 const rd=r.rounds?.find(x=>x.number===r.current_round);
 // 不依赖比赛是否结束：最后一轮已提交、随后结算也不能漏掉提醒。
 if(!rd || rd.status!=='revealed')return;
 const key=`judge-ready-v1.5-${r.id}-${r.match_number}-${rd.number}-${state.me?.user?.id||0}-${r.is_judge?'human-judge':'host'}`;
 if(sessionStorage.getItem(key))return;
 const dialog=$('#judgeNotice');
 const message=r.is_judge
  ? `第 ${rd.number} 轮全部参赛者已提交，可以查看匿名回答并继续裁判。`
  : `第 ${rd.number} 轮全部参赛者已提交。如有 AI 裁判，请将本轮匿名回答转发给对应 AI 裁判。`;
 if(!dialog){toast(message);return;}
 if(dialog.open)return;
 const label=$('#judgeNoticeText');
 if(label)label.textContent=message;
 try{
  dialog.showModal();
  // 只有确认显示成功才标记，避免弹窗异常后永远不再提醒。
  sessionStorage.setItem(key,'1');
 }catch(e){toast(message);}
}
function makeRoster(r){const submitted=new Set(r.rounds.find(x=>x.number===state.selectedRound)?.submitted||[]);
 const people=[...r.humans.map(h=>({...h,role:'人类',label:h.name})),...r.ais.map(a=>({...a,role:'AI',label:a.name}))].sort((a,b)=>a.alias.localeCompare(b.alias));
 return people.map(p=>`<div class="roster-person"><div class="person-left"><span class="alias">${esc(p.alias)}</span><div style="min-width:0"><span class="person-name" title="${esc(p.label)}">${esc(p.label)}</span><span class="roster-role">${p.role==='人类'?'人类 · '+esc(p.uid):'AI 参赛位'}${p.alias===r.mine?' · 我':''}</span></div></div><span class="pill ${submitted.has(p.alias)?'ok':''}" ${p.role==='人类'&&r.mine?`data-typing-alias="${esc(p.alias)}"`:''} title="本轮提交状态">${submitted.has(p.alias)?'√':p.role==='人类'&&r.mine?(state.selectedRound===r.current_round&&state.typing[p.alias]?'....':'X'):'—'}</span></div>`).join('')||D('等待人类玩家加入');}
function modelProfileOptions(r){
 const profiles=r.model_profiles||[];
 return `<option value="">手动转录（不调用 API）</option>${profiles.map(p=>`<option value="${p.id}">${esc(p.name)} · ${esc(p.model)}</option>`).join('')}`;
}
function judgeAdminPanel(r){
 const judges=r.judges||[];
 const allDone=judges.length>0&&judges.every(j=>j.locked);
 const pendingRound=r.rounds.find(x=>x.number===r.current_round);
 const hasQuestion=j=>pendingRound?.questions?.some(q=>q.kind==='ai'&&q.judge_id===j.id);
 const pendingAis=judges.filter(j=>j.kind==='ai'&&!j.model_profile_id&&!hasQuestion(j));
 const autoPending=judges.filter(j=>j.kind==='ai'&&j.model_profile_id&&!hasQuestion(j));
 const awaitingAiVerdict=judges.filter(j=>j.kind==='ai'&&!j.locked);
 const manualAwaitingAiVerdict=awaitingAiVerdict.filter(j=>!j.model_profile_id);
 const autoAwaitingAiVerdict=awaitingAiVerdict.filter(j=>j.model_profile_id);
 const aliases=[...r.humans,...r.ais].map(x=>x.alias).sort();
 return `<section class="card compact"><div class="label-line"><h2>真人 / AI 裁判管理</h2><span class="pill">${judges.length} 位裁判</span></div>
 <p class="hint">真人裁判独立登录。AI 裁判可绑定模型 API 自动出题和判断，也可继续使用手动转录。裁判的最终判决在揭晓前互相隔离。</p>
 ${autoPending.length?`<div class="divider"></div><p class="hint">${autoPending.length} 位模型 AI 裁判尚未提交第 ${r.current_round} 轮问题。</p><button class="primary" data-action="generate-ai-questions">让模型自动生成本轮问题</button>`:''}
 ${pendingRound?.status==='draft'&&pendingAis.length?`<div class="divider"></div><form data-form="ai-judge-question"><h3>代录 AI 裁判的第 ${r.current_round} 轮问题</h3><p class="hint">每位 AI 裁判每轮只能提交一次。</p><select name="judge_id">${pendingAis.map(j=>`<option value="${j.id}">${esc(j.label)}</option>`).join('')}</select><textarea name="text" data-draft="ai-judge-question" maxlength="3000" required placeholder="粘贴 AI 裁判提出的问题原文…">${myDraft('ai-judge-question')}</textarea><div class="form-actions"><button class="primary" type="submit">提交 AI 裁判问题</button></div></form>`:''}
 <div class="feed">${judges.length?judges.map(j=>`<div class="feed-item"><div class="label-line"><strong>${esc(j.label)}</strong><span class="pill ${j.locked?'ok':''}">${j.locked?'✓ 已锁定':'未锁定'}</span></div><div class="tiny muted">${j.kind==='ai'?'AI 裁判 · '+(j.model_profile_id?'模型 API':'手动转录'):'真人裁判 · '+esc(j.uid)}</div>${j.kind==='human'?`<button class="small spaced" data-action="copy-uid" data-uid="${esc(j.uid)}">复制 UID</button>`:''}</div>`).join(''):D('尚无裁判。可以添加真人裁判，或直接添加 AI 裁判席。')}</div>
 ${!r.revealed_at&&!r.verdict_locked&&!judges.some(j=>j.locked)?`<div class="divider"></div><form data-form="add-ai-judge"><h3>添加 AI 裁判</h3><label class="field">AI 裁判名称</label><input type="text" name="name" required maxlength="60" placeholder="例如 DeepSeek-R1"><label class="field">模型配置</label><select name="model_profile_id">${modelProfileOptions(r)}</select><div class="form-actions"><button class="primary" type="submit">＋ 添加 AI 裁判</button></div></form><div class="divider"></div><form data-form="add-judge"><h3>创建真人裁判登录账号</h3><label class="field">裁判用户名（可重名）</label><input type="text" name="username" required maxlength="40" placeholder="例如 一号裁判"><label class="field">裁判显示名</label><input type="text" name="display_name" maxlength="40" required placeholder="例如 一号裁判"><label class="field">初始密码（至少8位）</label><input name="password" type="password" minlength="8" maxlength="200" required autocomplete="new-password"><div class="form-actions"><button type="submit">＋ 创建并添加裁判</button></div></form><div class="divider"></div><form data-form="assign-judge"><h3>绑定已有账号</h3><p class="hint">适合同一位裁判参加其他房间。不能绑定本房间房主、参赛者或管理员账号。</p><label class="field">用户 UID</label><input type="text" name="uid" required maxlength="21" placeholder="例如 TL00000001" autocomplete="off" spellcheck="false"><label class="field">在本房间显示的裁判名</label><input type="text" name="label" required maxlength="40" placeholder="例如 二号裁判"><div class="form-actions"><button type="submit">绑定已有账号</button></div></form>`:''}
 ${pendingRound?.status==='draft'&&(judges.length>0||pendingRound.questions?.length)?`<div class="divider"></div><p class="hint">裁判问题提交后，房主确认并开始统一作答。</p><button class="primary" data-action="start-round">开始统一作答</button>`:''}
 ${!r.revealed_at&&r.rounds.every(x=>x.status==='revealed')&&autoAwaitingAiVerdict.length?`<div class="divider"></div><p class="hint">${autoAwaitingAiVerdict.length} 位模型 AI 裁判尚未完成最终判断。</p><button class="primary" data-action="generate-ai-verdicts">让模型自动完成最终判断</button>`:''}
 ${!r.revealed_at&&r.rounds.every(x=>x.status==='revealed')&&manualAwaitingAiVerdict.length?`<div class="divider"></div><form data-form="ai-judge-verdict"><h3>代录 AI 裁判最终判断</h3><p class="hint">只为未接入模型的 AI 裁判手动转录。</p><label class="field">选择 AI 裁判</label><select name="judge_id">${manualAwaitingAiVerdict.map(j=>`<option value="${j.id}">${esc(j.label)}</option>`).join('')}</select>${aliases.map(a=>`<div class="label-line" style="margin:8px 0"><label>匿名参赛者 ${esc(a)}</label><select name="pred-${esc(a)}" required style="max-width:130px"><option value="">请选择</option><option value="Human">人类</option><option value="AI">AI</option></select></div>`).join('')}<label class="field">AI 裁判判断依据</label><textarea name="evidence" maxlength="5000" required placeholder="复制 AI 裁判的判断依据…"></textarea><label class="field">置信度（0～100）</label><input name="confidence" type="number" min="0" max="100" value="50" required><label class="field">最弱环节（可选）</label><textarea name="weakness" maxlength="5000"></textarea><label class="field">可能误判原因（可选）</label><textarea name="possible_error" maxlength="5000"></textarea><div class="form-actions"><button class="primary" type="submit">锁定该 AI 裁判判断</button></div></form>`:''}
 ${!r.revealed_at&&judges.length?`<div class="divider"></div><h3>独立裁判判决</h3><p class="hint">${judges.filter(j=>j.locked).length}/${judges.length} 位真人及 AI 裁判已锁定。所有已创建轮次的回答必须公布，且每位裁判锁定判断后才能揭晓。</p>${allDone?`<button class="danger" data-action="reveal">全部裁判已锁定 · 正式揭晓</button>`:''}`:''}
 </section>`;
}
function renderJudgeRoom(r){
 const n=state.selectedRound||r.current_round;
 const round=r.rounds.find(x=>x.number===n)||r.rounds[r.rounds.length-1];
 const active=n===r.current_round;
 const finished=r.status==='finished';
 const questionSubmitted=round.questions?.some(x=>x.mine);
 const ready=r.rounds.every(x=>x.status==='revealed');
 const aliases=r.aliases||[];
 const canLock=!finished&&!r.my_verdict&&ready;
 const pending=r.rounds.find(x=>x.number===r.current_round)?.status==='draft';
 const questions=(round.questions||[]).map(q=>`<div class="feed-item"><strong>${esc(q.judge)}</strong><div class="feed-text">${esc(q.text)}</div></div>`).join('')||D('尚无裁判提交问题');
 const answers=round.status==='revealed'?aliases.map(a=>`<div class="answer-card"><strong>匿名参与者 ${esc(a)}</strong>${round.answers[a]?.edited_at?'<span class="pill">本轮答复已修订</span>':''}<div class="answer-content">${esc(round.answers[a]?.text||'（未提交）')}</div></div>`).join(''):
 `<div class="notice">🔒 尚未全部提交：${round.submitted.length}/${aliases.length}。在所有回答统一公开前，裁判同样不能读取任何回答正文。</div>`;
 const buttons=r.rounds.map(x=>`<button class="small ${x.number===n?'active':''}" data-action="round" data-id="${x.number}">第 ${x.number} 轮 ${x.status==='revealed'?'✓':''}</button>`).join('');
 const form=canLock?`<form data-form="judge-verdict"><p class="hint">只根据匿名回答判断。所有预测只保存在你的独立记录中，其他裁判始终无法通过网站读取。</p>${aliases.map(a=>`<div class="label-line" style="margin:8px 0"><label>参与者 ${esc(a)}</label><select name="pred-${esc(a)}" required><option value="">选择</option><option value="Human">人类</option><option value="AI">AI</option></select></div>`).join('')}<label class="field">判断依据</label><textarea name="evidence" data-draft="judge-evidence" required maxlength="5000">${myDraft('judge-evidence')}</textarea><label class="field">置信度 (0-100)</label><input name="confidence" type="number" min="0" max="100" value="50" required><label class="field">最弱环节（可选）</label><textarea name="weakness" maxlength="5000"></textarea><label class="field">可能误判原因（可选）</label><textarea name="possible_error" maxlength="5000"></textarea><div class="form-actions"><button class="primary" type="submit">锁定我自己的判断</button></div></form>`:
 r.my_verdict?`<div class="locked">🔒 你的判决已锁定，不能修改。其他裁判的判断对你保密。</div><div class="answer-card"><strong>我的判断</strong><div class="answer-content">${aliases.map(a=>a+'：'+(r.my_verdict.predictions[a]==='Human'?'人类':'AI')).join('\n')}</div><div class="hint">依据：${esc(r.my_verdict.evidence)}</div></div>`:
 D('待所有轮次的参赛者回答全部公开后才可提交最终判断。');
 app.innerHTML=`<div class="pagehead"><div><div class="overline">INDEPENDENT JUDGE / ROOM ${r.id}</div><h1>${esc(r.title)}</h1><div class="stat-row"><span class="pill">独立裁判 · ${esc(r.my_judge_label)}</span><span class="pill">${aliases.length} 位匿名参赛者</span><span class="pill">${finished?'比赛已结束':'匿名盲测中'}</span></div></div><div class="buttonrow"><button data-action="back" class="ghost">← 返回大厅</button><button data-action="sync">↻ 刷新</button></div></div>
 ${finishedActions(r)}<div class="room-layout"><aside class="aside"><section class="card flat"><h2>匿名参赛者</h2><p class="hint">仅显示固定代号，隐藏所有账号、昵称和真实身份。即使比赛结束，此视图也不显示真实身份。</p>${aliases.map(a=>`<div class="roster-person"><span class="alias">${esc(a)}</span><span>匿名参赛者</span></div>`).join('')}</section><section class="card flat"><h2>比赛轮次</h2><div class="round-tags">${buttons}</div></section></aside>
 <div class="stack"><section class="card"><div class="label-line"><h2>第 ${n} 轮问题</h2><span class="pill">${round.status==='draft'?'收集问题中':round.status==='collecting'?'等待答复':'回答已公开'}</span></div><p class="hint">当前独立裁判的题目将在房主开启本轮作答后汇总。</p><div class="feed">${questions}</div>${round.question?`<div class="question spaced">${esc(round.question)}</div>`:''}<div class="buttonrow spaced"><button data-action="download-log" class="small">↓ 下载匿名 Log</button>${finished?'<button data-action="download-full-log" class="small">↓ 下载整场匿名 Log</button>':''}</div></section>
 <section class="card"><h2>匿名回答</h2>${answers}</section><section class="card"><h2>参赛者提问与追问</h2><p class="hint">只显示匿名代号，不包含账号信息。</p><div class="feed">${r.suggestions.filter(s=>s.round_number===n).map(s=>`<div class="feed-item"><strong>${esc(s.alias)}</strong><div class="feed-text">${esc(s.text)}</div></div>`).join('')||D('暂无消息')}</div></section></div>
 <div class="right-panel"><section class="card"><h2>我的裁判操作</h2>${active&&pending&&!questionSubmitted?`<form data-form="judge-question"><p class="hint">每位裁判每轮最多提交一个问题，房主统一开启本轮作答。</p><textarea name="text" data-draft="judge-question" maxlength="3000" required placeholder="输入你要向所有参赛者提出的问题…">${myDraft('judge-queion')}</textarea><div class="form-actions"><button class="primary" type="submit">提交并实时同步问题</button></div></form>`:pending?D('你已提交本轮问题，等待房主开启作答。'):D('本轮已经开始作答，不能再添加新问题。')}
 <div class="divider"></div><h3>独立身份判决</h3>${form}</section></div></div>`;
}
function renderMultiResults(r,roster){
 return `<section class="card"><div class="overline">MULTI-JUDGE / RESULTS</div><h2>多裁判独立判决结果</h2><p class="hint">比赛结束后向人类参与者展示全部裁判判决及评分；各裁判登录时仍只能读取自己的判断。</p>${r.verdicts.map(v=>{const count=roster.filter(p=>v.predictions?.[p.alias]===(p.role==='AI'?'AI':'Human')).length;return `<div class="answer-card"><div class="label-line"><h3>${esc(v.judge)} <span class="tiny muted">${v.kind==='ai'?'AI 裁判':v.kind==='human'?'真人裁判':''}</span></h3><span class="pill ok">${count} / ${roster.length} 正确</span></div><div class="hint">置信度 ${v.confidence}%</div>${roster.map(p=>`<div class="hint">${esc(p.alias)} · ${p.role} · 判断：${v.predictions?.[p.alias]==='Human'?'人类':'AI'} ${v.predictions?.[p.alias]===(p.role==='AI'?'AI':'Human')?'✓':'✗'}</div>`).join('')}<div class="question spaced">${esc(v.evidence)}</div><p class="hint">最弱环节：${esc(v.weakness||'未填写')}</p><p class="hint">可能误判：${esc(v.possible_error||'未填写')}</p></div>`}).join('')}</section>`;
}
function renderAdminRoomNav(r,finished){
 return `<nav class="card flat room-view-menu" aria-label="房间页面导航"><div class="overline">房间导航 / NAVIGATION</div><h2>操作页面</h2>
 ${[['match','比赛现场','查看本轮问题与回答'],['judges','真人 / AI 裁判管理','管理裁判与自主判断'],['admin','房主管理台','管理参赛者与比赛'],['models','模型 API 接入','配置模型和 API Key']].map(([view,title,desc])=>`<button type="button" class="room-view-link ${state.roomView===view?'active':''}" data-action="room-view" data-view="${view}" ${state.roomView===view?'aria-current="page"':''}><strong>${title}</strong><span>${desc}</span></button>`).join('')}
 <div class="room-view-hint">第 ${r.current_round} 轮 · ${finished?'比赛已结束':'比赛进行中'}</div></nav>`;
}

function renderModelRoom(r){
 const profiles=r.model_profiles||[];
 const formFields=(p={})=>`<label class="field">配置名称</label><input name="name" maxlength="60" required value="${esc(p.name||'')}" placeholder="例如 DeepSeek 主力">
 <label class="field">Base URL</label><input name="base_url" maxlength="500" required value="${esc(p.base_url||'https://api.openai.com/v1')}" placeholder="https://api.openai.com/v1">
 <label class="field">模型名称</label><input name="model" maxlength="120" required value="${esc(p.model||'')}" placeholder="例如 gpt-4.1-mini">
 <label class="field">API Key ${p.has_api_key?'（已保存，留空保持不变）':''}</label><input name="api_key" type="password" maxlength="2000" autocomplete="new-password" ${p.has_api_key?'placeholder="已加密保存；不修改时留空" :'required placeholder="填写 API Key"'}>
 <div class="grid2"><div><label class="field">Temperature</label><input name="temperature" type="number" min="0" max="2" step="0.1" value="${p.temperature??0.7}" required></div><div><label class="field">最大输出 Token</label><input name="max_tokens" type="number" min="64" max="8192" step="1" value="${p.max_tokens??2048}" required></div></div>
 <label class="field">请求超时（秒）</label><input name="timeout_seconds" type="number" min="10" max="120" step="1" value="${p.timeout_seconds??60}" required>`;
 app.innerHTML=`<div class="pagehead"><div><div class="overline">MODEL API / ROOM ${r.id}</div><h1>模型 API 接入</h1><p class="muted">为本房间配置 OpenAI 兼容接口，并将配置分配给 AI 参赛者或 AI 裁判。</p></div><button data-action="room-view" data-view="match">← 返回比赛现场</button></div>
 ${r.model_api_enabled?'':`<section class="notice"><strong>服务器尚未配置 API Key 加密密钥</strong><p class="hint">请先在 Railway 的服务 Variables 中设置 MODEL_CREDENTIAL_KEY。可使用 Python 生成：<code>from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())</code>。密钥必须长期保存，不能在部署后随意更换。</p></section>`}
 <section class="card"><h2>新增模型配置</h2><p class="hint">支持标准 OpenAI Chat Completions 接口。Base URL 填到 /v1 这一层，不要填写 /chat/completions。</p><form data-form="model-profile">${formFields()}<div class="form-actions"><button class="primary" type="submit" ${r.model_api_enabled?'':'disabled'}>保存模型配置</button></div></form></section>
 <section class="spaced"><div class="label-line"><h2>已有配置</h2><span class="pill">${profiles.length} 个</span></div>
 ${profiles.length?profiles.map(p=>`<section class="card flat"><div class="label-line"><strong>${esc(p.name)}</strong><span class="pill">${p.has_api_key?'API Key 已保存':'缺少 API Key'}</span></div><form data-form="edit-model-profile" data-id="${p.id}">${formFields(p)}<div class="form-actions"><button type="submit">保存修改</button></div></form><div class="buttonrow spaced"><button class="small" data-action="test-model" data-id="${p.id}">测试连接</button><button class="small danger" data-action="delete-model" data-id="${p.id}">删除配置</button></div></section>`).join(''):D('还没有模型配置。添加配置后，就能在 AI 参赛位和 AI 裁判中选择它。')}
 </section><section class="notice spaced"><strong>密钥与连接安全</strong><p class="hint">API Key 在服务器端使用 Fernet 加密后写入 SQLite；浏览器只会收到“是否已保存”的状态，不会收到密钥或密文。仅本房间房主和全站管理员可以管理配置。当前仅允许 HTTPS 公网 Base URL，拒绝本机和局域网地址。</p></section>`;
}
function renderRoom(){
 const editing=document.activeElement;
 const editingDraft=editing?.dataset?.draft;
 const editingPos=typeof editing?.selectionStart==='number'?editing.selectionStart:null;
 const r=state.room;if(!r)return;if(r.records_deleted){renderReadyRoom(r);return;}if(r.is_judge){renderJudgeRoom(r);return;}if(r.can_manage&&state.roomView==='models'){renderModelRoom(r);return;}const n=state.selectedRound||r.current_round;const round=r.rounds.find(x=>x.number===n)||r.rounds[r.rounds.length-1];const active=n===r.current_round;const finished=r.status==='finished';const canManage=r.can_manage;const humans=r.humans.length,ais=r.ais.length;const submitted=new Set(round.submitted);const locked=!!r.verdict_locked||(r.judges||[]).some(j=>j.locked);
 const rtab=r.rounds.map(x=>`<button class="small ${x.number===n?'active':''}" data-action="round" data-id="${x.number}">第 ${x.number} 轮 ${x.status==='revealed'?'✓':''}</button>`).join('');
 const proposals=r.suggestions.filter(s=>s.round_number===n);
 const roster=[...r.humans.map(p=>({...p,role:'人类',label:p.name})),...r.ais.map(p=>({...p,role:'AI',label:p.name}))].sort((a,b)=>a.alias.localeCompare(b.alias));
 const answerView=round.status==='revealed'?roster.map(p=>`<div class="answer-card"><strong>${esc(p.alias)} · ${esc(p.label)}</strong><span class="pill" style="margin-left:7px">${p.role}</span><div class="tiny muted">${esc(shortDate(round.answers[p.alias]?.submitted_at))}</div>${round.answers[p.alias]?.edited_at?'<span class="pill">主持人已修订</span>':''}<div class="answer-content">${esc(round.answers[p.alias]?.text||'（无回答）')}</div></div>`).join(''):
   `<div class="notice">🔒 回答暂时隐藏。已提交 ${round.submitted.length} / ${roster.length}；全部参赛者（含 AI）提交后，服务器会自动向所有在线玩家公布。</div>${round.answers[r.mine]?.text?`<div class="answer-card"><strong>我已提交的回答</strong><div class="answer-content">${esc(round.answers[r.mine].text)}</div></div>`:''}`;
 const canAnswer=active&&!finished&&round.status==='collecting'&&r.mine&&!submitted.has(r.mine);
 const judgeBox= canManage?`<section class="card compact"><div class="label-line"><h2>房主管理台</h2><span class="pill">${r.is_owner?'房主':'管理员'}</span></div><p class="hint">${(r.judges||[]).length?'模型接入的 AI 参赛者可自动回答；AI 裁判可自动出题和判断。未接入模型的 AI 席位仍可手动转录。':'可以添加手动 AI 席位，也可以先在“模型 API 接入”页配置模型。'}</p>
 ${active&&round.status==='draft'&&!finished&&!round.questions?.length?`<div class="divider"></div><form data-form="question"><label class="field">录入裁判的第 ${n} 轮问题</label><textarea name="question" data-draft="question" maxlength="6000" required placeholder="从裁判聊天窗口复制原文…">${myDraft('question')}</textarea><div class="form-actions"><button class="primary" type="submit">向所有玩家发布问题</button></div></form>`:''}
 ${active&&round.status==='collecting'&&!finished&&r.ais.some(a=>a.model_profile_id&&!submitted.has(a.alias))?`<div class="divider"></div><button class="primary" data-action="generate-ai-answers">自动生成模型 AI 回复</button>`:''}
  ${active&&round.status==='collecting'&&!finished&&r.ais.some(a=>!a.model_profile_id&&!submitted.has(a.alias))?`<div class="divider"></div><form data-form="ai-answer"><label class="field">录入 AI 原始回答</label><select name="alias">${r.ais.filter(a=>!a.model_profile_id&&!submitted.has(a.alias)).map(a=>`<option value="${a.alias}">${a.alias} · ${esc(a.name)}</option>`).join('')}</select><label class="field">AI 回答（复制原文）</label><textarea name="answer" data-draft="ai-answer" maxlength="12000" required placeholder="请粘贴 AI 原样回答，不做修改…">${myDraft('ai-answer')}</textarea><div class="fom-actions"><button class="primary" type="submit">录入 AI 回答</button></div></form>`:''}
 ${r.invite_code?`<div class="notice"><div class="muted tiny">房间邀请码（房主 / 管理员可见）</div><div class="label-line"><strong class="code">${esc(r.invite_code)}</strong><button class="small" data-action="copy-code">复制</button></div></div>`:''}
 ${(r.rounds[0].status==='draft'||r.roster_recovery)&&active&&!finished?`<div class="divider"></div><label class="field">${r.roster_recovery?'补足本轮参赛者（至少 2 人）':'增加 AI 参赛位（最多总计 8 人）'}</label><form data-form="add-ai"><input name="name" maxlength="60" placeholder="AI 来源，如 GPT-6" required><label class="field">模型配置</label><select name="model_profile_id">${modelProfileOptions(r)}</select><div class="form-actions"><button type="submit">＋ 添加 AI</button></div></form><p class="hint">${r.roster_recovery?'账号退赛后人数不足，可补齐至 2 人再开启本轮；历史代号和回答保留。':'房主也可使用邀请码加入，以人类身份回答。'}</p>`:''}
 ${!r.mine&&(r.rounds[0].status==='draft'||r.roster_recovery)&&!finished?`<button class="small" data-action="admin-join">作为人类玩家加入</button>`:''}
 ${active&&!finished&&!locked&&['collecting','revealed'].includes(round.status)&&r.ais.some(a=>submitted.has(a.alias))?`<div class="divider"></div><form data-form="edit-ai-answer"><h3>修订 AI 参赛者回复</h3><p class="hint">仅能修改当前轮、下一轮开始之前的 AI 回复。已公开的修改将实时通知裁判，历史修订保留供审计；已锁定判决时不可修改。</p><label class="field">AI 参赛者</label><select name="alias" data-ai-edit-select>${r.ais.filter(a=>submitted.has(a.alias)).map(a=>`<option value="${esc(a.alias)}">${esc(a.alias)} · ${esc(a.name)}</option>`).join('')}</select><label class="field">修改后的回复</label><textarea name="text" data-ai-edit-text maxlength="12000" required>${esc(r.ai_edits?.[r.ais.find(a=>submitted.has(a.alias))?.alias]||'')}</textarea><div class="form-actions"><button type="submit">保存修订并同步</button></div></form>`:''}
 ${active&&!finished&&!locked&&r.humans.length&&roster.length>2?`<div class="divider"></div><h3>移除人类参赛者</h3><p class="hint">移除后该账号无法重新加入房间；历史已提交回答仍保留。</p>${r.humans.map(h=>`<div class="label-line" style="gap:8px;margin:8px 0"><span>${esc(h.alias)} · ${esc(h.name)}</span><button class="small danger" data-action="remove-human" data-id="${h.user_id}">移出房间</button></div>`).join('')}`:''}
 ${active&&round.status==='revealed'&&!finished&&!locked?`<div class="divider"></div><div class="buttonrow"><button class="primary" data-action="next-round">＋ 开启下一轮</button><button data-action="packet">生成匿名裁判包</button></div>`:''}
 ${r.packets.length?`<div class="divider"></div><details class="packet-panel" data-packet-panel data-key="${r.id}:${r.match_number}" ${state.packetPanels[`${r.id}:${r.match_number}`]?'open':''}><summary><strong>单轮裁判包快照</strong><span class="pill">${r.packets.length} 份</span></summary><div class="packet-panel-body"><p class="hint">每份快照仅包含该轮裁判问题和参赛者答复，生成后内容固定；修订回复后请重新生成快照。</p>${r.packets.slice(0,6).map(p=>`<div class="feed-item" style="margin-top:7px"><div class="label-line"><strong>仅第 ${p.through_round} 轮 · v${p.version}</strong><span class="pill ${p.sent_at?'ok':''}">${p.sent_at?'已确认发送':'待发送'}</span></div><div class="tiny muted">SHA256: ${esc(p.sha256.slice(0,16))}…</div><div class="buttonrow spaced"><button class="small" data-action="view-packet" data-id="${p.id}">查看 / 复制</button>${p.sent_at?'':`<button class="small" data-action="mark-sent" data-id="${p.id}">标记已发送</button>`}</div></div>`).join('')}</div></details>`:''}
 ${!finished&&!(r.judges||[]).length?`<div class="divider"></div><h3>裁判最终判断</h3>${locked?`<p class="locked">🔒 结论已锁定，揭晓后所有玩家都能查看。</p><button class="danger" data-action="reveal">揭晓身份与计分</button>`: (r.rounds.every(x=>x.status==='revealed')?`<form data-form="verdict"><p class="hint">手动录入裁判的最终判断。锁定前不能修改身份判决；请勿提前公布真实身份。</p>${roster.map(p=>`<div class="label-line" style="margin:8px 0"><label for="pred-${p.alias}">参与者 ${p.alias}</label><select required style="max-width:120px" name="pred-${p.alias}" id="pred-${p.alias}"><option value="">选择</option><option value="Human">人类</option><option value="AI">AI</option></select></div>`).join('')}<label class="field">裁判主要依据（必填）</label><textarea name="evidence" data-draft="evidence" required maxlength="5000" placeholder="裁判的判断依据…">${myDraft('evidence')}</textarea><label class="field">置信度（0–100）</label><input type="text" name="confidence" inputmode="numeric" pattern="[0-9]{1,3}" value="50" required><label class="field">最弱环节（可选）</label><textarea name="weakness" data-draft="weakness" maxlength="5000">${myDraft('weakness')}</textarea><label class="field">如果判断错误可能错在哪（可选）</label><textarea name="possible_error" data-draft="possible_error" maxlength="5000">${myDraft('possible_error')}</textarea><div class="form-actions"><button class="primary" type="submit">锁定裁判结论</button></div></form>`:`<p class="hint">所有轮次回答完成之后才可锁定裁判最终判断。</p>`)}`:''}</section>`:'';
 const scored=r.verdict&&finished?roster.filter(p=>r.verdict.predictions[p.alias]===(p.role==='AI'?'AI':'Human')).length:null;
 app.innerHTML=`<div class="pagehead"><div><div class="overline">ROOM ${r.id} / REAL-TIME MATCH</div><h1>${esc(r.title)}</h1><div class="stat-row"><span class="pill ${finished?'ok':'warn'}">${finished?'比赛已结束':'比赛进行中'}</span><span cass="pill">${humans} 位人类 · ${ais} 位 AI</span><span class="pill">${r.judges?.length?`${r.judges.length} 位裁判（真人 / AI）`:`外部裁判：${esc(r.judge)}`}</span></div></div><div class="buttonrow"><button class="ghost" data-action="back">← 比赛大厅</button><button data-action="sync">↻ 刷新</button></div></div>
 ${finishedActions(r)}<div class="room-layout ${canManage?'admin-room-layout':''}"><aside class="aside"><section class="card flat"><h2>参赛者身份</h2><p class="hint">人类玩家互相可见真实账号身份；裁判包仅含匿名代号。</p><div class="roster">${makeRoster(r)}</div></section><section class="card flat"><h2>比赛轮次</h2><div class="round-tags">${rtab}</div><p class="hint spaced">点击轮次可查看历史记录。</p></section></aside>
 <div class="stack">${canManage&&state.roomView==='judges'?judgeAdminPanel(r):canManage&&state.roomView==='admin'?judgeBox:''}
 <div class="${canManage&&state.roomView!=='match'?'hide':'stack'}"><section class="card"><div class="label-line"><div><div class="overline">JUDGE / ROUND ${n}</div><h2 style="margin:5px 0 0">裁判问题</h2></div><span class="pill ${round.status==='revealed'?'ok':'warn'}">${round.status==='draft'?'等待发布':round.status==='collecting'?'正在收集回答':'本轮已揭晓'}</span></div><div class="question spaced">${esc(round.question||'管理员尚未开启本轮作答。')}</div>${round.status==='draft'&&round.questions?.length?`<div class="feed spaced">${round.questions.map(q=>`<div class="feed-item"><strong>${esc(q.judge)}</strong><div class="feed-text">${esc(q.text)}</div></div>`).join('')}</div>`:''}<div class="buttonrow spaced"><button class="small" data-action="download-log">↓ 下载本轮 Log (.md)</button>${finished?'<button class="small" data-action="download-full-log">↓ 下载整场 Log (.md)</button>':''}</div></section>
 <section class="card"><div class="label-line"><h2>参赛者回答</h2><span class="pill">${round.submitted.length}/${roster.length} 已提交</span></div>${answerView}
 ${canAnswer?`<form data-form="answer" class="spaced"><label class="field">我的本轮回答 · ${esc(r.mine)}</label><textarea name="answer" data-draft="answer" maxlength="12000" required placeholder="在这里回答裁判的问题。提交后不可修改，直到全员提交才统一公开。">${myDraft('answer')}</textarea><div class="form-actions"><button class="primary" type="submit">提交本轮回答</button></div></form>`:''}
 ${!r.mine&&!finished?D('房主若希望亲自参赛，请在右侧点击“作为人类玩家加入”（仅开局前）。'):''}</section>
 <section class="card"><div class="label-line"><h2>问题与追问同步区</h2><span class="pill">${proposals.length} 条</span></div><p class="hint">玩家发送的提问建议实时广播给所有人类参赛者，不会自动发送给外部裁判。房主负责手动转录。</p><div class="feed">${proposals.length?proposals.map(p=>`<div class="feed-item"><div><span class="feed-name">${esc(p.alias||'管理员')} · ${esc(p.display_name)}</span><span class="feed-time">${esc(shortDate(p.created_at))}</span></div><div class="feed-text">${esc(p.text)}</div></div>`).join(''):D('本轮暂无玩家提问')}</div>
 ${active&&!finished?`<form data-form="suggestion" class="spaced"><label class="field">提交给主持人的问题或追问</label><textarea name="suggestion" data-draft="suggestion" maxlength="2000" required placeholder="提出追问建议，或对裁判的问题作出说明…">${myDraft('suggestion')}</textarea><div class="form-actions"><button type="submit">发送并同步所有玩家</button></div></form>`:''}</section>
 ${finished&&r.verdicts?.length?renderMultiResults(r,roster):''}
 ${finished&&!r.verdicts?.length?`<section class="card"><div class="overline">FINAL / RESULT</div><h2>最终判决与真实身份</h2><div class="score">${scored}/${roster.length}</div><p class="muted">裁判 ${esc(r.judge)} · 置信度 ${r.verdict?.confidence??'?'}%</p>${roster.map(p=>`<div class="answer-card"><strong>${p.alias} · ${esc(p.label)}</strong><div>真实身份：${p.role} / 裁判判断：${r.verdict?.predictions?.[p.alias]==='Human'?'人类':'AI'} ${r.verdict?.predictions?.[p.alias]===(p.role==='AI'?'AI':'Human')?'✓':'✗'}</div></div>`).join('')}<h3 class="spaced">判断依据</h3><div class="question">${esc(r.verdict?.evidence)}</div><p class="hint spaced">最弱环节：${esc(r.verdict?.weakness||'未填写')}</p><p class="hint">可能误判：${esc(r.verdict?.possible_error||'未填写')}</p></section>`:''}
 </div></div><div class="right-panel">${canManage?renderAdminRoomNav(r,finished):`<section class="card flat"><div class="overline">PARTICIPANT</div><h2>等待主持人推进比赛</h2><p class="hint">你的每次提交都会发送到服务器；全员提交完成后自动揭晓。若网络断开，请等待自动重连或手动刷新。</p></section>`}</div></div>`;
 updateTypingBadges();
 if(editingDraft){
   const next=app.querySelector(`[data-draft="${editingDraft}"]`);
   if(next){next.focus();if(editingPos!==null)next.setSelectionRange(editingPos,editingPos)}
 }
}
async function downloadLog(){const id=state.roomId,n=state.selectedRound;const file=await api(`/api/rooms/${id}/rounds/${n}/log`,{raw:true});const href=URL.createObjectURL(file);const a=document.createElement('a');a.href=href;a.download=`room-${id}-round-${n}.md`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(href),1500)}
async function downloadFullLog(){const id=state.roomId;const file=await api(`/api/rooms/${id}/log`,{raw:true});const href=URL.createObjectURL(file);const a=document.createElement('a');a.href=href;a.download=`room-${id}-full-log.md`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(href),1500)}
app.addEventListener('toggle',e=>{
 if(e.target.matches('details[data-packet-panel]'))state.packetPanels[e.target.dataset.key]=e.target.open;
},true);
app.addEventListener('input',e=>{
 if(e.target.dataset?.draft&&state.roomId){
  state.drafts[`${state.roomId}:${state.room?.match_number}:${state.selectedRound}:${e.target.dataset.draft}`]=e.target.value;
  if(e.target.dataset.draft==='answer')sendTyping(e.target.value.trim().length>0);
 }
});
app.addEventListener('change',e=>{
 if(e.target.matches('[data-ai-edit-select]')){
  const area=app.querySelector('[data-ai-edit-text]');if(area)area.value=state.room?.ai_edits?.[e.target.value]||'';
 }
});
$('#judgeNoticeClose').addEventListener('click',()=>$('#judgeNotice').close());
app.addEventListener('click',async e=>{const b=e.target.closest('[data-action]');if(!b)return;const action=b.dataset.action;
 try{
  if(action==='tab-login'||action==='tab-register'){state.tab=action==='tab-login'?'login':'register';renderAuth()}
  if(action==='toggle-legacy-login'){state.legacyLogin=!state.legacyLogin;renderAuth()}
  if(action==='copy-uid'){await navigator.clipboard.writeText(b.dataset.uid);toast('UID 已复制')}
  if(action==='back'){history.replaceState(null,'',location.pathname);await loadLobby()}
  if(action==='manage-users')await loadAccounts();
  if(action==='toggle-password-reset'){const form=b.closest('[data-account-uid]').querySelector('form');form.classList.toggle('hide');if(!form.classList.contains('hide'))form.querySelector('input').focus();}
  if(action==='delete-account'){
   const uid=b.dataset.uid;
   if(confirm(`确定删除账号 ${uid}？该账号将立即退出登录且无法再使用，未完赛参与者将退赛；已有比赛历史署名保留。`)){
    const result=await api(`/api/admin/users/${uid}`,{method:'DELETE',body:{confirm:true}});
    if(result.signed_out){state.me=null;state.room=null;state.roomId=null;renderAuth();}else await loadAccounts();toast('账号已删除');
   }
  }
  if(action==='delete-room'){
   const id=Number(b.dataset.id),match=Number(b.dataset.match);
   if(confirm(`永久删除房间 ${id}？所有比赛记录、回答、快照和房间配置都会删除，无法恢复；房间内所有人将退出，进行中的比赛也会终止。用户账号不受影响。`)){
    await api(`/api/rooms/${id}`,{method:'DELETE',body:{match_number:match,confirm:true}});await loadLobby();toast('房间已删除');
   }
  }
  if(action==='delete-records'){
   const id=Number(b.dataset.id),match=Number(b.dataset.match);
   if(confirm(`永久删除房间 ${id} 第 ${match} 场所有人的比赛记录？问题、回答、判决、日志和裁判快照都会清空，无法恢复。房间本身保留。建议先下载 Log。`)){
    await api(`/api/rooms/${id}/records`,{method:'DELETE',body:{match_number:match,confirm:true}});
    if(state.roomId===id)await refreshRoom();else await loadLobby();
   }
  }
  if(action==='restart-match'){
   const r=state.room;
   if(confirm('再次开赛将清空上一场所有人的日志、回答、判决与快照，无法恢复。房间、邀请码和在场名单保留。确定开始新比赛？')){
    await api(`/api/rooms/${r.id}/restart`,{method:'POST',body:{match_number:r.match_number,confirm:true}});await refreshRoom();toast('已重新开赛，上一场记录已清空');
   }
  }
  if(action==='open')await openRoom(Number(b.dataset.id));
  if(action==='round'){state.selectedRound=Number(b.dataset.id);if(state.room?.can_manage)state.roomView='match';renderRoom()}
 if(action==='room-view'){
    if(!state.room?.can_manage || state.room?.is_judge) return;
    const view=b.dataset.view;
    if(!['match','judges','admin','models'].includes(view)) return;
    if(state.roomView!==view){state.roomView=view;if(view!=='match')state.selectedRound=state.room.current_round;renderRoom();window.scrollTo({top:0,behavior:'instant'});}
   }
   if(action==='sync')await refreshRoom();
  if(action==='generate-ai-questions'){b.disabled=true;b.textContent='模型正在出题…';const result=await api(`/api/rooms/${state.roomId}/ai-judges/questions/generate`,{method:'POST'});await refreshRoom();const detail=result.failed?.length?'；失败：'+result.failed.map(x=>x.error).join('；'):'';toast(`AI 裁判已生成 ${result.generated||0} 个问题${detail}`)}
  if(action==='generate-ai-answers'){b.disabled=true;b.textContent='模型正在回答…';const result=await api(`/api/rooms/${state.roomId}/ai/answers/generate`,{method:'POST'});await refreshRoom();const detail=result.failed?.length?'；失败：'+result.failed.map(x=>x.alias+' '+x.error).join('；'):'';toast(`模型 AI 已提交 ${result.generated||0} 个回答${detail}`)}
  if(action==='generate-ai-verdicts'){b.disabled=true;b.textContent='模型正在判断…';const result=await api(`/api/rooms/${state.roomId}/ai-judges/verdicts/generate`,{method:'POST'});await refreshRoom();const detail=result.failed?.length?'；失败：'+result.failed.map(x=>x.error).join('；'):'';toast(`AI 裁判已锁定 ${result.generated||0} 份最终判断${detail}`)}
  if(action==='test-model'){b.disabled=true;b.textContent='测试中…';const result=await api(`/api/rooms/${state.roomId}/models/${Number(b.dataset.id)}/test`,{method:'POST'});toast(`连接成功 · ${result.model} · ${result.latency_ms} ms · ${result.preview}`);b.disabled=false;b.textContent='测试连接'}
  if(action==='delete-model'){const id=Number(b.dataset.id);if(confirm('删除此模型配置？关联的 AI 参赛者和 AI 裁判将改为手动转录，比赛记录不会删除。')){await api(`/api/rooms/${state.roomId}/models/${id}`,{method:'DELETE'});await refreshRoom();toast('模型配置已删除')}}
  if(action==='download-log')await downloadLog();
  if(action==='download-full-log')await downloadFullLog();
  if(action==='copy-code'){await navigator.clipboard.writeText(state.room.invite_code);toast('邀请码已复制')}
  if(action==='remove-human'){
   const person=state.room.humans.find(h=>h.user_id===Number(b.dataset.id));
   if(person&&confirm(`确定将 ${person.alias} · ${person.name} 移出房间吗？历史回答会保留，但对方不能重新加入。`)){
    await api(`/api/rooms/${state.roomId}/members/${person.user_id}/remove`,{method:'POST'});await refreshRoom();toast('已移出参赛者');
   }
  }
  if(action==='admin-join'){await api('/api/join',{method:'POST',body:{code:state.room.invite_code}});await refreshRoom();toast('已作为玩家加入')}
  if(action==='next-round'){if(confirm('确定开启下一轮？')){await api(`/api/rooms/${state.roomId}/next`,{method:'POST'});await refreshRoom();const q=await api(`/api/rooms/${state.roomId}/ai-judges/questions/generate`,{method:'POST'});await refreshRoom();const detail=q.failed?.length?'；AI 裁判出题失败：'+q.failed.map(x=>x.error).join('；'):'';toast('下一轮已开启'+(q.generated?`，AI 裁判已生成 ${q.generated} 个问题`:'')+detail)}}
  if(action==='start-round'){if(confirm('确认裁判问题都已提交，向所有参赛者开放作答？模型 AI 裁判会先自动出题，开局后模型 AI 参赛者会自动回答。')){b.disabled=true;b.textContent='正在准备模型…';const q=await api(`/api/rooms/${state.roomId}/ai-judges/questions/generate`,{method:'POST'});await refreshRoom();if(q.failed?.length)throw new Error('AI 裁判出题失败：'+q.failed.map(x=>x.error).join('；'));await api(`/api/rooms/${state.roomId}/start`,{method:'POST'});await refreshRoom();const a=await api(`/api/rooms/${state.roomId}/ai/answers/generate`,{method:'POST'});await refreshRoom();const detail=a.failed?.length?'；失败：'+a.failed.map(x=>x.alias+' '+x.error).join('；'):'';toast(`本轮已开始，模型自动提交 ${a.generated||0} 个回答${detail}`)}}
  if(action==='packet'){await api(`/api/rooms/${state.roomId}/packets`,{method:'POST'});await refreshRoom();toast('已生成匿名裁判包快照')}
  if(action==='view-packet'){const pack=await api(`/api/rooms/${state.roomId}/packets/${b.dataset.id}`);const win=window.open('','_blank');if(win){win.document.title='裁判匿名消息包';const pre=win.document.createElement('pre');pre.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.7 sans-serif;padding:24px';pre.textContent=pack.content;win.document.body.appendChild(pre);}else{await navigator.clipboard.writeText(pack.content);toast('弹窗被阻止，已复制裁判包');}}
  if(action==='mark-sent'){if(confirm('确认已亲自发送此版本到裁判窗口？标记并不代表系统已自动发送。')){await api(`/api/rooms/${state.roomId}/packets/${b.dataset.id}/sent`,{method:'POST'});await refreshRoom();toast('已记录手动发送时间')}}
  if(action==='reveal'){if(confirm('裁判结论锁定后将向全部玩家公开身份和计分。确定结束比赛并揭晓？')){await api(`/api/rooms/${state.roomId}/reveal`,{method:'POST'});await refreshRoom();toast('已结束并揭晓比赛')}}
 }catch(err){toast(err.message)}
});
app.addEventListener('submit',async e=>{const form=e.target.closest('form[data-form]');if(!form)return;e.preventDefault();const type=form.dataset.form;const data=Object.fromEntries(new FormData(form));const btn=form.querySelector('[type=submit]');if(btn)btn.disabled=true;
 try{
  if(type==='login'){await api('/api/login',{method:'POST',body:data});await init();toast('登录成功');return}
  if(type==='register'){const result=await api('/api/register',{method:'POST',body:data});history.replaceState(null,'',location.pathname);await loadLobby();toast(`注册成功，你的 UID 是 ${result.uid}`);return}
  if(type==='join'){const result=await api('/api/join',{method:'POST',body:{code:data.code}});await openRoom(result.room_id);toast('已加入比赛');return}
  if(type==='room'){const result=await api('/api/rooms',{method:'POST',body:data});await openRoom(result.room_id);toast('房间创建成功，可以分享邀请码');return}
  if(type==='reset-password'){
   const uid=form.dataset.uid;
   if(confirm(`确定修改 ${uid} 的密码并使其所有登录会话失效？`)){
    const result=await api(`/api/admin/users/${uid}/password`,{method:'POST',body:{password:data.password,confirm:true}});
    if(result.signed_out){state.me=null;state.room=null;state.roomId=null;renderAuth();}else await loadAccounts();toast('密码已修改，原登录会话已失效');
   }
   return;
  }
  const id=state.roomId;
  if(type==='model-profile'||type==='edit-model-profile'){
   const payload={name:data.name,base_url:data.base_url,model:data.model,api_key:data.api_key||'',temperature:Number(data.temperature),max_tokens:Number(data.max_tokens),timeout_seconds:Number(data.timeout_seconds)};
   if(type==='model-profile')await api(`/api/rooms/${id}/models`,{method:'POST',body:payload});
   else await api(`/api/rooms/${id}/models/${Number(form.dataset.id)}`,{method:'PUT',body:payload});
   await refreshRoom();state.roomView='models';renderRoom();toast(type==='model-profile'?'模型配置已保存':'模型配置已更新');return;
  }
  if(type==='add-ai'){const profileId=data.model_profile_id?Number(data.model_profile_id):null;await api(`/api/rooms/${id}/ai`,{method:'POST',body:{name:data.name,model_profile_id:profileId}});await refreshRoom();toast('AI 参赛位已创建')}
  if(type==='add-ai-judge'){const profileId=data.model_profile_id?Number(data.model_profile_id):null;await api(`/api/rooms/${id}/ai-judges`,{method:'POST',body:{name:data.name,model_profile_id:profileId}});await refreshRoom();toast(profileId?'已添加模型 AI 裁判':'已添加手动 AI 裁判')}
  if(type==='ai-judge-question'){await api(`/api/rooms/${id}/ai-judges/${Number(data.judge_id)}/questions`,{method:'POST',body:{text:data.text}});delete state.drafts[`${id}:${state.room?.match_number}:${state.selectedRound}:ai-judge-question`];await refreshRoom();toast('AI 裁判问题已实时同步')}
  if(type==='ai-judge-verdict'){const predictions={};for(const p of [...state.room.humans,...state.room.ais])predictions[p.alias]=data[`pred-${p.alias}`];if(!Object.values(predictions).every(Boolean))throw new Error('请填写所有匿名参赛者的判断');if(confirm('确认已从对应 AI 裁判获取判决？锁定后不可修改。')){await api(`/api/rooms/${id}/ai-judges/${Number(data.judge_id)}/verdict`,{method:'POST',body:{predictions,evidence:data.evidence,confidence:Number(data.confidence),weakness:data.weakness||'',possible_error:data.possible_error||''}});await refreshRoom();toast('AI 裁判独立判断已锁定')}}
  if(type==='add-judge'){const result=await api(`/api/rooms/${id}/judges`,{method:'POST',body:data});await refreshRoom();toast(`裁判已创建，UID：${result.uid}。请安全告知裁判 UID 和初始密码`)}
  if(type==='assign-judge'){await api(`/api/rooms/${id}/judges/assign`,{method:'POST',body:data});await refreshRoom();toast('已将现有账号设为本房间裁判')}
  if(type==='judge-question'){await api(`/api/rooms/${id}/judge-questions`,{method:'POST',body:{text:data.text}});delete state.drafts[`${id}:${state.room?.match_number}:${state.selectedRound}:judge-question`];await refreshRoom();toast('裁判问题已发送，等待主持人统一开始作答')}
  if(type==='judge-verdict'){
   const predictions={};for(const alias of state.room.aliases)predictions[alias]=data[`pred-${alias}`];
   if(!Object.values(predictions).every(Boolean))throw new Error('请给全部参赛者做出判断');
   if(confirm('锁定后不能修改，其他裁判不会看到你的判断内容。确定提交？')){await api(`/api/rooms/${id}/judge-verdict`,{method:'POST',body:{predictions,evidence:data.evidence,confidence:Number(data.confidence),weakness:data.weakness||'',possible_error:data.possible_error||''}});await refreshRoom();toast('已独立锁定裁判判断')}
  }
  if(type==='question'){await api(`/api/rooms/${id}/question`,{method:'POST',body:{text:data.question}});delete state.drafts[`${id}:${state.room?.match_number}:${state.selectedRound}:question`];await refreshRoom();toast('裁判问题已同步给所有玩家')}
  if(type==='answer'){await api(`/api/rooms/${id}/answers`,{method:'POST',body:{text:data.answer}});delete state.drafts[`${id}:${state.room?.match_number}:${state.selectedRound}:answer`];await refreshRoom();toast('回答已提交，等待其他玩家')}
  if(type==='suggestion'){await api(`/api/rooms/${id}/suggestions`,{method:'POST',body:{text:data.suggestion}});delete state.drafts[`${id}:${state.room?.match_number}:${state.selectedRound}:suggestion`];await refreshRoom();toast('消息已实时同步')}
  if(type==='edit-ai-answer'){
   if(confirm('确定保存 AI 回复修订吗？如果答案已公开，裁判会收到更新。')){
    await api(`/api/rooms/${id}/ai/${data.alias}/answer`,{method:'PUT',body:{text:data.text}});
    await refreshRoom();toast('AI 回复已修订，旧版本保留在服务器审计记录中');
   }
  }
  if(type==='ai-answer'){await api(`/api/rooms/${id}/ai/${data.alias}/answer`,{method:'POST',body:{text:data.answer}});delete state.drafts[`${id}:${state.room?.match_number}:${state.selectedRound}:ai-answer`];await refreshRoom();toast('AI 回答已录入')}
  if(type==='verdict'){
   const predictions={};for(const p of [...state.room.humans,...state.room.ais])predictions[p.alias]=data[`pred-${p.alias}`];
   if(!Object.values(predictions).every(Boolean))throw new Error('请填写全部参赛者的身份判断');
   if(!/^\d{1,3}$/.test(data.confidence)||Number(data.confidence)>100)throw new Error('置信度必须为 0–100');
   if(confirm('确定锁定裁判结论？一旦锁定就不能修改！')){await api(`/api/rooms/${id}/verdict`,{method:'POST',body:{predictions,evidence:data.evidence,confidence:Number(data.confidence),weakness:data.weakness,possible_error:data.possible_error}});await refreshRoom();toast('裁判结论已锁定')}}
 }catch(err){toast(err.message)}finally{if(btn)btn.disabled=false}
});
// 页面在后台休眠、网络断线或切回标签页时，重新核对轮次完成状态。
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.roomId&&state.me)refreshRoom()});
window.addEventListener('focus',()=>{if(state.roomId&&state.me)refreshRoom()});
window.addEventListener('hashchange',async()=>{const m=/^#room=(\d+)$/.exec(location.hash);if(!state.me)return;if(m&&Number(m[1])!==state.roomId)await openRoom(Number(m[1]));else if(!m&&state.roomId)await loadLobby()});
setInterval(()=>{if(state.roomId && state.me && (!state.ws||state.ws.readyState!==WebSocket.OPEN))refreshRoom()},15000);
init();

