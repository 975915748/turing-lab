'use strict';
const $ = (selector) => document.querySelector(selector);
const app = $('#app');
const state = {me:null,room:null,roomId:null,selectedRound:null,ws:null,reconnect:null,tab:'login',legacyLogin:false,roomView:'match',drafts:{},typing:{},lastTyping:null,accounts:[],packetPanels:{},chat:[],chatDraft:'',personalModels:[],modelApiEnabled:true,personalReturn:null,personalModelExpanded:{},personalModelDrafts:{},modelTestResults:{},personalModelTestVersions:{},personalModelTestPending:{},personalModelSavePending:{},social:null,dmMessages:[],dmBeforeId:null,dmHasMore:false,dmSocket:null,personalModelEpoch:0,modelOperationRecords:[],modelOperationStats:{},modelOperationProfileOptions:[],modelOperationTotal:0,modelOperationOffset:0,modelOperationFilters:{profile_id:'',purpose:'',status:''},modelOperationsExpanded:false,modelOperationRequestVersion:0,modelOperationHistoryPanels:{},serverTimeOffset:0,dmUid:null,dmOtherName:'',dmCanSend:false,dmPending:null,dmDraft:'',dmReconnect:null,dmLoadVersion:0,socialEpoch:0,pointsLedgerEntries:[],pointsLedgerBeforeId:null,pointsLedgerHasMore:false,pointsLedgerLoaded:false};
const esc = (v) => String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const shortDate = s => s ? new Date(s).toLocaleString('zh-CN',{hour12:false}) : '';
const val = (id) => document.getElementById(id)?.value??'';
const D = (txt='暂无记录') => `<p class="muted hint">${esc(txt)}</p>`;
let toastTimer;
function clearPersonalModelState(){stopDmWs();state.social=null;state.dmUid=null;state.dmMessages=[];state.dmBeforeId=null;state.dmHasMore=false;state.dmOtherName='';state.dmCanSend=false;state.dmPending=null;state.dmDraft='';state.dmLoadVersion++;state.socialEpoch++;state.pointsLedgerEntries=[];state.pointsLedgerBeforeId=null;state.pointsLedgerHasMore=false;state.pointsLedgerLoaded=false;state.personalModelEpoch=(state.personalModelEpoch||0)+1;state.modelOperationRequestVersion=(state.modelOperationRequestVersion||0)+1;state.personalModelExpanded={};state.personalModelDrafts={};state.modelTestResults={};state.personalModelTestVersions={};state.personalModelTestPending={};state.personalModelSavePending={};state.modelOperationRecords=[];state.modelOperationStats={};state.modelOperationProfileOptions=[];state.modelOperationTotal=0;state.modelOperationOffset=0;state.modelOperationFilters={profile_id:'',purpose:'',status:''};state.modelOperationsExpanded=false;state.modelOperationHistoryPanels={};}
function toast(message){const el=$('#toast');el.textContent=message;el.classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('visible'),4200)}
function setConnection(txt,online){const el=$('#connection');el.textContent=(online?'● ':'○ ')+txt;el.className='connection'+(online?' online':'')}
function applyPrefs(){let theme=localStorage.getItem('turing_theme')||'light';document.body.dataset.theme=theme;$('#themeBtn').textContent=theme==='light'?'☾ 深色':'☀ 日间';let f=Number(localStorage.getItem('turing_font')||15);if(f<12||f>22)f=15;document.documentElement.style.setProperty('--font',f+'px');$('#fontSize').value=f;$('#fontValue').textContent=f;}
$('#themeBtn').addEventListener('click',()=>{localStorage.setItem('turing_theme',document.body.dataset.theme==='light'?'dark':'light');applyPrefs()});
$('#fontSize').addEventListener('input',e=>{localStorage.setItem('turing_font',e.target.value);applyPrefs()});
$('#logoutBtn').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST'});}catch(e){toast(e.message)} state.me=null;state.room=null;state.roomId=null;state.personalModels=[];clearPersonalModelState();state.chat=[];state.chatDraft='';state.drafts={};state.personalReturn=null;state.tab='login';state.legacyLogin=false;stopWs();history.replaceState(null,'',location.pathname);await init()});
async function api(path,{method='GET',body,raw=false,headers:extraHeaders={}}={}){
 const requestUid=state.me?.user?.uid??null,requestCsrf=state.me?.user?.csrf??null,requestEpoch=state.personalModelEpoch||0;
 const opts={method,headers:{...extraHeaders}};
 if(body!==undefined){opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body)}
 if(method!=='GET'&&state.me?.user?.csrf)opts.headers['X-CSRF-Token']=state.me.user.csrf;
 if(method!=='GET'&&state.room&&path.startsWith(`/api/rooms/${state.room.id}/`))opts.headers['X-Match-Number']=String(state.room.match_number);
 const response=await fetch(path,opts);
 if(raw){if(!response.ok)throw new Error('下载失败');return response.blob()}
 const data=await response.json().catch(()=>({detail:'网络连接错误'}));
 if(!response.ok){
  if(response.status===401&&state.me&&state.me.user?.uid===requestUid&&state.me.user?.csrf===requestCsrf&&(state.personalModelEpoch||0)===requestEpoch){
   state.me=null;state.room=null;state.roomId=null;state.personalModels=[];clearPersonalModelState();state.chat=[];state.chatDraft='';state.drafts={};stopWs();renderAuth();
  }
  throw new Error(typeof data.detail==='string'?data.detail:'请求失败');
 }
 return data;
}
function stopWs(){state.lastTyping=null;if(state.reconnect)clearTimeout(state.reconnect);state.reconnect=null; if(state.ws){const ws=state.ws;state.ws=null;ws.onclose=null;ws.close();}setConnection('未连接',false)}
function connectWs(roomId){stopWs();if(state.roomId!==roomId)return;
 const proto=location.protocol==='https:'?'wss:':'ws:';
 const ws=new WebSocket(`${proto}//${location.host}/ws/rooms/${roomId}`);state.ws=ws;
 ws.onopen=()=>{setConnection('在线',true);sendTypingFromDraft();if(!state.room?.is_judge)loadRoomChat().catch(()=>{})};
 ws.onmessage=async e=>{if(state.exitingRoom)return;let m;try{m=JSON.parse(e.data)}catch{return}
 if(m.type==='room_updated' && state.roomId===roomId){
  if(m.reason==='room_closed'){await loadLobby();toast('房主已关闭房间，记录已保留。');return;}
  await refreshRoom();
 }
 if(m.type==='chat_message'&&state.roomId===roomId&&!state.room?.is_judge&&m.match_number===state.room?.match_number&&state.room?.status==='open'){addChatMessage(m.message)}
 if(m.type==='chat_cleared'&&state.roomId===roomId&&m.match_number===state.room?.match_number){state.chat=[];state.chatDraft='';renderRoom()}
 if(m.type==='typing_status'&&state.roomId===roomId){state.typing=m.round===state.room?.current_round&&m.match_number===state.room?.match_number?(m.states||{}):{};updateTypingBadges()}};
 ws.onclose=e=>{if(state.exitingRoom||state.roomId!==roomId||state.ws!==ws)return;if(e.code===1008){state.roomId=null;state.room=null;state.typing={};history.replaceState(null,'',location.pathname);loadLobby().then(()=>toast(e.reason==='房间已删除'?'房间已删除':'房间访问权限已失效或你已被移出')).catch(err=>toast(err.message));return;}setConnection('重连中',false);state.reconnect=setTimeout(()=>connectWs(roomId),3000)};
 ws.onerror=()=>setConnection('连接异常',false);
}
async function refreshRoom(){if(!state.roomId||location.hash.startsWith('#profile'))return;try{const r=await api(`/api/rooms/${state.roomId}`);if(state.exitingRoom||state.roomId!==r.id)return;state.serverTimeOffset=Date.parse(r.server_now||new Date().toISOString())-Date.now();if(!state.room||r.current_round!==state.room.current_round||r.match_number!==state.room.match_number){state.selectedRound=r.current_round;state.lastTyping=null;state.typing={};}
 if(state.room&&(r.match_number!==state.room.match_number||(!state.room.records_deleted&&r.records_deleted))){for(const key of Object.keys(state.drafts))if(key.startsWith(`${r.id}:`))delete state.drafts[key];const dialog=$('#judgeNotice');if(dialog?.open)dialog.close();state.roomView='match';state.chat=[];state.chatDraft='';}
 state.room=r;
 if(r.is_judge||r.status==='finished'){state.chat=[];state.chatDraft='';}else await loadRoomChat().catch(()=>{});
 if(r.can_manage)await loadPersonalModels().catch(()=>{});
 renderRoom();maybeNotifyJudge();}catch(e){if(/不属于这个房间|公共聊天/.test(e.message)){state.roomId=null;state.room=null;state.chat=[];await loadLobby();toast('你已不在该房间');}else toast(e.message)}}
