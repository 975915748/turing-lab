const assert=require('node:assert/strict');
const {chromium}=require('playwright-core');
const site=process.argv[2];
async function api(c,path,body,match=1){
 const me=await (await c.request.get(site+'/api/me')).json();
 const r=await c.request.post(site+path,{data:body||{},headers:me.user?{'X-CSRF-Token':me.user.csrf,'X-Match-Number':String(match)}:{}});
 assert.equal(r.status(),200,await r.text());return r.json();
}
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL==='default'?undefined:(process.env.PLAYWRIGHT_CHANNEL||'msedge')});
 try{
  const host=await browser.newContext(),player=await browser.newContext(),third=await browser.newContext();
  for(const [c,name] of [[host,'房主'],[player,'玩家'],[third,'留在房间的玩家']])await api(c,'/api/register',{username:name,password:'browser-password'});
  const room=await api(host,'/api/rooms',{title:'退出房间验收'}),base='/api/rooms/'+room.room_id;
  for(const c of [host,player,third])await api(c,'/api/join',{code:room.code});
  const hp=await host.newPage(),pp=await player.newPage(),tp=await third.newPage();
  let cancelHost=false;
  for(const p of [hp,pp,tp]){p.setDefaultTimeout(10000);p.on('dialog',d=>p===hp&&cancelHost?d.dismiss():d.accept());await p.goto(site+'/#room='+room.room_id);}
  await hp.locator('[data-action=room-view][data-view=admin]').click();
  await hp.locator('[data-action=withdraw]').click();
  await hp.waitForFunction(()=>state.room?.mine===null);
  await hp.locator('[data-action=admin-join]').click();
  await hp.waitForFunction(()=>!!state.room?.mine);
  cancelHost=true;
  await hp.evaluate(()=>location.hash='');
  await hp.waitForFunction(()=>location.hash.startsWith('#room='));
  assert.equal((await (await host.request.get(site+base)).json()).status,'open');
  cancelHost=false;
  await api(host,base+'/question',{text:'本场保留的问题'});
  await hp.reload();
  await hp.locator('[data-action=room-view][data-view=admin]').click();
  await hp.locator('[data-action=withdraw]').click();
  await hp.waitForFunction(()=>state.room?.mine===null);
  assert.equal(await hp.evaluate(()=>state.room.can_manage),true);
  await pp.locator('[data-action=back]').click();
  await pp.waitForFunction(()=>state.roomId===null&&location.hash==='');
  await pp.locator('[data-action=personal-center]').click();
  await pp.locator('form[data-form=profile-name]').waitFor();
  assert.equal(await pp.locator('input[name=display_name]').isEnabled(),true);
  await pp.locator('input[name=display_name]').fill('退房后新名字');
  await pp.locator('form[data-form=profile-name] button[type=submit]').click();
  await pp.waitForFunction(()=>state.me.user.display_name==='退房后新名字');
  // A profile refresh must retain the room context for a real hash-to-lobby exit.
  await hp.locator('[data-action=personal-center]').click();
  await hp.reload();
  await hp.locator('form[data-form=profile-name]').waitFor();
  await hp.evaluate(()=>location.hash='');
  await hp.waitForFunction(()=>state.roomId===null&&location.hash==='');
  await tp.waitForFunction(()=>state.roomId===null&&location.hash==='');
  await hp.locator('[data-action=open][data-id="'+room.room_id+'"]').click();
  await hp.getByRole('heading',{name:'房间已关闭',exact:true}).waitFor();
  assert.equal(await hp.locator('form[data-form=answer]').count(),0);
  assert.equal(await hp.locator('[data-action=withdraw]').count(),0);
  await hp.screenshot({path:'artifacts/room-exit-desktop.png',fullPage:true});
  await hp.setViewportSize({width:390,height:844});
  await hp.screenshot({path:'artifacts/room-exit-mobile.png',fullPage:true});
  assert.equal(await hp.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.equal(await hp.locator('[data-action=download-full-log]').count(),1);
  await hp.locator('[data-action=restart-match]').click();
  await hp.waitForFunction(()=>state.room?.match_number===2&&state.room.status==='open');
  console.log('Owner withdraw, player exit/rename, owner close, real-time lobby redirect, readonly history, restart, mobile: passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

