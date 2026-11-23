const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { chromium } = require('playwright-core');
const site = process.argv[2];
const artifacts = path.resolve(__dirname, '../artifacts');
fs.mkdirSync(artifacts, { recursive: true });
async function mutate(context, route, data, method = 'POST') {
  const me = await (await context.request.get(site + '/api/me')).json();
  const response = await context.request.fetch(site + route, { method, data,
    headers: me.user ? { 'X-CSRF-Token': me.user.csrf } : {} });
  assert.equal(response.status(), 200, await response.text());
  return response.json();
}
async function submit(page, form, fields) {
  const node = page.locator(`form[data-form="${form}"]`);
  for (const [name, value] of Object.entries(fields)) {
    const input = node.locator(`[name="${name}"]`);
    if (await input.evaluate(el => el.tagName === 'SELECT')) await input.selectOption(value);
    else await input.fill(value);
  }
  await node.locator('[type="submit"]').click();
}
async function screenshot(page, name) {
  await page.screenshot({ path: path.join(artifacts, name), fullPage: true, animations: 'disabled' });
}
(async () => {
  const browserOptions = { headless: true };\n  if (process.env.PLAYWRIGHT_CHANNEL !== 'default') browserOptions.channel = process.env.PLAYWRIGHT_CHANNEL || 'msedge';\n  const browser = await chromium.launch(browserOptions);
  const hostContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await hostContext.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  page.on('dialog', dialog => dialog.accept());
  try {
    await page.goto(site);
    await page.locator('[data-action="tab-register"]').click();
    assert.equal(await page.locator('#authUsername').getAttribute('placeholder'), '请输入用户名。');
    assert.equal(await page.getByText('注册后平台会分配唯一 UID。', { exact: true }).count(), 1);
    await submit(page, 'register', { username: '普通房主', password: 'host-password' });
    await page.locator('form[data-form="room"]').waitFor();
    assert.equal(await page.getByText('请输入房主分享的房间邀请码（不是网站密码）。', { exact: true }).count(), 0);
    await submit(page, 'room', { title: '房间重复使用验收' });
    await page.locator('[data-action="room-view"][data-view="admin"]').click();
    await page.getByRole('heading', { name: '房主管理台', exact: true }).waitFor();
    await submit(page, 'add-ai', { name: '手工AI' });
    const info = await (await hostContext.request.get(site + '/api/me')).json();
    const rid = info.rooms[0].id;
    const base = `/api/rooms/${rid}`;
    const room = await (await hostContext.request.get(site + base)).json();
    assert.equal(room.is_admin, false);
    assert.equal(room.can_manage, true);
    const playerContext = await browser.newContext();
    await mutate(playerContext, '/api/register', { username: '参赛玩家', password: 'player-password' });
    await mutate(playerContext, '/api/join', { code: room.invite_code });
    await page.locator('[data-action="sync"]').click();
    await submit(page, 'question', { question: '第一场的问题' });
    await mutate(playerContext, base + '/answers', { text: '第一场的回答' });
    await submit(page, 'ai-answer', { answer: '第一场AI回答' });
    await page.locator('#judgeNotice[open]').waitFor();
    await page.locator('#judgeNoticeClose').click();
    await submit(page, 'verdict', { 'pred-A': 'AI', 'pred-B': 'Human', evidence: '判断依据' });
    await page.locator('[data-action="reveal"]').click();
    await page.locator('[data-action="restart-match"]').waitFor();
    await screenshot(page, 'host-finished-desktop.png');
    await page.locator('[data-action="room-view"][data-view="match"]').click();
    assert(await page.getByText('第一场的回答', { exact: true }).isVisible());
    await page.locator('[data-action="restart-match"]').click();
    await page.locator('[data-action="room-view"][data-view="admin"]').click();
    await page.locator('form[data-form="question"]').waitFor();
    assert.equal(await page.locator('form[data-form="question"] textarea').inputValue(), '');
    assert.equal(await page.locator('#judgeNotice').getAttribute('open'), null);
    await submit(page, 'question', { question: '第二场的新问题' });
    await mutate(playerContext, base + '/answers', { text: '第二场人类回答' });
    await submit(page, 'ai-answer', { answer: '第二场AI回答' });
    // Round 1 completion reminder must appear again in match 2.
    await page.locator('#judgeNotice[open]').waitFor();
    await page.locator('#judgeNoticeClose').click();
    await submit(page, 'verdict', { 'pred-A': 'AI', 'pred-B': 'Human', evidence: '第二场依据' });
    await page.locator('[data-action="reveal"]').click();
    await page.locator('[data-action="restart-match"]').waitFor();
    const strangerContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
    await mutate(strangerContext, '/api/register', { username: '不在该房间', password: 'stranger-password' });
    const stranger = await strangerContext.newPage();
    stranger.on('pageerror', error => errors.push(String(error)));
    await stranger.goto(site);
    const erase = stranger.locator(`[data-action="delete-records"][data-id="${rid}"]`);
    await erase.waitFor();
    assert(await stranger.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await screenshot(stranger, 'completed-rooms-mobile.png');
    stranger.once('dialog', dialog => dialog.dismiss());
    await erase.click();
    assert.equal((await (await hostContext.request.get(site + base)).json()).records_deleted, false);
    stranger.once('dialog', dialog => dialog.accept());
    await erase.click();
    await stranger.locator(`[data-action="delete-records"][data-id="${rid}"]`).waitFor({state:'detached'});
    assert.equal(await stranger.getByText('记录已清空', {exact:false}).count(),0);
    await page.getByRole('heading', { name: '开始下一场比赛', exact: true }).waitFor();
    assert.equal(await page.getByText('第二场人类回答', { exact: true }).count(), 0);
    await screenshot(page, 'host-records-cleared.png');
    await page.locator('[data-action="restart-match"]').click();
    await page.locator('[data-action="room-view"][data-view="admin"]').click();
    await page.locator('form[data-form="question"]').waitFor();
    // Retained question remains startable if its only judge account is deleted.
    const formerJudgeContext = await browser.newContext();
    const formerJudge = await mutate(formerJudgeContext, '/api/register', { username: '临时裁判', password: 'judge-password' });
    await mutate(hostContext, base + '/judges/assign', { uid: formerJudge.uid, label: '留题裁判' });
    await mutate(formerJudgeContext, base + '/judge-questions', { text: '已删裁判保留的问题' });
    const adminContext = await browser.newContext();
    await mutate(adminContext, '/api/login', { uid: 'TL00000001', password: 'browser-password' });
    await mutate(adminContext, `/api/admin/users/${formerJudge.uid}`, { confirm: true }, 'DELETE');
    await page.locator('[data-action="room-view"][data-view="judges"]').click();
    await page.locator('[data-action="start-round"]').waitFor();
    await page.locator('[data-action="start-round"]').click();
    await page.locator('[data-action="room-view"][data-view="match"]').click();
    await page.locator('.question').filter({ hasText: '已删裁判保留的问题' }).waitFor();
    // Admin account controls and access to rooms owned by others.
    await page.locator('#logoutBtn').click();
    await page.locator('[data-action="toggle-legacy-login"]').click();
    await submit(page, 'login', { username: 'browser_admin', password: 'browser-password' });
    await page.locator('[data-action="manage-users"]').click();
    const row = page.locator('[data-account-uid="TL00000003"]');
    await row.locator('[data-action="toggle-password-reset"]').click();
    await row.locator('[name="password"]').fill('changed-password');
    await row.locator('[type="submit"]').click();
    await page.locator('#toast').filter({ hasText: '密码已修改' }).waitFor();
    assert.equal((await playerContext.request.get(site + '/api/me')).status(), 401);
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await screenshot(page, 'admin-accounts-mobile.png');
    await row.locator('[data-action="delete-account"]').click();
    await row.waitFor({ state: 'detached' });
    assert.equal((await playerContext.request.post(site + '/api/login', {
      data: { uid: 'TL00000003', password: 'changed-password' } })).status(), 401);
    await page.locator('[data-action="back"]').click();
    await page.locator(`[data-action="open"][data-id="${rid}"]`).click();
    await page.locator('[data-action="room-view"][data-view="admin"]').click();
    await page.getByRole('heading', { name: '房主管理台', exact: true }).waitFor();
    assert.deepEqual(errors, []);
    console.log('Browser acceptance passed: ordinary hosts, reuse, global record deletion, admin accounts');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