async function init(){applyPrefs();try{const previousUid=state.me?.user?.uid;state.me=await api('/api/me');if(previousUid&&previousUid!==state.me?.user?.uid)clearPersonalModelState();$('#logoutBtn').classList.remove('hide');const q=/^#room=(\d+)$/.exec(location.hash);if(location.hash.startsWith('#profile')){state.personalReturn=profileReturnFromHash();await openPersonalCenter({fromHash:true});}else if(q){await openRoom(Number(q[1]));}else renderLobby()}catch(e){state.me=null;state.roomId=null;state.personalModels=[];state.chat=[];$('#logoutBtn').classList.add('hide');renderAuth();}}
function renderAuth(){
 clearDmState();state.social=null;stopWs();$('#logoutBtn').classList.add('hide');setConnection('未登录',false);
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
 app.innerHTML=`<div class="pagehead"><div><div class="overline">比赛大厅 / MATCH LOBBY</div><h1>欢迎，${esc(state.me.user.display_name)}</h1><p class="muted">${isAdmin?'管理员账号 · 可管理所有房间及用户账号':'创建自己的房间，或加入其他人的比赛'}</p></div><div class="buttonrow"><button data-action="personal-center">个人中心</button><span class="pill">${isAdmin?'管理员':'人类玩家'}</span>${isAdmin?'<button data-action="manage-users">用户管理</button>':''}</div></div>
 <section class="notice" style="margin-bottom:20px"><div class="label-line"><div><div class="tiny muted">你的登录 UID</div><strong class="code" id="accountUid">${esc(state.me.user.uid)}</strong></div><button class="small" data-action="copy-uid" data-uid="${esc(state.me.user.uid)}">复制 UID</button></div><p class="hint" style="margin:8px 0 0">请保存此 UID，下次使用它和密码登录。</p></section>
 <div class="grid2"><section class="card"><h2>加入已有房间</h2><form data-form="join"><label class="field">邀请码</label><input name="code" maxlength="20" required type="text" placeholder="例如 A1B2C3D4E5"><label class="field">加入身份</label><select name="role"><option value="player">参赛者</option>${isAdmin?"":'<option value="judge">真人裁判</option>'}</select><div class="form-actions"><button class="primary" type="submit">加入比赛 →</button></div></form></section>
 <section class="card"><h2>创建一场图灵测试</h2><form data-form="room"><label class="field">比赛名称</label><input type="text" name="title" maxlength="80" required placeholder="例如 人机辨识实验 #1"><label class="field">旧版手工转录裁判名（多裁判模式不使用）</label><input type="text" name="judge" maxlength="40" value="裁判" required><div class="form-actions"><button class="primary" type="submit">＋ 创建房间</button></div></form></section></div>
 <section class="spaced"><div class="label-line"><h2>${isAdmin?'全部房间':'我的房间'}</h2><span class="pill">${state.me.rooms.length} 个</span></div><div class="roomcards">${state.me.rooms.length?state.me.rooms.map(r=>`<div class="card flat room-card"><div class="overline">ROOM ${r.id}</div><h3>${esc(r.title)}</h3><div class="muted hint">${r.status==='closed'?'房间已关闭':r.status==='finished'?'比赛已结束':'第 '+r.current_round+' 轮进行中'}${r.role==='judge'?' · 裁判席':r.role==='owner'?' · 我是房主':''}</div><div class="buttonrow"><button data-action="open" data-id="${r.id}">进入房间 →</button>${r.can_manage?`<button class="small danger" data-action="delete-room" data-id="${r.id}" data-match="${r.match_number}">删除房间</button>`:''}</div></div>`).join(''):D('尚未加入任何房间，可以创建房间或输入邀请码。')}</div></section>
 ${completed.length?`<section class="spaced"><h2>已完赛房间记录</h2><p class="hint">任何已登录用户都可以清空这些房间的比赛记录。清空对所有人立即生效，房间本身保留。</p><div class="roomcards">${completed.map(r=>`<div class="card flat room-card"><div class="overline">ROOM ${r.id} · 第 ${r.match_number} 场</div><h3>${esc(r.title)}</h3><button class="danger small" data-action="delete-records" data-id="${r.id}" data-match="${r.match_number}">删除所有人的比赛记录</button></div>`).join('')}</div></section>`:''}`;
}
async function loadAccounts(){
 const data=await api('/api/admin/users');state.accounts=data.users;stopWs();state.roomId=null;state.room=null;state.chat=[];state.chatDraft='';state.personalReturn=null;history.replaceState(null,'',location.pathname);setConnection('用户管理',false);renderAccounts();
}
function renderAccounts(){
 app.innerHTML=`<div class="pagehead"><div><div class="overline">ADMIN / ACCOUNTS</div><h1>用户管理</h1><p class="muted">按唯一 UID 管理账号；修改密码后，该账号的所有登录会话立即失效。</p></div><div class="buttonrow"><button data-action="personal-center">个人中心</button><button data-action="back">← 返回大厅</button></div></div>
 <div class="roomcards">${state.accounts.map(u=>`<section class="card flat" data-account-uid="${esc(u.uid)}"><div class="label-line"><h2>${esc(u.display_name)}</h2><span class="pill">${u.is_admin?'管理员':'用户'}</span></div><div class="code">${esc(u.uid)}</div><p class="hint">用户名：${esc(u.username)}</p><div class="buttonrow"><button class="small" data-action="toggle-password-reset" data-uid="${esc(u.uid)}">修改密码</button><button class="small danger" data-action="delete-account" data-uid="${esc(u.uid)}">删除账号</button></div>
 <form data-form="reset-password" data-uid="${esc(u.uid)}" class="hide spaced"><label class="field" for="password-${u.id}">新密码（至少 8 位）</label><input id="password-${u.id}" type="password" name="password" required minlength="8" maxlength="200" autocomplete="new-password"><div class="form-actions"><button type="submit">确认修改密码</button></div></form></section>`).join('')}</div>`;
}
function finishedActions(r){
 if(r.status!=='finished')return '';
 return `<div class="buttonrow spaced" style="margin-bottom:20px">${r.can_manage?'<button class="primary" data-action="restart-match">再次开赛</button>':''}${r.records_deleted?'':`<button class="danger" data-action="delete-records" data-id="${r.id}" data-match="${r.match_number}">删除所有人的比赛记录</button>`}</div>`;
}
function renderMyPointsResult(r){
 const x=r.my_points_result;if(!x)return '';
 const delta=String(x.delta),change=delta==='0'?'0':delta.startsWith('-')?delta:'+'+delta;
 return `<section class="card compact account-points-result"><div class="overline">ACCOUNT POINTS / MATCH RESULT</div><h2>本场账号积分</h2><div class="label-line"><strong>${x.outcome==='win'?'胜利':'失败'}</strong><span class="pill ${x.outcome==='win'?'ok':'warn'}">${esc(change)} 分</span></div><div class="stat-row"><div class="stat">更新后总积分 <strong>${esc(x.points_after)}</strong></div><div class="stat">当前连胜 <strong>${esc(x.streak_after)}</strong></div></div></section>`;
}
async function leaveCurrentRoom(){
 if(state.exitingRoom)return false;
 const back=state.personalReturn;
 const r=state.room||(back?.roomId?await api('/api/rooms/'+back.roomId):null);
 if(r?.is_owner&&r.status==='open'&&!confirm('返回大厅将关闭房间，所有人退出当前比赛；记录保留，不结算积分。仅想退出参赛请取消，并到参赛者管理选择退赛。确定关闭房间？'))return false;
 state.exitingRoom=true;
 try{
  if(r)await api('/api/rooms/'+r.id+'/leave',{method:'POST',body:{match_number:back?.matchNumber??r.match_number,confirm:true}});
  if(r)for(const key of Object.keys(state.drafts))if(key.startsWith(r.id+':'))delete state.drafts[key];
  const notice=$('#judgeNotice');if(notice?.open)notice.close();
  await loadLobby();
  return true;
 }finally{state.exitingRoom=false;}
}
function renderClosedRoom(r){
 const rounds=r.rounds.map(round=>'<section class="card"><h2>第 '+round.number+' 轮</h2><div class="question">'+esc(round.question||(round.questions||[]).map(q=>q.text).join('\n\n')||'尚未开启作答')+'</div><div class="feed spaced">'+Object.entries(round.answers||{}).map(([alias,a])=>'<article class="feed-item"><strong>'+esc(alias)+'</strong><div class="feed-text">'+esc(a.text)+'</div></article>').join('')+'</div><button class="small" data-action="closed-round-log" data-round="'+round.number+'">下载本轮 Log</button></section>').join('');
 const packets=r.can_manage&&r.packets?.length?'<section class="card"><details><summary>单轮裁判包快照（'+r.packets.length+'）</summary><div class="feed spaced">'+r.packets.map(p=>'<div class="label-line"><strong>第 '+p.through_round+' 轮 · v'+p.version+'</strong><button class="small" data-action="view-packet" data-id="'+p.id+'">查看 / 复制</button></div>').join('')+'</div></details></section>':'';
 app.innerHTML='<div class="pagehead"><div><h1>'+esc(r.title)+'</h1><h2>房间已关闭</h2><p class="muted">比赛已停止，记录保留，未结算积分。</p></div><div class="buttonrow"><button data-action="personal-center">个人中心</button><button data-action="back">← 返回大厅</button></div></div><div class="buttonrow spaced"><button data-action="download-full-log">下载整场 Log</button>'+(r.can_manage?'<button class="primary" data-action="restart-match">再次开赛</button>':'')+'</div><div class="stack spaced">'+rounds+packets+renderPublicChat(r)+'</div>';
}
function renderReadyRoom(r){
 app.innerHTML=`<div class="pagehead"><div><div class="overline">ROOM ${r.id} · 第 ${r.match_number} 场</div><h1>${esc(r.title)}</h1></div><div class="buttonrow"><button data-action="personal-center">个人中心</button><button data-action="back">← 返回大厅</button></div></div>${renderMyPointsResult(r)}<section class="card"><h2>${r.can_manage?'开始下一场比赛':'等待再次开赛'}</h2>${finishedActions(r)}</section>`;
}
async function openRoom(id,options={}){clearDmState();state.roomId=id;state.selectedRound=options.round||null;state.roomView=options.view||'match';state.chat=[];state.chatDraft='';try{const r=await api(`/api/rooms/${id}`);state.serverTimeOffset=Date.parse(r.server_now||new Date().toISOString())-Date.now();state.room=r;state.typing={};state.selectedRound=options.round&&r.rounds.some(x=>x.number===options.round)?options.round:r.current_round;history.replaceState(null,'',`#room=${id}`);await loadPersonalModels().catch(()=>{});if(!r.is_judge&&r.status!=='finished')await loadRoomChat().catch(()=>{});renderRoom();connectWs(id);maybeNotifyJudge();}catch(e){state.roomId=null;state.room=null;state.chat=[];history.replaceState(null,'',location.pathname);toast(e.message);await loadLobby()}}
async function loadLobby(){stopWs();state.roomId=null;state.room=null;history.replaceState(null,'',location.pathname);clearDmState();state.me=await api('/api/me');state.chat=[];state.chatDraft='';state.personalReturn=null;renderLobby()}

async function loadPersonalModels(){
 const data=await api('/api/me/models'),next=data.models||[];
 for(const previous of state.personalModels||[]){
  const current=next.find(p=>Number(p.id)===Number(previous.id));
  if(!current||personalModelTestFingerprint(previous)!==personalModelTestFingerprint(current))invalidatePersonalModelTest(previous.id);
 }
 for(const [id,result] of Object.entries(state.modelTestResults)){
  const current=next.find(p=>Number(p.id)===Number(id));
  if(!current||result.profileSnapshot!==personalModelTestFingerprint(current))invalidatePersonalModelTest(Number(id));
 }
 state.personalModels=next;state.modelApiEnabled=data.model_api_enabled!==false;return state.personalModels;
}

async function loadModelOperationRecords(){
 const uid=state.me?.user?.uid,epoch=state.personalModelEpoch;
 if(!uid)return;
 const version=++state.modelOperationRequestVersion;
 const params=new URLSearchParams({limit:'20',offset:String(state.modelOperationOffset)});
 for(const [key,value] of Object.entries(state.modelOperationFilters||{}))if(value)params.set(key,value);
 const data=await api('/api/me/model-operations?'+params.toString());
 if(state.personalModelEpoch!==epoch||state.me?.user?.uid!==uid||
    state.modelOperationRequestVersion!==version)return;
 state.modelOperationRecords=data.records||[];
 state.modelOperationStats=data.stats||{};
 state.modelOperationProfileOptions=data.profile_options||[];
 state.modelOperationTotal=data.total||0;
}
async function loadRoomChat(){
 const r=state.room;if(!r||r.is_judge||r.status==='finished'){state.chat=[];return;}
 const data=await api(`/api/rooms/${r.id}/chat?match_number=${r.match_number}`);
 if(state.roomId===r.id&&state.room?.match_number===data.match_number&&state.room?.status!=='finished'&&!state.room?.is_judge)state.chat=data.messages||[];
}
function chatMessageHtml(m){return `<article class="chat-message"><div class="chat-meta"><strong>${esc(m.sender_name)}</strong><span class="chat-uid">${esc(m.sender_uid)}</span><time>${esc(shortDate(m.created_at))}</time></div><div class="chat-body">${esc(m.text)}</div></article>`;}
function addChatMessage(message){
 if(!message||state.chat.some(x=>x.id===message.id))return;
 state.chat.push(message);state.chat.sort((a,b)=>a.id-b.id);
 const list=$('#chatMessages');if(list){list.innerHTML=state.chat.map(chatMessageHtml).join('')||D('暂无聊天消息');list.scrollTop=list.scrollHeight;}
}
function renderPublicChat(r){
 const messages=state.chat||[];
 return `<section class="card chat-card"><div class="label-line"><div><div class="overline">ROOM CHAT / MATCH ${r.match_number}</div><h2>公共聊天</h2></div><span class="pill">${messages.length} 条消息</span></div>
 
 <div id="chatMessages" class="chat-list" aria-live="polite" aria-relevant="additions text">${messages.map(chatMessageHtml).join('')||D('暂无聊天消息')}</div>
 ${r.status==='open'?`<form data-form="chat" class="chat-compose"><label class="field" for="roomChatInput">发送消息</label><textarea id="roomChatInput" name="text" data-chat-draft maxlength="2000" required placeholder="输入纯文本消息（最多 2000 字）">${esc(state.chatDraft)}</textarea><div class="form-actions"><span class="hint">纯文本 · 2000 字以内</span><button class="primary" type="submit">发送</button></div></form>`:'<p class="hint spaced">'+(r.status==='closed'?'房间已关闭，聊天记录保留。':'本场比赛已完赛，公共聊天已关闭。')+'</p>'}
 </section>`;
}
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
function makeRoster(r){
 const submitted=new Set(r.rounds.find(x=>x.number===state.selectedRound)?.submitted||[]);
 const people=[...r.humans.map(h=>({...h,role:'人类',label:h.name})),...r.ais.map(a=>({...a,role:'AI',label:a.name}))].sort((a,b)=>a.alias.localeCompare(b.alias));
 return people.map(p=>`<div class="roster-person"><div class="person-left"><span class="alias">${esc(p.alias)}</span><div style="min-width:0"><span class="person-name" title="${esc(p.label)}">${esc(p.label)}</span><span class="roster-role">${p.role==='人类'?'人类 · '+esc(p.uid):'AI 参赛位'}${p.alias===r.mine?' · 我':''}</span></div></div><span class="pill ${submitted.has(p.alias)?'ok':''}" ${p.role==='人类'&&p.alias===r.mine?`data-typing-alias="${esc(p.alias)}"`:''} title="本轮提交状态">${submitted.has(p.alias)?'√':p.role==='人类'&&p.alias===r.mine?(state.selectedRound===r.current_round&&state.typing[p.alias]?'....':'X'):'—'}</span>${p.role==='人类'&&p.alias!==r.mine?`<button class="small" data-action="friend-from-room" data-uid="${esc(p.uid)}">加好友</button>`:''}</div>`).join('')||D('等待人类玩家加入');
}
function profileReturnFromHash(){const m=/^#profile\?room=(\d+)&view=([^&]+)&round=(\d+)(?:&match=(\d+))?$/.exec(location.hash);return m?{roomId:Number(m[1]),view:decodeURIComponent(m[2]),round:Number(m[3]),matchNumber:m[4]?Number(m[4]):null}:null;}
function stopDmWs(){
 if(state.dmReconnect)clearTimeout(state.dmReconnect);state.dmReconnect=null;
 if(state.dmSocket){const ws=state.dmSocket;state.dmSocket=null;ws.onclose=null;try{ws.close();}catch{}}
}
function clearDmState(){
 stopDmWs();state.dmUid=null;state.dmMessages=[];state.dmBeforeId=null;state.dmHasMore=false;
 state.dmOtherName='';state.dmCanSend=false;state.dmPending=null;state.dmDraft='';state.dmLoadVersion++;
}
async function loadSocial(){
 const uid=state.me?.user?.uid,version=++state.socialEpoch;
 const data=await api('/api/me/social');
 if(state.socialEpoch!==version||state.me?.user?.uid!==uid)return state.social;
 state.social=data;
 if(state.dmUid){
  const conv=state.social.conversations?.find(x=>x.uid===state.dmUid);
  if(conv){state.dmCanSend=!!conv.can_send;if(conv.display_name)state.dmOtherName=conv.display_name;if(!conv.can_send){state.dmPending=null;state.dmDraft='';}}
  else {state.dmCanSend=false;state.dmPending=null;state.dmDraft='';}
 }
 return state.social;
}
function mergeDmMessages(...groups){
 const map=new Map();for(const group of groups)for(const message of group||[])map.set(Number(message.id),message);
 return [...map.values()].sort((a,b)=>Number(a.id)-Number(b.id));
}
function mergeLedgerEntries(...groups){
 const map=new Map();for(const group of groups)for(const entry of group||[])map.set(Number(entry.id),entry);
 return [...map.values()].sort((a,b)=>Number(b.id)-Number(a.id));
}
function captureDmDraft(){
 const form=$('#dmPanel form[data-form="dm-send"]');
 if(form&&form.dataset.uid===state.dmUid){
  const textarea=form.querySelector('textarea[name="text"]');
  if(textarea)state.dmDraft=textarea.value;
 }
}
function dmMessageHtml(m){
 return `<div class="feed-item dm-message ${m.mine?'mine':''}"><strong>${m.mine?'我':esc(m.sender_name)}</strong><div class="feed-text">${esc(m.text)}</div><div class="tiny muted">${esc(shortDate(m.created_at))}</div></div>`;
}
function renderDmPanel(){
 const box=$('#dmPanel');if(!box)return;
 captureDmDraft();
 const uid=state.dmUid;if(!uid){box.innerHTML='';return;}
 const friend=state.social?.friends?.find(x=>x.uid===uid);
 const conv=state.social?.conversations?.find(x=>x.uid===uid);
 const title=friend?.note||conv?.note||state.dmOtherName||uid;
 const canSend=conv?!!conv.can_send:state.dmCanSend;
 box.innerHTML=`<div class="divider"></div><div class="label-line"><h3>与 ${esc(title)}（${esc(uid)}）私信</h3><button class="small" data-action="dm-close">收起</button></div><div class="feed" id="dmFeed" aria-live="polite">${state.dmMessages.map(dmMessageHtml).join('')||D('暂无消息')}</div>${state.dmHasMore?`<button class="small" data-action="dm-older" data-uid="${esc(uid)}">加载更早消息</button>`:''}${canSend?`<form data-form="dm-send" data-uid="${esc(uid)}"><textarea name="text" maxlength="2000" required placeholder="输入私信">${esc(state.dmDraft||'')}</textarea><div class="form-actions"><span class="hint">纯文本 · 最多 2000 字</span><button class="primary" type="submit">发送</button></div></form>`:D('好友关系已解除，只能查看历史消息。')}`;
 const feed=$('#dmFeed');if(feed)feed.scrollTop=feed.scrollHeight;
}
async function markDmRead(uid,messageId,{refresh=true,accountUid=state.me?.user?.uid}={}){
 if(!messageId)return;
 const accountCsrf=state.me?.user?.csrf,accountEpoch=state.personalModelEpoch;
 await api('/api/messages/'+encodeURIComponent(uid)+'/read',{method:'POST',body:{last_read_message_id:Number(messageId)}});
 if(state.me?.user?.uid!==accountUid||state.me?.user?.csrf!==accountCsrf||state.personalModelEpoch!==accountEpoch)return;
 if(refresh){
  await loadSocial();
  if(location.hash.startsWith('#profile')&&state.me?.user?.uid===accountUid&&state.me?.user?.csrf===accountCsrf&&state.personalModelEpoch===accountEpoch)refreshSocialPanel();
 }
}
async function openDm(uid){
 const version=++state.dmLoadVersion,accountUid=state.me?.user?.uid,accountCsrf=state.me?.user?.csrf,accountEpoch=state.personalModelEpoch;
 if(state.dmUid!==uid)state.dmDraft='';
 state.dmUid=uid;state.dmMessages=[];state.dmBeforeId=null;state.dmHasMore=false;
 renderDmPanel();
 const data=await api('/api/messages/'+encodeURIComponent(uid));
 if(version!==state.dmLoadVersion||state.dmUid!==uid||state.me?.user?.uid!==accountUid||state.me?.user?.csrf!==accountCsrf||state.personalModelEpoch!==accountEpoch)return null;
 state.dmOtherName=data.other_name;state.dmCanSend=!!data.can_send;
 state.dmMessages=mergeDmMessages(data.messages,state.dmMessages);
 state.dmBeforeId=data.next_before_id;state.dmHasMore=!!data.has_more;
 renderDmPanel();
 if(data.last_received_message_id)await markDmRead(uid,data.last_received_message_id,{refresh:false,accountUid}).catch(()=>{});
 await loadSocial();
 if(location.hash.startsWith('#profile')&&state.me?.user?.uid===accountUid&&state.me?.user?.csrf===accountCsrf&&state.personalModelEpoch===accountEpoch&&state.dmLoadVersion===version&&state.dmUid===uid)refreshSocialPanel();
 return state.dmLoadVersion===version&&state.dmUid===uid?version:null;
}
async function loadOlderDm(uid){
 if(uid!==state.dmUid||!state.dmBeforeId)return;
 const accountUid=state.me?.user?.uid,accountCsrf=state.me?.user?.csrf,accountEpoch=state.personalModelEpoch,version=state.dmLoadVersion;
 const first=state.dmMessages[0]?.id;if(!first)return;
 const feed=$('#dmFeed'),oldHeight=feed?.scrollHeight||0,oldTop=feed?.scrollTop||0;
 const page=await api('/api/messages/'+encodeURIComponent(uid)+'?before_id='+encodeURIComponent(state.dmBeforeId));
 if(uid!==state.dmUid||state.me?.user?.uid!==accountUid||state.me?.user?.csrf!==accountCsrf||state.personalModelEpoch!==accountEpoch||state.dmLoadVersion!==version)return;
 state.dmMessages=mergeDmMessages(page.messages,state.dmMessages);
 state.dmBeforeId=page.next_before_id;state.dmHasMore=!!page.has_more;
 renderDmPanel();
 const next=$('#dmFeed');if(next)next.scrollTop=next.scrollHeight-oldHeight+oldTop;
}
async function loadPointsLedger({reset=false}={}){
 const accountUid=state.me?.user?.uid,before=reset?null:state.pointsLedgerBeforeId;
 const params=new URLSearchParams({limit:'20'});if(before)params.set('before_id',String(before));
 const data=await api('/api/me/points/ledger?'+params.toString());
 if(state.me?.user?.uid!==accountUid)return;
 state.pointsLedgerEntries=reset?data.entries:mergeLedgerEntries(state.pointsLedgerEntries,data.entries);
 state.pointsLedgerBeforeId=data.next_before_id;state.pointsLedgerHasMore=!!data.has_more;state.pointsLedgerLoaded=true;
 renderPointsLedger();
}
function renderPointsLedger(){
 const el=$('#pointsLedger');if(!el)return;
 if(!state.pointsLedgerLoaded){el.innerHTML='';return;}
 const rows=state.pointsLedgerEntries.map(x=>{
  const delta=String(x.delta),sign=delta==='0'||delta.startsWith('-')?'':'+'; 
  return `<div class="feed-item">${x.outcome==='win'?'胜利':'失败'} · 房间 ${esc(x.room_id)} 第 ${esc(x.match_number)} 场 · ${sign}${esc(delta)} · 积分 ${esc(x.points_after)} · 连胜 ${esc(x.streak_after)} · ${esc(shortDate(x.settled_at))}</div>`;
 }).join('')||D('暂无积分流水');
 el.innerHTML=rows+(state.pointsLedgerHasMore?'<button class="small spaced" data-action="points-ledger-more">加载更多</button>':'');
}
function refreshSocialPanel(){
 const panel=$('#socialPanel');if(!panel)return;
 captureDmDraft();
 const holder=document.createElement('div');
 holder.innerHTML=renderSocialPanel();
 const next=holder.firstElementChild;
 if(next)panel.replaceWith(next);
 renderDmPanel();renderPointsLedger();
}
async function refreshSocialUi(){
 const accountUid=state.me?.user?.uid,accountCsrf=state.me?.user?.csrf,accountEpoch=state.personalModelEpoch;
 await loadSocial();
 if(state.me?.user?.uid!==accountUid||state.me?.user?.csrf!==accountCsrf||state.personalModelEpoch!==accountEpoch)return;
 if(location.hash.startsWith('#profile'))refreshSocialPanel();
}
function connectDmWs(){
 stopDmWs();const uid=state.me?.user?.uid;if(!uid||!location.hash.startsWith('#profile'))return;
 const proto=location.protocol==='https:'?'wss:':'ws:';
 const ws=new WebSocket(`${proto}//${location.host}/ws/messages`);state.dmSocket=ws;
 ws.onopen=async()=>{
  if(state.dmSocket!==ws||state.me?.user?.uid!==uid)return;
  try{
   await loadSocial();
   if(state.dmSocket!==ws||state.me?.user?.uid!==uid)return;
   if(state.dmUid)await openDm(state.dmUid);else refreshSocialPanel();
  }catch{}
 };
 ws.onmessage=async event=>{
  if(state.dmSocket!==ws||state.me?.user?.uid!==uid)return;
  let message;try{message=JSON.parse(event.data)}catch{return;}
  if(message.type==='social_updated'){await refreshSocialUi();return;}
  if(message.type==='points_updated'){
   await refreshSocialUi();
   if(state.pointsLedgerLoaded)await loadPointsLedger({reset:true});
   return;
  }
  if(message.type==='name_lock_updated'){
   try{state.me=await api('/api/me');if(location.hash.startsWith('#profile'))renderPersonalCenter();}catch{}
   return;
  }
  if(message.type==='dm_message'){
   const otherUid=message.other_uid,entry=message.message;
   if(!otherUid||!entry)return;
   if(state.dmUid===otherUid){
    state.dmMessages=mergeDmMessages(state.dmMessages,[entry]);renderDmPanel();
    if(!entry.mine)await markDmRead(otherUid,entry.id,{refresh:false,accountUid:uid}).catch(()=>{});
   }
   await refreshSocialUi();
  }
 };
 ws.onclose=async event=>{
  if(state.dmSocket!==ws)return;state.dmSocket=null;
  if(event.code===1008){
   try{
    const current=await api('/api/me');
    if(current.user?.uid!==uid){state.me=current;clearPersonalModelState();await init();}
    else{
     state.me=current;
     state.dmReconnect=setTimeout(()=>{if(location.hash.startsWith('#profile')&&state.me?.user?.uid===uid)connectDmWs();},1000);
    }
   }catch{
    state.me=null;clearPersonalModelState();renderAuth();
   }
   return;
  }
  if(!location.hash.startsWith('#profile')||state.me?.user?.uid!==uid)return;
  state.dmReconnect=setTimeout(()=>{if(location.hash.startsWith('#profile')&&state.me?.user?.uid===uid)connectDmWs();},3000);
 };
 ws.onerror=()=>{};
}
async function openPersonalCenter(options={}){
 if(!options.fromHash){state.personalReturn=state.roomId&&state.room?{roomId:state.roomId,view:state.roomView,round:state.selectedRound,matchNumber:state.room.match_number}:null;}
 else if(!state.personalReturn)state.personalReturn=profileReturnFromHash();
 stopWs();const back=state.personalReturn;const hash=back?.roomId?`#profile?room=${back.roomId}&view=${encodeURIComponent(back.view)}&round=${back.round||1}${back.matchNumber?'&match='+back.matchNumber:''}`:'#profile';history.replaceState(null,'',hash);
 state.me=await api('/api/me');
 await loadPersonalModels();await loadModelOperationRecords();await loadSocial();renderPersonalCenter();connectDmWs();
}
async function returnFromPersonalCenter(){
 const back=state.personalReturn;state.personalReturn=null;clearDmState();
 if(back?.roomId){await openRoom(back.roomId,{view:back.view,round:back.round});return;}
 history.replaceState(null,'',location.pathname);state.roomId=null;state.room=null;await loadLobby();
}
function credentialStatusLabel(p){if(!state.modelApiEnabled)return '模型 API 暂不可用，请联系管理员。';return ({ok:'API Key 已保存（可解密）',missing:'缺少 API Key',needs_repair:'需要重新录入 API Key',key_unavailable:'服务器密钥不可用',owner_unavailable:'配置所属账号不可用'})[p.credential_status]||'状态未知';}
function personalModelDraftKey(form){return form.dataset.form==='personal-model-create'?'create':'model:'+Number(form.dataset.id);}
function capturePersonalModelDrafts(skipKey=null){
 app.querySelectorAll('form[data-form^="personal-model-"]').forEach(form=>{
  const key=personalModelDraftKey(form);
  if(key===skipKey){delete state.personalModelDrafts[key];return;}
  state.personalModelDrafts[key]=Object.fromEntries(new FormData(form));
 });
}
function personalModelFields(p={},draft={}){
 const field=(name,fallback)=>Object.prototype.hasOwnProperty.call(draft,name)?draft[name]:fallback;
 const status=p.credential_status||'missing',editing=!!p.id,repair=editing&&status!=='ok';
 const keyLabel=!editing?'API Key':status==='needs_repair'?'API Key（无法解密，必须重新录入）':status==='key_unavailable'?'API Key（服务器密钥不可用）':status==='missing'?'API Key（缺少密钥）':p.has_api_key?'API Key（已保存，留空保持不变）':'API Key';
 const keyRequired=!editing||repair||!p.has_api_key;
 return '<label class="field">配置名称</label><input name="name" type="text" maxlength="60" required value="'+esc(field('name',p.name||''))+'" placeholder="例如 DeepSeek 主力">'+
 '<label class="field">Base URL</label><input name="base_url" type="text" maxlength="500" required value="'+esc(field('base_url',p.base_url||'https://api.openai.com/v1'))+'" placeholder="https://api.openai.com/v1">'+
 '<label class="field">模型名称</label><input name="model" type="text" maxlength="120" required value="'+esc(field('model',p.model||''))+'" placeholder="例如 gpt-4.1-mini">'+
 '<label class="field">'+keyLabel+'</label><input name="api_key" type="password" maxlength="2000" autocomplete="new-password" '+(keyRequired?'required':'')+' value="'+esc(field('api_key',''))+'" placeholder="'+(editing&&p.has_api_key&&status==='ok'?'不修改时留空':'填写 API Key')+'">'+
 '<div class="grid2"><div><label class="field">采样温度</label><input name="temperature" type="number" min="0" max="2" step="0.1" value="'+esc(field('temperature',p.temperature??0.7))+'" required></div><div><label class="field">最大输出 Token</label><input name="max_tokens" type="number" min="64" max="8192" step="1" value="'+esc(field('max_tokens',p.max_tokens??2048))+'" required></div></div>'+
 '<label class="field">请求超时（秒）</label><input name="timeout_seconds" type="number" min="10" max="120" step="1" value="'+esc(field('timeout_seconds',p.timeout_seconds??60))+'" required>';
}
function personalModelTestFingerprint(profile){
 if(!profile)return '';
 return JSON.stringify([profile.id,profile.name,profile.base_url,profile.model,profile.temperature,
  profile.max_tokens,profile.timeout_seconds,profile.has_api_key,profile.credential_status]);
}
function nextPersonalModelTestVersion(id){
 const key=String(id),version=(state.personalModelTestVersions[key]||0)+1;
 state.personalModelTestVersions[key]=version;return version;
}
function invalidatePersonalModelTest(id){
 const key=String(id);nextPersonalModelTestVersion(key);
 delete state.personalModelTestPending[key];delete state.modelTestResults[key];
 const result=app.querySelector('[data-model-test-result="'+Number(id)+'"]');
 if(result){result.textContent='';result.className='model-test-result';}
 const button=app.querySelector('[data-action="test-personal-model"][data-id="'+Number(id)+'"]');
 if(button){
  const profile=state.personalModels.find(p=>Number(p.id)===Number(id)),saving=!!state.personalModelSavePending[key];
  button.disabled=profile?.credential_status!=='ok'||saving;button.textContent=saving?'保存中…':'测试连接';
 }
}
function setPersonalModelSavePending(id,pending){
 const key=String(id);
 if(pending)state.personalModelSavePending[key]=true;else delete state.personalModelSavePending[key];
 const button=app.querySelector('[data-action="test-personal-model"][data-id="'+Number(id)+'"]');
 if(button){
  const profile=state.personalModels.find(p=>Number(p.id)===Number(id)),saving=!!state.personalModelSavePending[key],testing=state.personalModelTestPending[key]!==undefined;
  button.disabled=profile?.credential_status!=='ok'||saving||testing;
  button.textContent=saving?'保存中…':testing?'测试中…':'测试连接';
 }
}
function beginPersonalModelTest(id){
 const key=String(id);
 if(state.personalModelSavePending[key])return null;
 const profile=state.personalModels.find(p=>Number(p.id)===Number(id));
 if(!profile)return null;
 const version=nextPersonalModelTestVersion(key);
 const request={version,epoch:state.personalModelEpoch,uid:state.me?.user?.uid,
  profileSnapshot:personalModelTestFingerprint(profile)};
 state.personalModelTestPending[key]=version;return request;
}
function isCurrentPersonalModelTest(id,request){
 if(!request)return false;
 const key=String(id),profile=state.personalModels.find(p=>Number(p.id)===Number(id));
 return state.personalModelEpoch===request.epoch&&state.personalModelTestVersions[key]===request.version&&
  state.personalModelTestPending[key]===request.version&&state.me?.user?.uid===request.uid&&
  !!profile&&personalModelTestFingerprint(profile)===request.profileSnapshot;
}
function modelTestResultText(result){
 if(!result)return '';
 return result.ok?'已保存配置连接测试成功 · '+esc(result.model)+' · '+esc(result.latency_ms)+' ms':'已保存配置连接测试失败：'+esc(result.error);
}
function updateModelTestResult(id,result,request){
 if(!isCurrentPersonalModelTest(id,request))return false;
 const stored={...result,profileSnapshot:request.profileSnapshot};
 state.modelTestResults[id]=stored;
 const node=app.querySelector('[data-model-test-result="'+id+'"]');
 if(node){node.textContent=stored.ok?'已保存配置连接测试成功 · '+stored.model+' · '+stored.latency_ms+' ms':'已保存配置连接测试失败：'+stored.error;
  node.className='model-test-result '+(stored.ok?'success':'failed');}
 return true;
}
function renderPersonalModelManager(){
 if(!state.modelApiEnabled)return '<section class="notice"><strong>模型 API 暂不可用，请联系管理员。</strong></section>';
 let html='<p class="hint">配置归当前账号所有，可在自己管理的多个房间中复用。</p><div class="label-line"><h3>新增模型配置</h3><span class="pill">'+state.personalModels.length+' 个配置</span></div>';
 html+='<form data-form="personal-model-create" class="model-form">'+personalModelFields({},state.personalModelDrafts.create||{})+'<div class="form-actions"><button class="primary" type="submit">保存配置</button></div></form>';
 html+='<div class="divider"></div><h3>已有配置</h3>';
 if(!state.personalModels.length)return html+D('尚无个人模型配置。');
 html+=state.personalModels.map(p=>{
  const storedTest=state.modelTestResults[p.id];
  const test=storedTest?.profileSnapshot===personalModelTestFingerprint(p)?storedTest:null;
  const testing=state.personalModelTestPending[String(p.id)]!==undefined;
  const saving=!!state.personalModelSavePending[String(p.id)];
  const expanded=!!state.personalModelExpanded[p.id];
  let card='<details class="personal-model-card" data-model-config data-id="'+p.id+'" '+(expanded?'open':'')+'><summary class="model-config-summary"><strong>'+esc(p.name)+'</strong><span class="model-config-chevron" aria-hidden="true"></span></summary><div class="model-config-details">'+
   '<div class="label-line"><span class="pill '+(p.credential_status==='ok'?'ok':'warn')+'">'+credentialStatusLabel(p)+'</span></div>'+
   '<div class="tiny muted">模型标识：'+esc(p.model)+'<br>Base URL：'+esc(p.base_url)+'</div>';
  if(p.credential_status==='needs_repair')card+='<p class="hint">此配置无法解密，请重新录入服务商 API Key 并保存。</p>';
  if(p.credential_status==='missing')card+='<p class="hint">此配置缺少 API Key，请重新录入后保存。</p>';
  card+='<form data-form="personal-model-edit" data-id="'+p.id+'" class="spaced">'+personalModelFields(p,state.personalModelDrafts['model:'+p.id]||{})+'<div class="form-actions"><button type="submit">保存修改</button></div></form>'+
   '<div class="buttonrow spaced"><button class="small" data-action="test-personal-model" data-id="'+p.id+'" '+(p.credential_status==='ok'&&!testing&&!saving?'':'disabled')+'>'+(saving?'保存中…':testing?'测试中…':'测试连接')+'</button><button class="small danger" data-action="delete-personal-model" data-id="'+p.id+'">删除配置</button></div>'+
   '<p class="model-test-result '+(test?(test.ok?'success':'failed'):'')+'" data-model-test-result="'+p.id+'" aria-live="polite">'+modelTestResultText(test)+'</p></div></details>';
  return card;
 }).join('');
 return html;
}

function operationPurposeLabel(purpose){
 return ({connection_test:'连接测试',judge_question:'AI 裁判出题',ai_answer:'AI 参赛者回答',judge_verdict:'AI 裁判最终判断'})[purpose]||purpose;
}
function operationStateLabel(state){
 return ({queued:'排队中',requesting:'请求中',completed:'已完成',failed:'失败',blocked:'已阻止',stale:'已作废',interrupted:'已中断'})[state]||state;
}
function operationTokens(value){return value===null||value===undefined?'未返回':Number(value).toLocaleString('zh-CN');}
function renderModelOperationRecords(){
 const stats=state.modelOperationStats||{},records=state.modelOperationRecords||[];
 const known=Number(stats.known_usage_records||0);
 const uniqueProfiles=[];
 for(const p of state.modelOperationProfileOptions||[]){
  if(!uniqueProfiles.some(x=>Number(x.profile_id)===Number(p.profile_id)))uniqueProfiles.push(p);
 }
 const profileOptions=uniqueProfiles.map(p=>'<option value="'+esc(p.profile_id)+'" '+(String(state.modelOperationFilters.profile_id)===String(p.profile_id)?'selected':'')+'>'+esc(p.profile_name)+' · '+esc(p.model_name)+'</option>').join('');
 const purposeOptions=[['connection_test','连接测试'],['judge_question','AI 裁判出题'],['ai_answer','AI 参赛者回答'],['judge_verdict','AI 裁判最终判断']]
  .map(([value,label])=>'<option value="'+value+'" '+(state.modelOperationFilters.purpose===value?'selected':'')+'>'+label+'</option>').join('');
 const stateOptions=[['queued','排队中'],['requesting','请求中'],['completed','已完成'],['failed','失败'],['blocked','已阻止'],['stale','已作废'],['interrupted','已中断']]
  .map(([value,label])=>'<option value="'+value+'" '+(state.modelOperationFilters.status===value?'selected':'')+'>'+label+'</option>').join('');
 const recordHtml=records.map(r=>{
  const requestStatus=r.request_succeeded===true?'请求成功':r.request_succeeded===false?'请求失败':r.request_started_at?'请求结果待确认':'未进入请求阶段';
  const resultStatus=r.result_applied?'结果已写入比赛':r.request_succeeded===true?'请求成功但结果未应用':'结果未应用';
  const location=r.room_id?'ROOM '+r.room_id+' · 第 '+r.match_number+' 场'+(r.round_number?' · 第 '+r.round_number+' 轮':''):'独立测试';
  const usage=r.usage_known?'输入 '+operationTokens(r.prompt_tokens)+' · 输出 '+operationTokens(r.completion_tokens)+' · 总计 '+operationTokens(r.total_tokens):'服务商未返回 Token 用量';
  return '<article class="operation-record"><div class="label-line"><strong>'+esc(r.profile_name)+' · '+esc(r.model_name)+'</strong><span class="operation-status '+esc(r.state)+'">'+operationStateLabel(r.state)+'</span></div>'+
   '<div class="tiny muted">'+esc(shortDate(r.queued_at))+' · '+operationPurposeLabel(r.purpose)+' · '+esc(location)+'</div>'+
   '<div class="operation-meta"><span>'+requestStatus+'</span><span>'+resultStatus+'</span></div>'+
   '<div class="tiny muted">等待 '+(r.queue_ms===null?'—':esc(r.queue_ms)+' ms')+' · 请求 '+(r.duration_ms===null?'—':esc(r.duration_ms)+' ms')+'</div>'+
   '<div class="tiny muted">'+usage+'</div>'+
   (r.trigger_uid?'<div class="tiny muted">配置所有者：当前账号 · 触发者：'+esc(r.trigger_name||r.trigger_uid)+' ('+esc(r.trigger_uid)+')</div>':'')+
   (r.retry_of?'<div class="tiny muted">重试自任务 #'+esc(r.retry_of)+'</div>':'')+
   (r.error_message?'<p class="operation-error">'+esc(r.error_message)+'</p>':'')+'</article>';
 }).join('');
 const pages=Math.max(1,Math.ceil(state.modelOperationTotal/20)),page=Math.floor(state.modelOperationOffset/20)+1;
 return '<section class="card"><details data-model-operations-panel '+(state.modelOperationsExpanded?'open':'')+'><summary class="label-line"><strong>模型调用记录</strong><span class="pill">'+state.modelOperationTotal+' 条</span></summary><div class="model-operations-body">'+
  '<div class="stat-row operation-stats"><div class="stat">任务尝试 <strong>'+Number(stats.task_attempts||0)+'</strong></div><div class="stat">已发起请求 <strong>'+Number(stats.request_attempts||0)+'</strong></div><div class="stat">请求成功 <strong>'+Number(stats.request_successes||0)+'</strong></div><div class="stat">失败 <strong>'+Number(stats.failures||0)+'</strong></div><div class="stat">结果已应用 <strong>'+Number(stats.applied_results||0)+'</strong></div></div>'+
  '<p class="hint">当前保留记录的用量。已知 Token 合计：'+(known?operationTokens(stats.total_tokens)+'（'+known+' 条记录有用量）':'服务商未返回可汇总的 Token 用量')+'；清理比赛记录后，相应用量会同步清除。</p>'+
  '<form data-form="model-operation-filters" class="operation-filters"><select name="profile_id"><option value="">全部模型配置</option>'+profileOptions+'</select><select name="purpose"><option value="">全部用途</option>'+purposeOptions+'</select><select name="status"><option value="">全部状态</option>'+stateOptions+'</select><button class="small" type="submit">筛选</button></form>'+
  '<div class="operation-list">'+(recordHtml||D('没有符合筛选条件的调用记录。'))+'</div>'+
  '<div class="label-line operation-pagination"><span class="tiny muted">第 '+page+' / '+pages+' 页</span><div class="buttonrow"><button class="small" data-action="model-operations-page" data-direction="prev" '+(state.modelOperationOffset<=0?'disabled':'')+'>上一页</button><button class="small" data-action="model-operations-page" data-direction="next" '+(state.modelOperationOffset+20>=state.modelOperationTotal?'disabled':'')+'>下一页</button></div></div>'+
  '</div></details></section>';
}
function renderSocialPanel(){
 const x=state.social||{friends:[],incoming:[],outgoing:[],conversations:[],points:{total_points:'0',current_streak:'0'}};
 const friendRows=x.friends.map(f=>`<div class="feed-item"><strong>${esc(f.note)}</strong> <span class="code">${esc(f.uid)}</span><div class="hint">${esc(f.display_name)} · 积分 ${esc(f.total_points)} · 连胜 ${esc(f.current_streak)}</div><div class="buttonrow"><button class="small" data-action="dm-open" data-uid="${esc(f.uid)}">私信</button><button class="small" data-action="friend-note" data-uid="${esc(f.uid)}">修改备注</button><button class="small" data-action="friend-reset-note" data-uid="${esc(f.uid)}">恢复默认</button><button class="small danger" data-action="friend-delete" data-uid="${esc(f.uid)}">删除好友</button></div></div>`).join('')||D('暂无好友');
 const incoming=x.incoming.map(f=>`<div class="feed-item">${esc(f.display_name)} · ${esc(f.uid)} <button class="small" data-action="friend-accept" data-id="${f.id}">接受</button> <button class="small" data-action="friend-reject" data-id="${f.id}">拒绝</button></div>`).join('')||D('没有收到的申请');
 const outgoing=x.outgoing.map(f=>`<div class="feed-item">${esc(f.display_name)} · ${esc(f.uid)} <button class="small" data-action="friend-withdraw" data-id="${f.id}">撤回</button></div>`).join('')||D('没有发出的申请');
 const conversations=x.conversations.map(c=>`<button class="room-view-link" data-action="dm-open" data-uid="${esc(c.uid)}"><strong>${esc(c.note||c.display_name)}</strong> · ${esc(c.uid)} ${c.unread_count?`<span class="pill">${c.unread_count} 未读</span>`:''}</button>`).join('')||D('暂无私信会话');
 return `<div id="socialPanel" class="stack"><section class="card"><h2>账号积分</h2><div class="stat-row"><div class="stat">总积分 <strong>${esc(x.points.total_points)}</strong></div><div class="stat">当前连胜 <strong>${esc(x.points.current_streak)}</strong></div></div><div id="pointsLedger" class="feed spaced"></div><button class="small" data-action="points-ledger">${state.pointsLedgerLoaded?'刷新积分流水':'查看积分流水'}</button></section>
 <section class="card"><h2>好友与私信</h2><form data-form="friend-search"><label class="field">通过 UID 搜索</label><input name="uid" maxlength="40" required placeholder="TL00000001"><div class="form-actions"><button class="primary" type="submit">搜索</button></div></form><div id="friendSearchResult"></div><h3 class="spaced">好友</h3>${friendRows}<h3 class="spaced">收到的申请</h3>${incoming}<h3 class="spaced">发出的申请</h3>${outgoing}<h3 class="spaced">私信会话</h3>${conversations}<div id="dmPanel"></div></section></div>`;
}
function renderPersonalCenter({skipModelDraftKey=null}={}){
 const u=state.me?.user;if(!u){clearPersonalModelState();renderAuth();return;}
 captureDmDraft();
 capturePersonalModelDrafts(skipModelDraftKey);
 if(skipModelDraftKey)delete state.personalModelDrafts[skipModelDraftKey];
 stopWs();
 app.innerHTML='<div class="pagehead"><div><div class="overline">ACCOUNT / PERSONAL CENTER</div><h1>个人中心</h1><p class="muted">管理当前账号的基本资料、登录密码和个人模型 API 配置。</p></div><div class="buttonrow"><button data-action="personal-back">← 返回'+(state.personalReturn?.roomId?'房间':'大厅')+'</button></div></div>'+
 '<div class="personal-layout"><div class="stack">'+
 '<section class="card"><h2>基本资料</h2><div class="uid-row"><div><label class="field">UID（不可修改）</label><div class="code"> '+esc(u.uid)+'</div></div><button class="small" data-action="copy-uid" data-uid="'+esc(u.uid)+'">复制 UID</button></div><form data-form="profile-name" class="spaced"><label class="field">名字</label><input name="display_name" type="text" maxlength="40" required value="'+esc(u.display_name)+'"'+(u.name_change_locked?' disabled':'')+'><div class="form-actions"><button class="primary" type="submit"'+(u.name_change_locked?' disabled':'')+'>保存名字</button></div>'+(u.name_change_locked?'<p class="hint">比赛进行中，暂不能修改名字</p>':'')+'</form></section>'+
 renderSocialPanel()+ '<section class="card"><h2>修改密码</h2><p class="hint">修改成功后，当前账号的所有登录会话都会退出，需要使用新密码重新登录。</p><form data-form="change-password"><label class="field">当前密码</label><input name="current_password" type="password" autocomplete="current-password" maxlength="200" required><label class="field">新密码（8～200 位）</label><input name="new_password" type="password" autocomplete="new-password" minlength="8" maxlength="200" required><label class="field">确认新密码</label><input name="confirm_password" type="password" autocomplete="new-password" minlength="8" maxlength="200" required><div class="form-actions"><button class="primary" type="submit">修改密码并退出登录</button></div></form></section>'+
 '</div><div class="stack"><section class="card"><h2>个人模型 API</h2>'+renderPersonalModelManager()+'</section>'+renderModelOperationRecords()+'</div></div>';
 renderDmPanel();renderPointsLedger();
}
function modelProfileOptions(r){
 const profiles=(state.personalModels||[]).filter(p=>p.credential_status==='ok');
 return `<option value="">手动转录（不调用 API）</option>${profiles.map(p=>`<option value="${p.id}">${esc(p.name)} · ${esc(p.model)}</option>`).join('')}`;
}
function modelBindingOptions(current=null){
 const profiles=(state.personalModels||[]).filter(p=>p.credential_status==='ok');
 const currentId=current?.model_profile_id??null;
 let html='';
 if(currentId&&!profiles.some(p=>p.id===currentId))html+='<option value="__unchanged__" selected>保持当前已授权绑定</option>';
 html+='<option value="" '+(!currentId?'selected':'')+'>手动转录（不调用 API）</option>';
 html+=profiles.map(p=>'<option value="'+p.id+'" '+(p.id===currentId?'selected':'')+'>'+esc(p.name)+' · '+esc(p.model)+'</option>').join('');
 return html;
}
function bindingLabel(item){
 if(!state.modelApiEnabled&&item.model_profile_id)return '模型 API 暂不可用，请联系管理员。';
 if(!item.model_profile_id)return '手动转录';
 const p=item.model_profile;if(!p)return '配置不可用';
 return `${p.name} · ${p.model}（${p.owned_by_me?'我的配置':'其他账号的已授权配置'} · ${credentialStatusLabel(p)}）`;
}

function renderHumanJudgeAdminPanel(r){
 const judges=(r.judges||[]).filter(j=>j.kind==='human');
 return '<section class="card compact"><div class="label-line"><h2>真人裁判管理</h2><span class="pill">'+judges.length+' 位真人裁判</span></div>'+
 '<div class="feed">'+judges.map(j=>'<div class="feed-item"><div class="label-line"><strong>'+esc(j.label)+'</strong><span class="pill '+(j.locked?'ok':'')+'">'+(j.locked?'✓ 已锁定':'未锁定')+'</span></div><div class="tiny muted">UID：'+esc(j.uid||'不可用')+'</div><button class="small spaced" data-action="copy-uid" data-uid="'+esc(j.uid||'')+'">复制 UID</button></div>').join('')+'</div></section>';
}
function personaEditor(type,key,p,disabled){
 p=p||{};const presets=type==='participant'?[['default','默认风格'],['minimal','极简交流者'],['casual','日常闲聊者'],['analytical','严谨分析者'],['emotional','情绪表达者'],['custom','自定义']]:[['balanced','默认均衡'],['language_detail','语言细节'],['cross_round','跨轮一致性'],['custom','自定义']];
 const lengths=[['default','沿用模型上限'],['short','短'],['medium','中'],['long','长']];
 const opts=presets.map(x=>'<option value="'+x[0]+'" '+((p.preset_id||presets[0][0])===x[0]?'selected':'')+'>'+x[1]+'</option>').join('');
 const lens=lengths.map(x=>'<option value="'+x[0]+'" '+((p.output_length||'default')===x[0]?'selected':'')+'>'+x[1]+'</option>').join('');
 return '<details class="persona-editor"><summary>人设与回答策略 · '+esc(p.display_name||p.preset_id||'默认风格')+'</summary><form data-form="seat-persona" data-seat-type="'+type+'" data-seat-key="'+esc(key)+'" class="stack spaced">'+
 '<label class="field">人设预设</label><select name="preset_id" '+(disabled?'disabled':'')+'>'+opts+'</select>'+
 '<label class="field">自定义 System Prompt（最多 8000 字；选择自定义时必填）</label><textarea name="custom_system_prompt" maxlength="8000" '+(disabled?'disabled':'')+'>'+esc(p.custom_system_prompt||'')+'</textarea>'+
 '<div class="grid2"><div><label class="field">输出长度</label><select name="output_length" '+(disabled?'disabled':'')+'>'+lens+'</select></div><div><label class="field">出题历史轮数（0～100）</label><input name="history_rounds" type="number" min="0" max="100" value="'+esc(p.history_rounds??5)+'" '+(disabled?'disabled':'')+'></div></div>'+
 '<p class="tiny muted">历史轮数只影响回答和出题；最终裁判判断仍读取完整匿名资料。正式揭晓后会展示自定义提示词，请勿填写密钥。</p><button class="small" type="submit" '+(disabled?'disabled':'')+'>保存人设</button></form></details>';
}
function renderAIBindingControls(r){
 if(!r.ais?.length)return '';
 const disabled=r.status!=='open'||r.verdict_locked||r.personas_frozen||(r.judges||[]).some(j=>j.locked);
 return '<div class="divider"></div><h3>AI 参赛席模型绑定</h3><p class="hint">更换或解除绑定不会删除 AI 席位及其匿名代号、历史回答。你只能选择自己拥有的配置；其他账号授权的现有绑定可以保留或解除。</p>'+
 r.ais.map(a=>'<form class="binding-row" data-form="bind-ai-player-model" data-alias="'+esc(a.alias)+'"><div><strong>'+esc(a.alias)+' · '+esc(a.name)+'</strong><div class="tiny muted">当前：'+esc(bindingLabel(a))+'</div></div><select name="model_profile_id" '+(disabled?'disabled':'')+'>'+modelBindingOptions(a)+'</select><button class="small" type="submit" '+(disabled?'disabled':'')+'>更新绑定</button></form>'+personaEditor('participant',a.alias,a.persona,disabled)).join('');
}
function aiQuestionFeedback(result){
 const details=(result.results||[]).filter(x=>x.status==='failed'||x.status==='skipped'||x.status==='blocked').map(x=>{
  const who=x.label||('AI 裁判 '+(x.judge_id??''));
  return who+'：'+(x.error||x.reason||'未生成问题');
 });
 if(!details.length&&(result.failed||[]).length)details.push(...result.failed.map(x=>(x.label||'AI 裁判')+'：'+x.error));
 const prefix=result.generated?('成功生成 '+result.generated+' 个 AI 裁判问题'):(result.message||'没有 AI 裁判成功生成问题');
 return details.length?prefix+'；'+details.join('；'):prefix;
}
function judgeAdminPanel(r){
 const judges=(r.judges||[]).filter(j=>j.kind==='ai');
 const pendingRound=r.rounds.find(x=>x.number===r.current_round);
 const hasQuestion=j=>pendingRound?.questions?.some(q=>q.kind==='ai'&&q.judge_id===j.id);
 const usable=j=>j.model_profile?.credential_status==='ok';
 const pendingAis=judges.filter(j=>!usable(j)&&!hasQuestion(j));
 const autoPending=judges.filter(j=>usable(j)&&!hasQuestion(j));
 const participantCount=(r.humans||[]).length+(r.ais||[]).length;
 const awaiting=judges.filter(j=>!j.locked);
 const manualAwaiting=awaiting.filter(j=>!usable(j));
 const autoAwaiting=awaiting.filter(j=>usable(j));
 const aliases=[...r.humans,...r.ais].map(x=>x.alias).sort();
 const canEdit=r.status==='open'&&!r.verdict_locked&&!r.personas_frozen&&!judges.some(j=>j.locked);
 let html='<section class="card compact"><div class="label-line"><h2>AI 裁判管理</h2><span class="pill">'+judges.length+' 位 AI 裁判</span></div>'+
 '<p class="hint">AI 裁判可绑定你自己的个人模型配置自动出题和判断，也可手动转录。绑定后，本房间房主和全站管理员可通过比赛操作触发该配置的模型调用，服务商可能按实际调用量计费。绑定他人配置时，只能看到当前席位的必要摘要，不能读取或编辑对方的个人配置。</p>';
 if(judges.length){
  html+=judges.map(j=>'<div class="feed-item"><div class="label-line"><strong>'+esc(j.label)+'</strong><span class="pill '+(j.locked?'ok':'')+'">'+(j.locked?'✓ 已锁定':'未锁定')+'</span></div>'+
   '<div class="tiny muted">当前绑定：'+esc(bindingLabel(j))+'</div><form data-form="bind-ai-judge-model" data-id="'+j.id+'" class="binding-row"><select name="model_profile_id" '+(canEdit?'':'disabled')+'>'+modelBindingOptions(j)+'</select><button class="small" type="submit" '+(canEdit?'':'disabled')+'>更新绑定</button></form>'+personaEditor('judge',j.id,j.persona,!canEdit)+'</div>').join('');
 }else html+=D('尚无 AI 裁判席。');
 if(r.model_request_limit!==null&&r.model_request_limit!==undefined&&
    Number(r.model_request_count)>=Number(r.model_request_limit)){
  html+='<p class="hint">本场模型请求次数已达上限；可提高上限或手动转录。</p>';
 }
 if(autoPending.length){
  html+='<div class="divider"></div><p class="hint">'+autoPending.length+' 位模型 AI 裁判尚未提交第 '+r.current_round+' 轮问题。</p>';
  if(participantCount<2)html+='<p class="hint">当前仅有 '+participantCount+' 位参赛者；至少需要两名在场参赛者才能出题，请先让两位参赛者加入。</p><button class="primary" data-action="generate-ai-questions" disabled>让模型自动生成本轮问题</button>';
  else html+='<button class="primary" data-action="generate-ai-questions">让模型自动生成本轮问题</button>';
 }
 if(pendingRound?.status==='draft'&&pendingAis.length){
  html+='<div class="divider"></div><form data-form="ai-judge-question"><h3>代录 AI 裁判的第 '+r.current_round+' 轮问题</h3><p class="hint">手动转录或配置暂不可用的 AI 裁判，每席每轮只能提交一次。</p><select name="judge_id">'+pendingAis.map(j=>'<option value="'+j.id+'">'+esc(j.label)+'</option>').join('')+'</select><textarea name="text" data-draft="ai-judge-question" maxlength="3000" required placeholder="粘贴 AI 裁判提出的问题原文…">'+myDraft('ai-judge-question')+'</textarea><div class="form-actions"><button class="primary" type="submit">提交 AI 裁判问题</button></div></form>';
 }
 if(!r.revealed_at&&!r.verdict_locked&&canEdit){
  html+='<div class="divider"></div><form data-form="add-ai-judge"><h3>添加 AI 裁判</h3><label class="field">AI 裁判名称</label><input type="text" name="name" required maxlength="60" placeholder="例如 DeepSeek-R1"><label class="field">个人模型配置</label><select name="model_profile_id">'+modelProfileOptions(r)+'</select><div class="form-actions"><button class="primary" type="submit">＋ 添加 AI 裁判</button></div></form>';
 }
 if(!r.revealed_at&&r.rounds.every(x=>x.status==='revealed')&&autoAwaiting.length){
  html+='<div class="divider"></div><p class="hint">'+autoAwaiting.length+' 位模型 AI 裁判尚未完成最终判断。</p><button class="primary" data-action="generate-ai-verdicts">让模型自动完成最终判断</button>';
 }
 if(!r.revealed_at&&r.rounds.every(x=>x.status==='revealed')&&manualAwaiting.length){
  html+='<div class="divider"></div><form data-form="ai-judge-verdict"><h3>代录 AI 裁判最终判断</h3><label class="field">选择 AI 裁判</label><select name="judge_id">'+manualAwaiting.map(j=>'<option value="'+j.id+'">'+esc(j.label)+'</option>').join('')+'</select>'+
   aliases.map(a=>'<div class="label-line" style="margin:8px 0"><label>匿名参赛者 '+esc(a)+'</label><select name="pred-'+esc(a)+'" required style="max-width:130px"><option value="">请选择</option><option value="Human">人类</option><option value="AI">AI</option></select></div>').join('')+
   '<label class="field">AI 裁判判断依据</label><textarea name="evidence" maxlength="5000" required placeholder="复制 AI 裁判的判断依据…"></textarea><label class="field">置信度（0～100）</label><input name="confidence" type="number" min="0" max="100" value="50" required><label class="field">最弱环节（可选）</label><textarea name="weakness" maxlength="5000"></textarea><label class="field">可能误判原因（可选）</label><textarea name="possible_error" maxlength="5000"></textarea><div class="form-actions"><button class="primary" type="submit">锁定该 AI 裁判判断</button></div></form>';
 }
 return html+'</section>';
}

function workflowStateText(value){
 const labels={queued:'排队中',generating_questions:'正在生成裁判问题',starting_round:'正在开启作答',
  generating_answers:'正在生成 AI 回答',waiting_input:'等待输入或人工确认',blocked:'等待处理阻塞',
  failed:'执行失败',completed:'自动步骤已完成',cancelled:'已取消',stale:'已作废'};
 return labels[value]||value||'未知';
}
function renderPersonaPreparationControl(r){
 if(!r.can_manage||!r.personas_frozen||r.match_started||r.current_round!==1)return '';
 return '<section class="card compact"><div class="label-line"><h2>人设配置已冻结</h2><span class="pill">首轮尚未开始</span></div><p class="hint">如需修改 AI 席位或人设，先取消准备。旧 AI 裁判题目和在途任务会作废，真人裁判问题保留。</p><button class="small danger" data-action="reset-persona-preparation">取消准备并编辑人设</button></section>';
}
function renderRoundWorkflowPanel(r){
 const wf=r.round_workflow;if(!wf)return '';
 const actions=[];
 if(wf.can_resume)actions.push('<button class="small primary" data-action="resume-round-workflow" data-id="'+wf.id+'">恢复工作流</button>');
 if(wf.can_cancel)actions.push('<button class="small danger" data-action="cancel-round-workflow" data-id="'+wf.id+'">取消准备</button>');
 return '<section class="card compact workflow-card"><div class="label-line"><h2>服务端比赛编排</h2><span class="pill">'+esc(workflowStateText(wf.state))+'</span></div>'+
  '<p class="hint">任务由服务器持续推进；关闭网页、刷新或断线不会中断已授权的工作流。</p>'+
  (wf.waiting_reason?'<p class="hint">'+esc(wf.waiting_reason)+'</p>':'')+
  (wf.error_message?'<p class="operation-error">'+esc(wf.error_message)+'</p>':'')+
  (actions.length?'<div class="buttonrow spaced">'+actions.join('')+'</div>':'')+
  '</section>';
}
function renderRoundTimerSettings(r){
 if(!r.can_manage||r.status!=='open')return '';
 const value=r.round_duration_seconds===null||r.round_duration_seconds===undefined?'':esc(r.round_duration_seconds);
 return '<section class="card compact"><div class="label-line"><h2>单轮答题限时</h2><span class="pill">'+(value?'已启用':'已关闭')+'</span></div>'+
  '<p class="hint">留空表示关闭；启用范围为 30～3600 秒。当前轮次开始后，修改只影响后续轮次。</p>'+
  '<form data-form="round-timer" class="operation-limit-form"><input name="seconds" type="number" min="30" max="3600" step="1" value="'+value+'" placeholder="留空表示关闭">'+
  '<button class="small" type="submit">保存限时</button></form></section>';
}
function serverNowMs(){return Date.now()+(Number(state.serverTimeOffset)||0)}
function formatCountdown(ms){
 const seconds=Math.max(0,Math.ceil(ms/1000));
 const hours=Math.floor(seconds/3600),minutes=Math.floor((seconds%3600)/60),rest=seconds%60;
 return hours?String(hours).padStart(2,'0')+':'+String(minutes).padStart(2,'0')+':'+String(rest).padStart(2,'0'):
  String(minutes).padStart(2,'0')+':'+String(rest).padStart(2,'0');
}
function renderRoundTimer(r,round,active){
 if(round.duration_seconds===null||round.duration_seconds===undefined)return '';
 const manager=!!r.can_manage&&active&&round.status==='collecting';
 return '<div class="notice round-timer" data-round-timer><div class="label-line"><strong>本轮限时 · '+Math.round(round.duration_seconds/60*10)/10+' 分钟</strong>'+
  '<strong id="roundCountdown" data-deadline="'+esc(round.deadline_at||'')+'">计算中…</strong></div>'+
  (manager?'<div class="buttonrow spaced"><button class="small danger" id="finishRoundTimeoutBtn" data-action="finish-round-timeout" data-round="'+round.number+'" disabled>结束本轮</button></div>':'')+
  '</div>';
}
function updateRoundCountdown(){
 const el=$('#roundCountdown');if(!el)return;
 const deadline=Date.parse(el.dataset.deadline||'');
 if(!Number.isFinite(deadline))return;
 const remaining=deadline-serverNowMs();
 const round=state.room?.rounds?.find(x=>x.number===state.room.current_round);
 const expired=remaining<=0;
 el.textContent=expired?'时间已到':formatCountdown(remaining);
 const button=$('#finishRoundTimeoutBtn');
 if(button)button.disabled=!expired||round?.status!=='collecting';
 const notice=el.closest('[data-round-timer]');
 if(notice&&expired&&round?.status==='collecting'){
  const hint=notice.querySelector('[data-timeout-hint]');
  if(!hint){const p=document.createElement('p');p.className='hint';p.dataset.timeoutHint='1';p.textContent='已到截止时间；本轮不会自动结束，请房主确认结束。';notice.appendChild(p);}
 }
}
function renderReadinessPanel(r){
 const workflowPanel=renderRoundWorkflowPanel(r);
 const x=r.readiness;if(!x||x.phase!=='draft')return workflowPanel;
 const limit=x.model_request_limit===null?'不限':x.model_request_limit;
 const blockers=(x.blockers||[]).map(message=>'<li>'+esc(message)+'</li>').join('');
 const notices=(x.notices||[]).map(message=>'<li>'+esc(message)+'</li>').join('');
 const wf=r.round_workflow;
 const workflowBlocksNew=wf&&['queued','generating_questions','starting_round','generating_answers',
  'waiting_input','blocked','failed'].includes(wf.state);
 let actions='';
 if(workflowBlocksNew)actions+='<button class="primary" disabled>服务端工作流处理中</button>';
 else if(x.can_generate_and_start)actions+='<button class="primary" data-action="start-round" data-generate-questions="true">生成问题并开始</button>';
 else if(x.can_start)actions+='<button data-action="start-round" data-generate-questions="false">按已提交问题开始</button>';
 else actions+='<button data-action="start-round" disabled>开始本轮</button>';
 if(!workflowBlocksNew&&!x.can_start&&!x.can_generate_and_start){
  if(x.participant_count<2)actions+='<button class="small" data-action="room-view" data-view="admin">前往参赛者管理补足参赛者</button>';
  if(x.manual_question_required)actions+='<button class="small" data-action="focus-manual-question">前往外部裁判问题录入</button>';
  else if(x.participant_count>=2&&x.suggested_view&&x.suggested_view!=='admin')actions+='<button class="small" data-action="room-view" data-view="'+esc(x.suggested_view)+'">前往裁判管理</button>';
 }
 return workflowPanel+'<section class="card compact readiness-card"><div class="label-line"><h2>本轮准备</h2><span class="pill">'+(x.can_start?'可以开始':'尚未就绪')+'</span></div>'+
  '<div class="readiness-grid"><div><span class="tiny muted">参赛者</span><strong>'+x.participant_count+' 人已加入</strong></div>'+
  '<div><span class="tiny muted">裁判问题</span><strong>已提交 '+x.submitted_judges+' / '+x.total_judges+' 位裁判 · '+x.question_count+' 份</strong></div>'+
  '<div><span class="tiny muted">自动席位</span><strong>'+x.auto_pending+' 位可用模型 AI 裁判待出题'+(x.unavailable_ai_pending?' · '+x.unavailable_ai_pending+' 位配置不可用':'')+'</strong></div>'+
  '<div><span class="tiny muted">本场请求</span><strong>已尝试 '+x.model_request_count+' / '+limit+'</strong></div></div>'+
  (blockers?'<ul class="readiness-messages danger-text">'+blockers+'</ul>':'')+
  (notices?'<ul class="readiness-messages">'+notices+'</ul>':'')+
  '<div class="buttonrow spaced">'+actions+'</div>'+
  '<div class="divider"></div><div class="label-line"><div><strong>本场模型请求上限</strong><p class="hint">每个实际进入模型请求阶段的席位尝试计 1 次；失败和超时也计入。留空表示不限。</p></div></div>'+
  '<form data-form="model-request-limit" class="operation-limit-form"><input name="limit" type="number" min="1" max="1000000" step="1" value="'+(x.model_request_limit===null?'':esc(x.model_request_limit))+'" placeholder="留空表示不限"><button class="small" type="submit">保存上限</button></form>'+
  '</section>';
}
function renderModelOperationPanel(r){
 const tasks=r.model_operations||[],history=r.model_operation_history||[];
 const historyKey=String(r.id)+':'+String(r.match_number);
 const renderTask=(t,allowRetry)=>{
  const retryable=allowRetry&&!t.workflow_id&&['failed','interrupted','blocked'].includes(t.state);
  const request=t.request_succeeded===true?'请求成功':t.request_succeeded===false?'请求失败':t.request_started_at?'请求结果待确认':'尚未开始请求';
  const applied=t.result_applied?'已写入比赛':t.request_succeeded===true?'结果未应用':'未写入比赛';
  const usage=t.usage_known?'Token：'+operationTokens(t.total_tokens):'Token：未返回';
  return '<article class="operation-record"><div class="label-line"><strong>'+esc(t.target_label)+' · '+operationPurposeLabel(t.purpose)+'</strong><span class="operation-status '+esc(t.state)+'">'+operationStateLabel(t.state)+'</span></div>'+
   '<div class="tiny muted">'+esc(t.profile_name)+' · '+esc(t.model_name)+'</div>'+
   '<div class="operation-meta"><span>'+request+'</span><span>'+applied+'</span></div>'+
   '<div class="tiny muted">等待 '+(t.queue_ms===null?'—':esc(t.queue_ms)+' ms')+' · 请求 '+(t.duration_ms===null?'—':esc(t.duration_ms)+' ms')+' · '+usage+'</div>'+
   (t.retry_of?'<div class="tiny muted">重试自任务 #'+esc(t.retry_of)+'</div>':'')+
   (t.error_message?'<p class="operation-error">'+esc(t.error_message)+'</p>':'')+
   (retryable?'<div class="buttonrow spaced"><button class="small" data-action="retry-model-operation" data-id="'+t.id+'">重试此席位</button></div>':'')+
   '</article>';
 };
 const currentHtml=tasks.length?'<section class="card compact"><div class="label-line"><h2>模型调用状态</h2><span class="pill">'+tasks.length+' 个任务</span></div><p class="hint">显示本场当前阶段的模型任务。</p><div class="operation-list">'+tasks.map(t=>renderTask(t,true)).join('')+'</div></section>':'';
 const historyHtml=history.length?'<section class="card compact"><details class="operation-history" data-model-operation-history data-key="'+esc(historyKey)+'" '+(state.modelOperationHistoryPanels[historyKey]?'open':'')+'><summary class="label-line"><strong>历史模型调用</strong><span class="pill">'+history.length+' 条</span></summary><div class="operation-history-body"><p class="hint">本场此前阶段的调用记录；旧阶段任务不能在这里重试。</p><div class="operation-list">'+history.map(t=>renderTask(t,false)).join('')+'</div></div></details></section>':'';
 return currentHtml+historyHtml;
}
function renderJudgeWorkflowPanel(r){
 const judges=r.judges||[];
 const pending=r.rounds.find(x=>x.number===r.current_round);if(!judges.length&&!pending?.questions?.length)return '';
 const allDone=judges.every(j=>j.locked);
 const allRoundsDone=r.rounds.length>0&&r.rounds.every(x=>x.status==='revealed');
 let html='<section class="card compact"><div class="label-line"><h2>统一作答与揭晓</h2><span class="pill">'+judges.filter(j=>j.locked).length+'/'+judges.length+' 位裁判已锁定</span></div>';
 if(!r.revealed_at&&allDone&&allRoundsDone)html+='<button class="danger" data-action="reveal">正式揭晓</button>';
 return html+'</section>';
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
 const answers=round.status==='revealed'?aliases.map(a=>`<div class="answer-card"><strong>匿名参与者 ${esc(a)}</strong>${round.timed_out?.includes(a)?'<span class="pill">超时未提交</span>':''}${round.answers[a]?.edited_at?'<span class="pill">本轮答复已修订</span>':''}<div class="answer-content">${esc(round.answers[a]?.text||(round.timed_out?.includes(a)?'（超时未提交）':'（未提交）'))}</div></div>`).join(''):
 `<div class="notice">🔒 尚未全部提交：${round.submitted.length}/${aliases.length}。在所有回答统一公开前，裁判同样不能读取任何回答正文。</div>`;
 const buttons=r.rounds.map(x=>`<button class="small ${x.number===n?'active':''}" data-action="round" data-id="${x.number}">第 ${x.number} 轮 ${x.status==='revealed'?'✓':''}</button>`).join('');
 const form=canLock?`<form data-form="judge-verdict"><p class="hint">只根据匿名回答判断。所有预测只保存在你的独立记录中，其他裁判始终无法通过网站读取。</p>${aliases.map(a=>`<div class="label-line" style="margin:8px 0"><label>参与者 ${esc(a)}</label><select name="pred-${esc(a)}" required><option value="">选择</option><option value="Human">人类</option><option value="AI">AI</option></select></div>`).join('')}<label class="field">判断依据</label><textarea name="evidence" data-draft="judge-evidence" required maxlength="5000">${myDraft('judge-evidence')}</textarea><label class="field">置信度 (0-100)</label><input name="confidence" type="number" min="0" max="100" value="50" required><label class="field">最弱环节（可选）</label><textarea name="weakness" maxlength="5000"></textarea><label class="field">可能误判原因（可选）</label><textarea name="possible_error" maxlength="5000"></textarea><div class="form-actions"><button class="primary" type="submit">锁定我自己的判断</button></div></form>`:
 r.my_verdict?`<div class="locked">🔒 你的判决已锁定，不能修改。其他裁判的判断对你保密。</div><div class="answer-card"><strong>我的判断</strong><div class="answer-content">${aliases.map(a=>a+'：'+(r.my_verdict.predictions[a]==='Human'?'人类':'AI')).join('\n')}</div><div class="hint">依据：${esc(r.my_verdict.evidence)}</div></div>`:
 '';
 app.innerHTML=`<div class="pagehead"><div><div class="overline">INDEPENDENT JUDGE / ROOM ${r.id}</div><h1>${esc(r.title)}</h1><div class="stat-row"><span class="pill">独立裁判 · ${esc(r.my_judge_label)}</span><span class="pill">${aliases.length} 位匿名参赛者</span><span class="pill">${finished?'比赛已结束':'匿名盲测中'}</span></div></div><div class="buttonrow"><button data-action="personal-center">个人中心</button><button data-action="back" class="ghost">← 返回大厅</button><button data-action="sync">↻ 刷新</button></div></div>
 ${finishedActions(r)}<div class="room-layout"><aside class="aside"><section class="card flat"><h2>匿名参赛者</h2><p class="hint">仅显示固定代号，隐藏所有账号、昵称和真实身份。即使比赛结束，此视图也不显示真实身份。</p>${aliases.map(a=>`<div class="roster-person"><span class="alias">${esc(a)}</span><span>匿名参赛者</span></div>`).join('')}</section><section class="card flat"><h2>比赛轮次</h2><div class="round-tags">${buttons}</div></section></aside>
 <div class="stack"><section class="card"><div class="label-line"><h2>第 ${n} 轮问题</h2><span class="pill">${round.status==='draft'?'收集问题中':round.status==='collecting'?'等待答复':'回答已公开'}</span></div><div class="feed">${questions}</div>${round.question?`<div class="question spaced">${esc(round.question)}</div>${renderRoundTimer(r,round,active)}`:''}<div class="buttonrow spaced"><button data-action="download-log" class="small">↓ 下载匿名 Log</button>${finished?'<button data-action="download-full-log" class="small">↓ 下载整场匿名 Log</button>':''}</div></section>
 <section class="card"><h2>匿名回答</h2>${answers}</section></div>
 <div class="right-panel"><section class="card"><h2>我的裁判操作</h2>${active&&pending&&!questionSubmitted?`<form data-form="judge-question"><p class="hint">每位裁判每轮最多提交一个问题，房主统一开启本轮作答。</p><textarea name="text" data-draft="judge-question" maxlength="3000" required placeholder="输入你要向所有参赛者提出的问题…">${myDraft('judge-queion')}</textarea><div class="form-actions"><button class="primary" type="submit">提交并实时同步问题</button></div></form>`:pending?D('你已提交本轮问题，等待房主开启作答。'):D('本轮已经开始作答，不能再添加新问题。')}
 <div class="divider"></div><h3>独立身份判决</h3>${form}</section></div></div>`;
}
function renderMultiResults(r,roster){
 return `<section class="card"><div class="overline">MATCH ACCURACY / 比赛识别统计</div><h2>多裁判独立判决结果</h2><p class="hint">比赛结束后向人类参与者展示全部裁判判决及评分；各裁判登录时仍只能读取自己的判断。</p>${r.verdicts.map(v=>{const count=roster.filter(p=>v.predictions?.[p.alias]===(p.role==='AI'?'AI':'Human')).length;return `<div class="answer-card"><div class="label-line"><h3>${esc(v.judge)} <span class="tiny muted">${v.kind==='ai'?'AI 裁判':v.kind==='human'?'真人裁判':''}</span></h3><span class="pill ok">${count} / ${roster.length} 正确</span></div><div class="hint">置信度 ${v.confidence}%</div>${roster.map(p=>`<div class="hint">${esc(p.alias)} · ${p.role} · 判断：${v.predictions?.[p.alias]==='Human'?'人类':'AI'} ${v.predictions?.[p.alias]===(p.role==='AI'?'AI':'Human')?'✓':'✗'}</div>`).join('')}<div class="question spaced">${esc(v.evidence)}</div><p class="hint">最弱环节：${esc(v.weakness||'未填写')}</p><p class="hint">可能误判：${esc(v.possible_error||'未填写')}</p></div>`}).join('')}</section>`+(r.seat_personas?.length?`<section class="card"><h2>本场 AI 席位人设</h2><p class="hint">以下内容来自本场冻结快照，不是个人中心当前配置。</p>${r.seat_personas.map(p=>`<div class="answer-card"><strong>${esc(p.seat_key)} · ${esc(p.seat_label)} · ${esc(p.persona_name)}</strong><div class="hint">${p.seat_type==='participant'?'AI 参赛者':'AI 裁判'} · 输出：${esc(p.output_length)} · 出题历史：${p.history_rounds} 轮</div>${p.custom_system_prompt?`<div class="question spaced">${esc(p.custom_system_prompt)}</div>`:''}</div>`).join('')}</section>`:'');
}
function renderAdminRoomNav(r,finished){
 const views=[['match','比赛现场'],['human-judges','真人裁判管理'],['ai-judges','AI 裁判管理'],['admin','参赛者管理']];
 return '<nav class="card flat room-view-menu" aria-label="房间页面导航"><div class="overline">房间导航 / NAVIGATION</div><h2>操作页面</h2>'+
 views.map(([view,title])=>'<button type="button" class="room-view-link '+(state.roomView===view?'active':'')+'" data-action="room-view" data-view="'+view+'" '+(state.roomView===view?'aria-current="page"':'')+'><strong>'+title+'</strong></button>').join('')+
 
 '<div class="room-view-hint">第 '+r.current_round+' 轮 · '+(finished?'比赛已结束':'比赛进行中')+'</div></nav>';
}
function renderRoom(){
 const editing=document.activeElement;
 const editingDraft=editing?.dataset?.draft;
 const editingPos=typeof editing?.selectionStart==='number'?editing.selectionStart:null;
 const r=state.room;if(!r)return;if(r.status==='closed'){renderClosedRoom(r);return;}if(r.records_deleted){renderReadyRoom(r);return;}if(r.is_judge){renderJudgeRoom(r);return;}const n=state.selectedRound||r.current_round;const round=r.rounds.find(x=>x.number===n)||r.rounds[r.rounds.length-1];const active=n===r.current_round;const finished=r.status==='finished';const canManage=r.can_manage;const humans=r.humans.length,ais=r.ais.length;const submitted=new Set(round.submitted);const locked=!!r.verdict_locked||(r.judges||[]).some(j=>j.locked);
 const rtab=r.rounds.map(x=>`<button class="small ${x.number===n?'active':''}" data-action="round" data-id="${x.number}">第 ${x.number} 轮 ${x.status==='revealed'?'✓':''}</button>`).join('');
 const roster=[...r.humans.map(p=>({...p,role:'人类',label:p.name})),...r.ais.map(p=>({...p,role:'AI',label:p.name}))].sort((a,b)=>a.alias.localeCompare(b.alias));
 const answerView=round.status==='revealed'?roster.map(p=>`<div class="answer-card"><strong>${esc(p.alias)} · ${esc(p.label)}</strong><span class="pill" style="margin-left:7px">${p.role}</span>${round.timed_out?.includes(p.alias)?'<span class="pill">超时未提交</span>':''}<div class="tiny muted">${esc(shortDate(round.answers[p.alias]?.submitted_at))}</div>${round.answers[p.alias]?.edited_at?'<span class="pill">主持人已修订</span>':''}<div class="answer-content">${esc(round.answers[p.alias]?.text||(round.timed_out?.includes(p.alias)?'（超时未提交）':'（无回答）'))}</div></div>`).join(''):
   `<div class="notice">🔒 回答暂时隐藏。已提交 ${round.submitted.length} / ${roster.length}；全部参赛者（含 AI）提交后，服务器会自动向所有在线玩家公布。</div>${round.answers[r.mine]?.text?`<div class="answer-card"><strong>我已提交的回答</strong><div class="answer-content">${esc(round.answers[r.mine].text)}</div></div>`:''}`;
 const canAnswer=active&&!finished&&round.status==='collecting'&&r.mine&&!submitted.has(r.mine);
 const judgeBox= canManage?`<section class="card compact"><div class="label-line"><h2>参赛者管理</h2><span class="pill">${r.is_owner?'房主':'管理员'}</span></div>${renderRoundTimerSettings(r)}
 ${active&&round.status==='draft'&&!finished&&!round.questions?.length&&!(r.judges||[]).length?`<div class="divider"></div><form id="manual-question-form" data-form="question"><label class="field">录入裁判的第 ${n} 轮问题</label><textarea name="question" data-draft="question" maxlength="6000" required placeholder="从裁判聊天窗口复制原文…">${myDraft('question')}</textarea><div class="form-actions"><button class="primary" type="submit">向所有玩家发布问题</button></div></form>`:''}
 ${active&&round.status==='collecting'&&!finished&&r.ais.some(a=>a.model_profile_id&&a.model_profile?.credential_status==='ok'&&!submitted.has(a.alias))?`<div class="divider"></div><button class="primary" data-action="generate-ai-answers">自动生成模型 AI 回复</button>`:''}
  ${active&&round.status==='collecting'&&!finished&&r.ais.some(a=>(!a.model_profile_id||a.model_profile?.credential_status!=='ok')&&!submitted.has(a.alias))?`<div class="divider"></div><form data-form="ai-answer"><label class="field">录入 AI 原始回答</label><select name="alias">${r.ais.filter(a=>(!a.model_profile_id||a.model_profile?.credential_status!=='ok')&&!submitted.has(a.alias)).map(a=>`<option value="${a.alias}">${a.alias} · ${esc(a.name)}</option>`).join('')}</select><label class="field">AI 回答（复制原文）</label><textarea name="answer" data-draft="ai-answer" maxlength="12000" required placeholder="请粘贴 AI 原样回答，不做修改…">${myDraft('ai-answer')}</textarea><div class="fom-actions"><button class="primary" type="submit">录入 AI 回答</button></div></form>`:''}
 ${r.invite_code?`<div class="notice"><div class="muted tiny">房间邀请码（房主 / 管理员可见）</div><div class="label-line"><strong class="code">${esc(r.invite_code)}</strong><button class="small" data-action="copy-code">复制</button></div></div>`:''}
 ${(r.rounds[0].status==='draft'||r.roster_recovery)&&active&&!finished?`<div class="divider"></div><label class="field">${r.roster_recovery?'补足本轮参赛者（至少 2 人）':'增加 AI 参赛位（最多总计 8 人）'}</label><form data-form="add-ai"><input name="name" maxlength="60" placeholder="AI 来源，如 GPT-6" required>${r.personas_frozen?'<p class="hint">本场配置已冻结；账号删除导致人数不足时，只能补入使用默认人设的手动 AI 席位。</p>':'<label class="field">模型配置</label><select name="model_profile_id">'+modelProfileOptions(r)+'</select>'}<div class="form-actions"><button type="submit">＋ 添加 AI</button></div></form>${r.roster_recovery?'<p class="hint">账号退赛后人数不足，可补齐至 2 人再开启本轮；历史代号和回答保留。</p>':''}`:''}
 ${!r.mine&&(!r.self_withdrawn||!r.match_started)&&(r.rounds[0].status==='draft'||r.roster_recovery)&&!finished?`<button class="small" data-action="admin-join">作为人类玩家加入</button>`:''}
 ${r.mine&&!finished?'<div class="divider"></div><button class="danger" data-action="withdraw">退赛（保留管理权限）</button>':''}
 ${renderAIBindingControls(r)}
 ${active&&!finished&&!locked&&['collecting','revealed'].includes(round.status)&&r.ais.some(a=>submitted.has(a.alias))?`<div class="divider"></div><form data-form="edit-ai-answer"><h3>修订 AI 参赛者回复</h3><p class="hint">仅能修改当前轮、下一轮开始之前的 AI 回复。已公开的修改将实时通知裁判，历史修订保留供审计；已锁定判决时不可修改。</p><label class="field">AI 参赛者</label><select name="alias" data-ai-edit-select>${r.ais.filter(a=>submitted.has(a.alias)).map(a=>`<option value="${esc(a.alias)}">${esc(a.alias)} · ${esc(a.name)}</option>`).join('')}</select><label class="field">修改后的回复</label><textarea name="text" data-ai-edit-text maxlength="12000" required>${esc(r.ai_edits?.[r.ais.find(a=>submitted.has(a.alias))?.alias]||'')}</textarea><div class="form-actions"><button type="submit">保存修订并同步</button></div></form>`:''}
 ${active&&!finished&&!locked&&r.humans.length&&roster.length>2?`<div class="divider"></div><h3>移除人类参赛者</h3><p class="hint">移除后该账号无法重新加入房间；历史已提交回答仍保留。</p>${r.humans.map(h=>`<div class="label-line" style="gap:8px;margin:8px 0"><span>${esc(h.alias)} · ${esc(h.name)}</span><button class="small danger" data-action="remove-human" data-id="${h.user_id}">移出房间</button></div>`).join('')}`:''}
 ${active&&round.status==='revealed'&&!finished&&!locked?`<div class="divider"></div><div class="buttonrow"><button class="primary" data-action="next-round">＋ 开启下一轮</button><button data-action="packet">生成匿名裁判包</button></div>`:''}
 ${r.packets.length?`<div class="divider"></div><details class="packet-panel" data-packet-panel data-key="${r.id}:${r.match_number}" ${state.packetPanels[`${r.id}:${r.match_number}`]?'open':''}><summary><strong>单轮裁判包快照</strong><span class="pill">${r.packets.length} 份</span></summary><div class="packet-panel-body"><p class="hint">每份快照仅包含该轮裁判问题和参赛者答复，生成后内容固定；修订回复后请重新生成快照。</p>${r.packets.slice(0,6).map(p=>`<div class="feed-item" style="margin-top:7px"><div class="label-line"><strong>仅第 ${p.through_round} 轮 · v${p.version}</strong><span class="pill ${p.sent_at?'ok':''}">${p.sent_at?'已确认发送':'待发送'}</span></div><div class="tiny muted">SHA256: ${esc(p.sha256.slice(0,16))}…</div><div class="buttonrow spaced"><button class="small" data-action="view-packet" data-id="${p.id}">查看 / 复制</button>${p.sent_at?'':`<button class="small" data-action="mark-sent" data-id="${p.id}">标记已发送</button>`}</div></div>`).join('')}</div></details>`:''}
 ${!finished&&!(r.judges||[]).length?`<div class="divider"></div><h3>裁判最终判断</h3>${locked?`<p class="locked">🔒 结论已锁定，揭晓后所有玩家都能查看。</p><button class="danger" data-action="reveal">揭晓身份与计分</button>`: (r.rounds.every(x=>x.status==='revealed')?`<form data-form="verdict"><p class="hint">手动录入裁判的最终判断。锁定前不能修改身份判决；请勿提前公布真实身份。</p>${roster.map(p=>`<div class="label-line" style="margin:8px 0"><label for="pred-${p.alias}">参与者 ${p.alias}</label><select required style="max-width:120px" name="pred-${p.alias}" id="pred-${p.alias}"><option value="">选择</option><option value="Human">人类</option><option value="AI">AI</option></select></div>`).join('')}<label class="field">裁判主要依据（必填）</label><textarea name="evidence" data-draft="evidence" required maxlength="5000" placeholder="裁判的判断依据…">${myDraft('evidence')}</textarea><label class="field">置信度（0–100）</label><input type="text" name="confidence" inputmode="numeric" pattern="[0-9]{1,3}" value="50" required><label class="field">最弱环节（可选）</label><textarea name="weakness" data-draft="weakness" maxlength="5000">${myDraft('weakness')}</textarea><label class="field">如果判断错误可能错在哪（可选）</label><textarea name="possible_error" data-draft="possible_error" maxlength="5000">${myDraft('possible_error')}</textarea><div class="form-actions"><button class="primary" type="submit">锁定裁判结论</button></div></form>`:``)}`:''}</section>`:'';
 const scored=r.verdict&&finished?roster.filter(p=>r.verdict.predictions[p.alias]===(p.role==='AI'?'AI':'Human')).length:null;
 app.innerHTML=`<div class="pagehead"><div><div class="overline">ROOM ${r.id} / REAL-TIME MATCH</div><h1>${esc(r.title)}</h1><div class="stat-row"><span class="pill ${finished?'ok':'warn'}">${finished?'比赛已结束':'比赛进行中'}</span><span cass="pill">${humans} 位人类 · ${ais} 位 AI</span><span class="pill">${r.judges?.length?`${r.judges.length} 位裁判（真人 / AI）`:`外部裁判：${esc(r.judge)}`}</span></div></div><div class="buttonrow"><button data-action="personal-center">个人中心</button><button class="ghost" data-action="back">← 比赛大厅</button><button data-action="sync">↻ 刷新</button></div></div>
 ${finishedActions(r)}${renderMyPointsResult(r)}<div class="room-layout ${canManage?'admin-room-layout':''}"><aside class="aside"><section class="card flat"><h2>参赛者身份</h2><div class="roster">${makeRoster(r)}</div></section><section class="card flat"><h2>比赛轮次</h2><div class="round-tags">${rtab}</div></section></aside>
 <div class="stack">${canManage&&state.roomView==='human-judges'?renderHumanJudgeAdminPanel(r):canManage&&state.roomView==='ai-judges'?judgeAdminPanel(r)+renderModelOperationPanel(r):canManage&&state.roomView==='admin'?judgeBox+renderPersonaPreparationControl(r)+renderReadinessPanel(r)+renderJudgeWorkflowPanel(r)+renderModelOperationPanel(r):''}
 <div class="${canManage&&state.roomView!=='match'?'hide':'stack'}"><section class="card"><div class="label-line"><div><div class="overline">JUDGE / ROUND ${n}</div><h2 style="margin:5px 0 0">裁判问题</h2></div><span class="pill ${round.status==='revealed'?'ok':'warn'}">${round.status==='draft'?'等待发布':round.status==='collecting'?'正在收集回答':'本轮已揭晓'}</span></div><div class="question spaced">${esc(round.question||'管理员尚未开启本轮作答。')}</div>${renderRoundTimer(r,round,active)}${round.status==='draft'&&round.questions?.length?`<div class="feed spaced">${round.questions.map(q=>`<div class="feed-item"><strong>${esc(q.judge)}</strong><div class="feed-text">${esc(q.text)}</div></div>`).join('')}</div>`:''}<div class="buttonrow spaced"><button class="small" data-action="download-log">↓ 下载本轮 Log (.md)</button>${finished?'<button class="small" data-action="download-full-log">↓ 下载整场 Log (.md)</button>':''}</div></section>
 <section class="card"><div class="label-line"><h2>参赛者回答</h2><span class="pill">${round.submitted.length}/${roster.length} 已提交</span></div>${answerView}
 ${canAnswer?`<form data-form="answer" class="spaced"><label class="field">我的本轮回答 · ${esc(r.mine)}</label><textarea name="answer" data-draft="answer" maxlength="12000" required placeholder="在这里回答裁判的问题。提交后不可修改，直到全员提交才统一公开。">${myDraft('answer')}</textarea><div class="form-actions"><button class="primary" type="submit">提交本轮回答</button></div></form>`:''}
 ${!r.mine&&!finished?D('在参赛者管理选择以人类身份加入。'):''}</section>
${renderPublicChat(r)}
 ${finished&&r.verdicts?.length?renderMultiResults(r,roster):''}
 ${finished&&!r.verdicts?.length?`<section class="card"><div class="overline">MATCH ACCURACY / 比赛识别统计</div><h2>最终判决与真实身份</h2><div class="score">${scored}/${roster.length}</div><p class="muted">裁判 ${esc(r.judge)} · 置信度 ${r.verdict?.confidence??'?'}%</p>${roster.map(p=>`<div class="answer-card"><strong>${p.alias} · ${esc(p.label)}</strong><div>真实身份：${p.role} / 裁判判断：${r.verdict?.predictions?.[p.alias]==='Human'?'人类':'AI'} ${r.verdict?.predictions?.[p.alias]===(p.role==='AI'?'AI':'Human')?'✓':'✗'}</div></div>`).join('')}<h3 class="spaced">判断依据</h3><div class="question">${esc(r.verdict?.evidence)}</div><p class="hint spaced">最弱环节：${esc(r.verdict?.weakness||'未填写')}</p><p class="hint">可能误判：${esc(r.verdict?.possible_error||'未填写')}</p></section>`:''}
 </div></div><div class="right-panel">${canManage?renderAdminRoomNav(r,finished):`<section class="card flat"><div class="overline">PARTICIPANT</div><h2>等待主持人推进比赛</h2><p class="hint">你的每次提交都会发送到服务器；全员提交完成后自动揭晓。若网络断开，请等待自动重连或手动刷新。</p></section>`}</div></div>`;
 updateTypingBadges();
 updateRoundCountdown();
 if(editingDraft){
   const next=app.querySelector(`[data-draft="${editingDraft}"]`);
   if(next){next.focus();if(editingPos!==null)next.setSelectionRange(editingPos,editingPos)}
 }
}
setInterval(updateRoundCountdown,1000);
async function downloadLog(){const id=state.roomId,n=state.selectedRound;const file=await api(`/api/rooms/${id}/rounds/${n}/log`,{raw:true});const href=URL.createObjectURL(file);const a=document.createElement('a');a.href=href;a.download=`room-${id}-round-${n}.md`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(href),1500)}
async function downloadFullLog(){const id=state.roomId;const file=await api(`/api/rooms/${id}/log`,{raw:true});const href=URL.createObjectURL(file);const a=document.createElement('a');a.href=href;a.download=`room-${id}-full-log.md`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(href),1500)}
app.addEventListener('toggle',e=>{
 if(e.target.matches('details[data-packet-panel]'))state.packetPanels[e.target.dataset.key]=e.target.open;
 if(e.target.matches('details[data-model-config]'))state.personalModelExpanded[Number(e.target.dataset.id)]=e.target.open;
 if(e.target.matches('[data-model-operations-panel]'))state.modelOperationsExpanded=!!e.target.open;
 if(e.target.matches('details[data-model-operation-history]'))state.modelOperationHistoryPanels[e.target.dataset.key]=e.target.open;
},true);
app.addEventListener('input',e=>{
 const dmInput=e.target.closest('form[data-form="dm-send"] textarea[name="text"]');
 if(dmInput){state.dmDraft=dmInput.value;return;}
 const modelForm=e.target.closest('form[data-form="personal-model-edit"]');
 if(modelForm)invalidatePersonalModelTest(Number(modelForm.dataset.id));
 if(e.target.dataset?.chatDraft!==undefined){state.chatDraft=e.target.value;return;}
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
  if(action==='copy-uid'){if(!b.dataset.uid)throw new Error('UID 不可用');await navigator.clipboard.writeText(b.dataset.uid);toast('UID 已复制')}
  if(action==='dm-open')await openDm(b.dataset.uid);
  if(action==='dm-close'){state.dmUid=null;state.dmMessages=[];state.dmBeforeId=null;state.dmHasMore=false;state.dmPending=null;state.dmDraft='';renderDmPanel();}
  if(action==='dm-older')await loadOlderDm(b.dataset.uid);
  if(action==='friend-accept'||action==='friend-reject'||action==='friend-withdraw'){
   const id=Number(b.dataset.id),suffix=action==='friend-accept'?'accept':action==='friend-reject'?'reject':'withdraw';
   await api('/api/friends/requests/'+id+'/'+suffix,{method:'POST'});await loadSocial();renderPersonalCenter();toast('好友申请状态已更新');
  }
  if(action==='friend-delete'){
   const uid=b.dataset.uid;if(confirm('删除好友关系？双方原有私信仍可查看。')){await api('/api/friends/'+encodeURIComponent(uid),{method:'DELETE',body:{confirm:true}});await loadSocial();renderPersonalCenter();}
  }
  if(action==='friend-note'){
   const uid=b.dataset.uid,current=state.social?.friends?.find(x=>x.uid===uid)?.note||'';const note=prompt('设置仅自己可见的好友备注（1～40 字）',current);
   if(note!==null){await api('/api/friends/'+encodeURIComponent(uid)+'/note',{method:'PUT',body:{note}});await loadSocial();renderPersonalCenter();}
  }
  if(action==='friend-reset-note'){
   await api('/api/friends/'+encodeURIComponent(b.dataset.uid)+'/note',{method:'PUT',body:{reset_default:true}});
   await loadSocial();renderPersonalCenter();toast('已恢复为对方当前名字');
  }
  if(action==='points-ledger')await loadPointsLedger({reset:true});
  if(action==='points-ledger-more')await loadPointsLedger();
  if(action==='friend-add'){
   await api('/api/friends/requests',{method:'POST',body:{uid:b.dataset.uid}});await loadSocial();renderPersonalCenter();toast('好友申请已发送');
  }
  if(action==='friend-from-room'){
   const uid=b.dataset.uid,result=await api('/api/users/search?uid='+encodeURIComponent(uid));
   if(result.status==='none'){await api('/api/friends/requests',{method:'POST',body:{uid}});await loadSocial();toast('好友申请已发送');}
   else if(result.status==='friend')toast('已经是好友');
   else if(result.status==='incoming')toast('对方已向你发送好友申请，请在个人中心处理');
   else toast('好友申请已经发送');
  }
  if(action==='personal-center')await openPersonalCenter();
  if(action==='personal-back')await returnFromPersonalCenter();
  if(action==='back')await leaveCurrentRoom();
  if(action==='closed-round-log'){state.selectedRound=Number(b.dataset.round);await downloadLog();}
  if(action==='manage-users')await loadAccounts();
  if(action==='toggle-password-reset'){const form=b.closest('[data-account-uid]').querySelector('form');form.classList.toggle('hide');if(!form.classList.contains('hide'))form.querySelector('input').focus();}
  if(action==='delete-account'){
   const uid=b.dataset.uid;
   if(confirm(`确定删除账号 ${uid}？该账号将立即退出登录且无法再使用，未完赛参与者将退赛；已有比赛历史署名保留。`)){
    const result=await api(`/api/admin/users/${uid}`,{method:'DELETE',body:{confirm:true}});
    if(result.signed_out){state.me=null;state.room=null;state.roomId=null;clearPersonalModelState();renderAuth();}else await loadAccounts();toast('账号已删除');
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
    if(!['match','human-judges','ai-judges','admin'].includes(view)) return;
    if(state.roomView!==view){state.roomView=view;if(view!=='match')state.selectedRound=state.room.current_round;renderRoom();window.scrollTo({top:0,behavior:'instant'});}
   }
   if(action==='sync')await refreshRoom();
  if(action==='reset-persona-preparation'){
   if(confirm('取消本场准备并编辑人设？在途任务和已生成的 AI 裁判问题会作废；真人裁判已提交的问题保留。')){
    await api('/api/rooms/'+state.roomId+'/personas/reset-preparation',{method:'POST',body:{confirm:true}});await refreshRoom();toast('准备已取消，可以修改人设');
   }
  }
  if(action==='generate-ai-questions'){b.disabled=true;b.textContent='模型正在出题…';const result=await api(`/api/rooms/${state.roomId}/ai-judges/questions/generate`,{method:'POST'});await refreshRoom();toast(aiQuestionFeedback(result))}
  if(action==='generate-ai-answers'){b.disabled=true;b.textContent='模型正在回答…';const result=await api(`/api/rooms/${state.roomId}/ai/answers/generate`,{method:'POST'});await refreshRoom();const detail=result.failed?.length?'；失败：'+result.failed.map(x=>x.alias+' '+x.error).join('；'):'';toast(`模型 AI 已提交 ${result.generated||0} 个回答${detail}`)}
  if(action==='generate-ai-verdicts'){b.disabled=true;b.textContent='模型正在判断…';const result=await api(`/api/rooms/${state.roomId}/ai-judges/verdicts/generate`,{method:'POST'});await refreshRoom();const detail=result.failed?.length?'；失败：'+result.failed.map(x=>x.error).join('；'):'';toast(`AI 裁判已锁定 ${result.generated||0} 份最终判断${detail}`)}
  if(action==='focus-manual-question'){
   let form=app.querySelector('#manual-question-form');
   if(!form){state.roomView='admin';renderRoom();form=app.querySelector('#manual-question-form');}
   if(form){form.scrollIntoView({behavior:'smooth',block:'center'});form.querySelector('textarea')?.focus();}
  }
  if(action==='retry-model-operation'){
   const taskId=Number(b.dataset.id);b.disabled=true;b.textContent='正在重试…';
   const result=await api('/api/rooms/'+state.roomId+'/model-operations/'+taskId+'/retry',{method:'POST'});
   await refreshRoom();
   const summary=(result.results||[]).map(x=>(x.label||x.alias||('席位 '+(x.judge_id||'')))+'：'+(x.status==='generated'?'已完成':x.error||x.reason||x.status)).join('；');
   toast('已重试指定席位'+(summary?'；'+summary:''));
  }
  if(action==='model-operations-page'){
   state.modelOperationOffset=Math.max(0,state.modelOperationOffset+(b.dataset.direction==='next'?20:-20));
   await loadModelOperationRecords();renderPersonalCenter();
  }
  if(action==='test-personal-model'){
   const id=Number(b.dataset.id);
   if(state.personalModelSavePending[String(id)])throw new Error('模型配置正在保存，请保存完成后再测试连接。');
   const request=beginPersonalModelTest(id);
   if(!request)throw new Error('模型配置已不存在，请刷新后重试。');
   b.disabled=true;b.textContent='测试中…';
   try{
    const result=await api('/api/me/models/'+id+'/test',{method:'POST'});
    if(!isCurrentPersonalModelTest(id,request))return;
    updateModelTestResult(id,{ok:true,model:result.model,latency_ms:result.latency_ms},request);
    toast('连接测试成功 · '+result.model+' · '+result.latency_ms+' ms');
   }catch(err){
    if(!isCurrentPersonalModelTest(id,request))return;
    updateModelTestResult(id,{ok:false,error:err.message},request);toast(err.message);
   }finally{
    if(isCurrentPersonalModelTest(id,request)){
     delete state.personalModelTestPending[String(id)];
     const currentButton=app.querySelector('[data-action="test-personal-model"][data-id="'+id+'"]');
     if(currentButton){currentButton.disabled=false;currentButton.textContent='测试连接';}
    }
    if(state.me&&app.querySelector('[data-model-operations-panel]')){
     await loadModelOperationRecords();renderPersonalCenter();
    }
   }
   return;
  }
  if(action==='delete-personal-model'){const id=Number(b.dataset.id),draftKey='model:'+id;if(confirm('删除此个人模型配置？所有房间中引用它的 AI 席位会解除模型绑定并转为手动转录；席位、匿名代号和比赛历史都会保留。')){invalidatePersonalModelTest(id);delete state.personalModelExpanded[id];await api('/api/me/models/'+id,{method:'DELETE'});await loadPersonalModels();await loadModelOperationRecords();renderPersonalCenter({skipModelDraftKey:draftKey});toast('个人模型配置已删除，关联席位已解除绑定')}}  if(action==='delete-model'){const id=Number(b.dataset.id);if(confirm('删除此模型配置？关联的 AI 参赛者和 AI 裁判将改为手动转录，比赛记录不会删除。')){await api(`/api/rooms/${state.roomId}/models/${id}`,{method:'DELETE'});await refreshRoom();toast('模型配置已删除')}}
  if(action==='download-log')await downloadLog();
  if(action==='download-full-log')await downloadFullLog();
  if(action==='copy-code'){await navigator.clipboard.writeText(state.room.invite_code);toast('邀请码已复制')}
  if(action==='remove-human'){
   const person=state.room.humans.find(h=>h.user_id===Number(b.dataset.id));
   if(person&&confirm(`确定将 ${person.alias} · ${person.name} 移出房间吗？历史回答会保留，但对方不能重新加入。`)){
    await api(`/api/rooms/${state.roomId}/members/${person.user_id}/remove`,{method:'POST'});await refreshRoom();toast('已移出参赛者');
   }
  }
  if(action==='withdraw'){
   if(confirm('仅退出自己的参赛身份？房间将继续运行，管理权限保留。本场已开赛则不能重新参赛。')){
    await api('/api/rooms/'+state.roomId+'/withdraw',{method:'POST',body:{match_number:state.room.match_number,confirm:true}});
    await refreshRoom();toast('已退赛，仍可管理比赛');
   }
  }
  if(action==='admin-join'){await api('/api/join',{method:'POST',body:{code:state.room.invite_code}});await refreshRoom();toast('已作为玩家加入')}
  if(action==='next-round'){if(confirm('确定创建下一轮？服务器会在后台准备可用 AI 裁判问题，但不会自动开放作答。')){const result=await api(`/api/rooms/${state.roomId}/next`,{method:'POST'});await refreshRoom();toast('第 '+result.number+' 轮已创建；服务端正在准备问题，准备完成后仍需单独开始答题。')}}
  if(action==='start-round'){
   const shouldGenerate=b.dataset.generateQuestions==='true';
   const prompt=shouldGenerate?'提交一次服务端开局任务？服务器会先生成可用 AI 裁判问题，再检查条件、开启作答并自动生成 AI 回答。':'提交一次服务端开局任务？服务器会按当前已提交问题检查条件、开启作答并自动生成 AI 回答。';
   if(confirm(prompt)){
    b.disabled=true;b.textContent='已提交…';
    const key=crypto.randomUUID?crypto.randomUUID():Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');
    const result=await api('/api/rooms/'+state.roomId+'/round-workflows',{
     method:'POST',headers:{'Idempotency-Key':key},
     body:{match_number:state.room.match_number,round_number:state.room.current_round,
      include_ai_questions:shouldGenerate,mode:'start'}
    });
    await refreshRoom();
    toast('服务端开局任务已提交：'+workflowStateText(result.workflow?.state));
   }
  }
  if(action==='finish-round-timeout'){
   if(confirm('答题期限已到。结束本轮后，未提交席位会标记为“超时未提交”，已有回答将统一公开；不会自动开启下一轮或结算积分。')){
    await api('/api/rooms/'+state.roomId+'/rounds/'+Number(b.dataset.round)+'/finish-timeout',{
     method:'POST',body:{match_number:state.room.match_number,confirm:true}
    });
    await refreshRoom();toast('本轮已结束，超时状态和已有回答已公开');
   }
  }
  if(action==='cancel-round-workflow'){
   if(confirm('确定取消本次服务端准备？已发出的第三方模型请求可能仍会产生费用，但迟到结果不会写入比赛。')){
    await api('/api/rooms/'+state.roomId+'/round-workflows/'+Number(b.dataset.id)+'/cancel',{
     method:'POST',body:{confirm:true}
    });
    await refreshRoom();toast('服务端工作流已取消');
   }
  }
  if(action==='resume-round-workflow'){
   const wf=state.room?.round_workflow;
   const unknown=!!wf?.requires_unknown_confirmation;
   const prompt=unknown?'存在结果未知的模型请求。请先核对比赛内容；确认再次请求可能产生费用后继续？':'恢复此服务端工作流？服务器会只补齐尚未完成的席位。';
   if(confirm(prompt)){
    await api('/api/rooms/'+state.roomId+'/round-workflows/'+Number(b.dataset.id)+'/resume',{
     method:'POST',body:{confirm_unknown_attempts:unknown}
    });
    await refreshRoom();toast('已请求恢复服务端工作流');
   }
  }
  if(action==='packet'){await api(`/api/rooms/${state.roomId}/packets`,{method:'POST'});await refreshRoom();toast('已生成匿名裁判包快照')}
  if(action==='view-packet'){const pack=await api(`/api/rooms/${state.roomId}/packets/${b.dataset.id}`);const win=window.open('','_blank');if(win){win.document.title='裁判匿名消息包';const pre=win.document.createElement('pre');pre.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.7 sans-serif;padding:24px';pre.textContent=pack.content;win.document.body.appendChild(pre);}else{await navigator.clipboard.writeText(pack.content);toast('弹窗被阻止，已复制裁判包');}}
  if(action==='mark-sent'){if(confirm('确认已亲自发送此版本到裁判窗口？标记并不代表系统已自动发送。')){await api(`/api/rooms/${state.roomId}/packets/${b.dataset.id}/sent`,{method:'POST'});await refreshRoom();toast('已记录手动发送时间')}}
  if(action==='reveal'){if(confirm('裁判结论锁定后将向全部玩家公开身份和计分。确定结束比赛并揭晓？')){await api(`/api/rooms/${state.roomId}/reveal`,{method:'POST'});await refreshRoom();toast('已结束并揭晓比赛')}}
 }catch(err){toast(err.message)}
});
app.addEventListener('submit',async e=>{const form=e.target.closest('form[data-form]');if(!form)return;e.preventDefault();const type=form.dataset.form;const data=Object.fromEntries(new FormData(form));const btn=form.querySelector('[type=submit]');if(btn)btn.disabled=true;
 try{
  if(type==='friend-search'){
   const result=await api('/api/users/search?uid='+encodeURIComponent(data.uid.trim()));
   const box=$('#friendSearchResult');if(box)box.innerHTML=`<div class="feed-item"><strong>${esc(result.display_name)}</strong> · ${esc(result.uid)}<div class="hint">积分 ${esc(result.total_points)} · 连胜 ${esc(result.current_streak)}</div><div class="buttonrow">${result.status==='none'?'<button class="small primary" data-action="friend-add" data-uid="'+esc(result.uid)+'">发送好友申请</button>':result.status==='incoming'?'对方已向你发送申请，请处理收到的申请':result.status==='outgoing'?'申请已发送': '已经是好友'}</div></div>`;
   return;
  }
  if(type==='dm-send'){
   const uid=form.dataset.uid,textValue=String(data.text||''),accountUid=state.me?.user?.uid;
   const accountCsrf=state.me?.user?.csrf,accountEpoch=state.personalModelEpoch,dmVersion=state.dmLoadVersion;
   const sameSession=()=>state.me?.user?.uid===accountUid&&state.me?.user?.csrf===accountCsrf&&state.personalModelEpoch===accountEpoch;
   const sameConversation=()=>sameSession()&&state.dmUid===uid&&state.dmLoadVersion===dmVersion&&location.hash.startsWith('#profile');
   if(!state.dmPending||state.dmPending.uid!==uid||state.dmPending.text!==textValue){
    const client_message_id=crypto.randomUUID?crypto.randomUUID():Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');
    state.dmPending={uid,text:textValue,client_message_id};
   }
   const pending=state.dmPending;
   let sent;
   try{
    sent=await api('/api/messages/'+encodeURIComponent(uid),{method:'POST',body:{text:pending.text,client_message_id:pending.client_message_id}});
   }catch(error){
    if(!sameConversation())return;
    throw error;
   }
   if(!sameConversation())return;
   if(state.dmPending===pending)state.dmPending=null;
   if(state.dmDraft===pending.text){
    state.dmDraft='';
    const currentInput=$('#dmPanel form[data-form="dm-send"] textarea[name="text"]');
    if(currentInput)currentInput.value='';
   }
   const openedVersion=await openDm(uid);
   if(!sameSession()||state.dmUid!==uid||openedVersion===null||state.dmLoadVersion!==openedVersion)return;
   if(sent.message&&!state.dmMessages.some(message=>Number(message.id)===Number(sent.message.id))){
    state.dmMessages=mergeDmMessages(state.dmMessages,[sent.message]);renderDmPanel();
   }
   return;
  }
  if(type==='login'){await api('/api/login',{method:'POST',body:data});await init();toast('登录成功');return}
  if(type==='register'){const result=await api('/api/register',{method:'POST',body:data});history.replaceState(null,'',location.pathname);await loadLobby();toast(`注册成功，你的 UID 是 ${result.uid}`);return}
  if(type==='join'){const result=await api('/api/join',{method:'POST',body:{code:data.code,role:data.role||'player'}});await openRoom(result.room_id);toast(result.role==='judge'?'已作为真人裁判加入':'已加入比赛');return}
  if(type==='room'){const result=await api('/api/rooms',{method:'POST',body:data});await openRoom(result.room_id);toast('房间创建成功，可以分享邀请码');return}
  if(type==='profile-name'){
   const result=await api('/api/me/profile',{method:'PUT',body:{display_name:data.display_name}});
   state.me.user=result.user;renderPersonalCenter();toast('名字已更新');return;
  }
  if(type==='change-password'){
   if(data.new_password!==data.confirm_password)throw new Error('两次输入的新密码不一致。');
   if(!confirm('修改密码后，当前账号的所有登录会话都会失效。确定继续？'))return;
   const result=await api('/api/me/password',{method:'POST',body:data});
   if(result.signed_out){state.me=null;state.room=null;state.roomId=null;state.personalModels=[];clearPersonalModelState();state.chat=[];state.chatDraft='';state.drafts={};state.personalReturn=null;stopWs();history.replaceState(null,'',location.pathname);renderAuth();toast('密码已修改，请重新登录。');return;}
  }
  if(type==='model-operation-filters'){
    state.modelOperationFilters={profile_id:data.profile_id||'',purpose:data.purpose||'',status:data.status||''};
    state.modelOperationOffset=0;await loadModelOperationRecords();renderPersonalCenter();return;
   }
   if(type==='round-timer'){
    const raw=String(data.seconds||'').trim();
    const seconds=raw===''?null:Number(raw);
    if(seconds!==null&&(!Number.isSafeInteger(seconds)||seconds<30||seconds>3600))throw new Error('单轮限时必须为 30～3600 秒，或留空表示关闭。');
    await api('/api/rooms/'+state.roomId+'/round-timer',{method:'PUT',body:{seconds}});
    await refreshRoom();toast(seconds===null?'单轮限时已关闭':'单轮限时已设置为 '+seconds+' 秒');return;
   }
   if(type==='model-request-limit'){
    const value=String(data.limit||'').trim()===''?null:Number(data.limit);
    if(value!==null&&(!Number.isSafeInteger(value)||value<1||value>1000000))throw new Error('请求上限必须为正整数，或留空表示不限。');
    await api('/api/rooms/'+state.roomId+'/model-request-limit',{method:'PUT',body:{limit:value}});
    await refreshRoom();toast(value===null?'本场模型请求上限已设为不限':'本场模型请求上限已更新为 '+value);return;
   }
   if(type==='personal-model-create'||type==='personal-model-edit'){
   const payload={name:data.name,base_url:data.base_url,model:data.model,api_key:data.api_key||'',temperature:Number(data.temperature),max_tokens:Number(data.max_tokens),timeout_seconds:Number(data.timeout_seconds)};
   const draftKey=personalModelDraftKey(form),profileId=Number(form.dataset.id||0);
   const saveEpoch=state.personalModelEpoch,saveUid=state.me?.user?.uid;
   if(type==='personal-model-edit'){
    invalidatePersonalModelTest(profileId);
    setPersonalModelSavePending(profileId,true);
   }
   try{
    if(type==='personal-model-create')await api('/api/me/models',{method:'POST',body:payload});
    else await api('/api/me/models/'+profileId,{method:'PUT',body:payload});
    if(type==='personal-model-edit'){
     if(state.personalModelEpoch!==saveEpoch||state.me?.user?.uid!==saveUid)return;
     // Invalidate again after the server has committed the new credential. This
     // also kills any request that slipped into the save window.
     invalidatePersonalModelTest(profileId);
    }
    await loadPersonalModels();
    if(type==='personal-model-edit'){
     if(state.personalModelEpoch!==saveEpoch||state.me?.user?.uid!==saveUid)return;
     setPersonalModelSavePending(profileId,false);
    }
    renderPersonalCenter({skipModelDraftKey:draftKey});toast(type==='personal-model-create'?'个人模型配置已保存':'个人模型配置已更新');return;
   }finally{
    if(type==='personal-model-edit'&&state.personalModelEpoch===saveEpoch&&state.me?.user?.uid===saveUid)setPersonalModelSavePending(profileId,false);
   }
  }
  if(type==='reset-password'){
   const uid=form.dataset.uid;
   if(confirm(`确定修改 ${uid} 的密码并使其所有登录会话失效？`)){
    const result=await api(`/api/admin/users/${uid}/password`,{method:'POST',body:{password:data.password,confirm:true}});
    if(result.signed_out){state.me=null;state.room=null;state.roomId=null;renderAuth();}else await loadAccounts();toast('密码已修改，原登录会话已失效');
   }
   return;
  }
  const id=state.roomId;
  if(type==='chat'){
   const requestRoomId=id,requestMatch=state.room?.match_number,requestUid=state.me?.user?.uid,requestCsrf=state.me?.user?.csrf,requestDraft=state.chatDraft;
   await api(`/api/rooms/${requestRoomId}/chat`,{method:'POST',body:{match_number:requestMatch,text:data.text}});
   if(state.roomId!==requestRoomId||state.room?.id!==requestRoomId||state.room?.match_number!==requestMatch||state.room?.status!=='open'||state.me?.user?.uid!==requestUid||state.me?.user?.csrf!==requestCsrf)return;
   await refreshRoom();
   if(state.roomId!==requestRoomId||state.room?.id!==requestRoomId||state.room?.match_number!==requestMatch||state.me?.user?.uid!==requestUid||state.me?.user?.csrf!==requestCsrf)return;
   // The user may have typed another message while the authoritative refresh was pending.
   if(state.room?.status==='open'&&state.chatDraft===requestDraft){state.chatDraft='';const chatInput=$('[data-form="chat"] [name="text"]');if(chatInput)chatInput.value='';}
   toast('消息已发送');return;
  }
  if(type==='seat-persona'){
   const payload={preset_id:data.preset_id,custom_system_prompt:data.custom_system_prompt||'',output_length:data.output_length,history_rounds:Number(data.history_rounds)};
   if(!Number.isSafeInteger(payload.history_rounds)||payload.history_rounds<0||payload.history_rounds>100)throw new Error('出题历史轮数必须为 0～100 的整数');
   if(form.dataset.seatType==='participant')await api('/api/rooms/'+id+'/ai/'+encodeURIComponent(form.dataset.seatKey)+'/persona',{method:'PUT',body:payload});
   else await api('/api/rooms/'+id+'/ai-judges/'+Number(form.dataset.seatKey)+'/persona',{method:'PUT',body:payload});
   await refreshRoom();toast('席位人设已保存');return;
  }
  if(type==='bind-ai-player-model'||type==='bind-ai-judge-model'){
   const keep=data.model_profile_id==='__unchanged__';
   const payload=keep?{keep_existing:true}:{model_profile_id:data.model_profile_id?Number(data.model_profile_id):null};
   if(type==='bind-ai-player-model')await api(`/api/rooms/${id}/ai/${encodeURIComponent(form.dataset.alias)}/model`,{method:'PUT',body:payload});
   else await api(`/api/rooms/${id}/ai-judges/${Number(form.dataset.id)}/model`,{method:'PUT',body:payload});
   await refreshRoom();toast(keep?'保持原模型绑定':'模型绑定已更新');return;
  }
  if(type==='add-ai'){const profileId=data.model_profile_id?Number(data.model_profile_id):null;await api(`/api/rooms/${id}/ai`,{method:'POST',body:{name:data.name,model_profile_id:profileId}});await refreshRoom();toast('AI 参赛位已创建')}
  if(type==='add-ai-judge'){const profileId=data.model_profile_id?Number(data.model_profile_id):null;await api(`/api/rooms/${id}/ai-judges`,{method:'POST',body:{name:data.name,model_profile_id:profileId}});await refreshRoom();toast(profileId?'已添加模型 AI 裁判':'已添加手动 AI 裁判')}
  if(type==='ai-judge-question'){await api(`/api/rooms/${id}/ai-judges/${Number(data.judge_id)}/questions`,{method:'POST',body:{text:data.text}});delete state.drafts[`${id}:${state.room?.match_number}:${state.selectedRound}:ai-judge-question`];await refreshRoom();toast('AI 裁判问题已实时同步')}
  if(type==='ai-judge-verdict'){const predictions={};for(const p of [...state.room.humans,...state.room.ais])predictions[p.alias]=data[`pred-${p.alias}`];if(!Object.values(predictions).every(Boolean))throw new Error('请填写所有匿名参赛者的判断');if(confirm('确认已从对应 AI 裁判获取判决？锁定后不可修改。')){await api(`/api/rooms/${id}/ai-judges/${Number(data.judge_id)}/verdict`,{method:'POST',body:{predictions,evidence:data.evidence,confidence:Number(data.confidence),weakness:data.weakness||'',possible_error:data.possible_error||''}});await refreshRoom();toast('AI 裁判独立判断已锁定')}}
  
  if(type==='judge-question'){await api(`/api/rooms/${id}/judge-questions`,{method:'POST',body:{text:data.text}});delete state.drafts[`${id}:${state.room?.match_number}:${state.selectedRound}:judge-question`];await refreshRoom();toast('裁判问题已发送，等待主持人统一开始作答')}
  if(type==='judge-verdict'){
   const predictions={};for(const alias of state.room.aliases)predictions[alias]=data[`pred-${alias}`];
   if(!Object.values(predictions).every(Boolean))throw new Error('请给全部参赛者做出判断');
   if(confirm('锁定后不能修改，其他裁判不会看到你的判断内容。确定提交？')){await api(`/api/rooms/${id}/judge-verdict`,{method:'POST',body:{predictions,evidence:data.evidence,confidence:Number(data.confidence),weakness:data.weakness||'',possible_error:data.possible_error||''}});await refreshRoom();toast('已独立锁定裁判判断')}
  }
  if(type==='question'){await api(`/api/rooms/${id}/question`,{method:'POST',body:{text:data.question}});delete state.drafts[`${id}:${state.room?.match_number}:${state.selectedRound}:question`];await refreshRoom();toast('裁判问题已同步给所有玩家')}
  if(type==='answer'){await api(`/api/rooms/${id}/answers`,{method:'POST',body:{text:data.answer}});delete state.drafts[`${id}:${state.room?.match_number}:${state.selectedRound}:answer`];await refreshRoom();toast('回答已提交，等待其他玩家')}
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
window.addEventListener('hashchange',async event=>{if(!state.me)return;if(location.hash.startsWith('#profile')){if(!state.personalReturn)state.personalReturn=profileReturnFromHash();await openPersonalCenter({fromHash:true});return;}const m=/^#room=(\d+)$/.exec(location.hash);if(m&&Number(m[1])!==state.roomId)await openRoom(Number(m[1]));else if(!m&&(state.roomId||state.personalReturn?.roomId)){const oldHash=new URL(event.oldURL).hash;try{if(!await leaveCurrentRoom())history.replaceState(null,'',oldHash);}catch(err){history.replaceState(null,'',oldHash);toast(err.message);}}});
setInterval(()=>{if(state.roomId && state.me && (!state.ws||state.ws.readyState!==WebSocket.OPEN))refreshRoom()},15000);
init();


