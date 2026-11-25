// Real-browser acceptance for name locking, friend workflows, DMs and account points.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright-core');
const [site] = process.argv.slice(2);
const artifacts = path.resolve(__dirname, '../artifacts');
fs.mkdirSync(artifacts, { recursive: true });

async function visible(locator) { await locator.waitFor({ state: 'visible' }); }
async function waitEnabled(page, locator) {
  for (let attempt = 0; attempt < 80; attempt++) {
    if (await locator.isEnabled()) return;
    await page.waitForTimeout(100);
  }
  throw new Error('Expected control to become enabled after match completion');
}
async function clickSubmit(page, form) {
  await page.locator(\`form[data-form="\${form}"] button[type="submit"]\`).click();
}
async function register(page, username) {
  await page.getByRole('button', { name: '注册账号', exact: true }).click();
  await page.locator('[name="username"]').fill(username);
  await page.locator('[name="password"]').fill('browser-password');
  await clickSubmit(page, 'register');
  await visible(page.locator('#accountUid'));
  return (await page.locator('#accountUid').textContent()).trim();
}
async function api(page, method, route, body = {}, match = null) {
  return page.evaluate(async ({ method, route, body, match }) => {
    const meResponse = await fetch('/api/me');
    const me = await meResponse.json();
    const headers = {};
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      headers['X-CSRF-Token'] = me.user.csrf;
      if (match !== null) headers['X-Match-Number'] = String(match);
    }
    const response = await fetch(route, {
      method,
      headers,
      body: method === 'GET' ? undefined : JSON.stringify(body ?? {}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(\`\${method} \${route}: \${response.status} \${JSON.stringify(data)}\`);
    return data;
  }, { method, route, body, match });
}
async function openPersonalCenter(page) {
  await page.locator('[data-action="personal-center"]').click();
  await visible(page.locator('form[data-form="profile-name"]'));
}
async function closePersonalCenter(page) {
  await page.locator('[data-action="personal-back"]').click();
}

(async () => {
  const browserOptions = { headless: true };
  if (process.env.PLAYWRIGHT_CHANNEL !== 'default') browserOptions.channel = process.env.PLAYWRIGHT_CHANNEL || 'msedge';
  const browser = await chromium.launch(browserOptions);
  const contextA = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const contextB = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const pageA = await contextA.newPage();
  const pageB = await contextB.newPage();
  for (const page of [pageA, pageB]) page.setDefaultTimeout(10000);
  const errors = [];
  pageA.on('pageerror', error => errors.push(String(error)));
  pageB.on('pageerror', error => errors.push(String(error)));
  try {
    await pageA.goto(site);
    await pageB.goto(site);
    const uidA = await register(pageA, '好友测试甲');
    const uidB = await register(pageB, '好友测试乙');
    assert.equal(uidA, 'TL00000002');
    assert.equal(uidB, 'TL00000003');

    await pageA.locator('form[data-form="room"] [name="title"]').fill('好友与积分浏览器验收');
    await clickSubmit(pageA, 'room');
    const roomHash = await pageA.evaluate(() => location.hash);
    const roomId = Number(roomHash.match(/room=(\d+)/)?.[1]);
    assert(roomId > 0);
    const room = await api(pageA, 'GET', \`/api/rooms/\${roomId}\`);
    const base = \`/api/rooms/\${roomId}\`;
    const hostJoin = await api(pageA, 'POST', '/api/join', { code: room.invite_code, role: 'player' });
    const playerJoin = await api(pageB, 'POST', '/api/join', { code: room.invite_code, role: 'player' });
    assert(hostJoin.alias && playerJoin.alias);

    await pageA.locator('[data-action="friend-from-room"]').waitFor();
    assert.equal(await pageA.locator('[data-action="personal-center"]').count(), 1);
    assert(await pageA.getByText('参赛者管理', { exact: false }).count() > 0);
    assert.equal(await pageA.getByText('房主管理台', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('仅本房间的人类参赛者、房主和管理员可见', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('实时同步中', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('所有轮次回答完成之后才可锁定裁判最终判断', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('当前独立裁判的题目将在房主开启本轮作答后汇总', { exact: false }).count(), 0);

    await api(pageA, 'POST', base + '/question', { text: '统一作答开始' }, 1);
    await openPersonalCenter(pageA);
    await openPersonalCenter(pageB);
    await pageA.waitForTimeout(500);
    await pageB.waitForTimeout(500);
    const nameA = pageA.locator('form[data-form="profile-name"] input[name="display_name"]');
    const nameB = pageB.locator('form[data-form="profile-name"] input[name="display_name"]');
    await visible(pageA.getByText('比赛进行中，暂不能修改名字'));
    await visible(pageB.getByText('比赛进行中，暂不能修改名字'));
    assert.equal(await nameA.isDisabled(), true);
    assert.equal(await nameB.isDisabled(), true);
    assert.equal(await pageA.locator('form[data-form="profile-name"] button[type="submit"]').isDisabled(), true);

    await pageA.locator('form[data-form="friend-search"] input[name="uid"]').fill(uidB);
    await clickSubmit(pageA, 'friend-search');
    await pageA.locator('#friendSearchResult [data-action="friend-add"]').click();
    await pageB.locator('[data-action="friend-accept"]').waitFor();
    await pageB.locator('[data-action="friend-accept"]').click();
    await pageA.locator(\`[data-action="friend-note"][data-uid="\${uidB}"]\`).waitFor();
    pageA.once('dialog', dialog => dialog.accept('仅我可见的备注'));
    await pageA.locator(\`[data-action="friend-note"][data-uid="\${uidB}"]\`).click();
    await visible(pageA.getByText('仅我可见的备注'));
    await pageA.locator(\`[data-action="friend-reset-note"][data-uid="\${uidB}"]\`).click();
    await visible(pageA.getByText('好友测试乙'));

    await pageA.locator(\`[data-action="dm-open"][data-uid="\${uidB}"]\`).first().click();
    const dmFormA = pageA.locator('form[data-form="dm-send"]');
    await visible(dmFormA);
    const messageText = '<img src=x onerror=alert(1)>私信纯文本';
    await dmFormA.locator('textarea[name="text"]').fill(messageText);
    await dmFormA.locator('button[type="submit"]').click();
    await pageA.locator('.dm-message.mine').filter({ hasText: '私信纯文本' }).waitFor();
    assert.equal(await pageA.locator('#dmPanel img').count(), 0);

    await pageB.locator(\`[data-action="dm-open"][data-uid="\${uidA}"]\`).first().click();
    await pageB.locator('.dm-message:not(.mine)').filter({ hasText: '私信纯文本' }).waitFor();
    assert.equal(await pageB.locator('#dmPanel img').count(), 0);

    await api(pageA, 'POST', base + '/answers', { text: '房主回答' }, 1);
    await api(pageB, 'POST', base + '/answers', { text: '玩家回答' }, 1);
    await api(pageA, 'POST', base + '/verdict', {
      predictions: { [hostJoin.alias]: 'AI', [playerJoin.alias]: 'Human' },
      evidence: '浏览器验收判决',
      confidence: 75,
    }, 1);
    await api(pageA, 'POST', base + '/reveal', { match_number: 1 }, 1);
    await waitEnabled(pageA, nameA);
    await waitEnabled(pageB, nameB);
    assert.equal(await pageA.getByText('比赛进行中，暂不能修改名字').count(), 0);
    assert.equal(await pageB.getByText('比赛进行中，暂不能修改名字').count(), 0);

    await closePersonalCenter(pageA);
    await pageA.locator('[data-action="friend-from-room"]').click();
    await pageA.getByText('已经是好友', { exact: false }).waitFor();
    await pageA.locator('.account-points-result').waitFor();
    assert.equal(await pageA.locator('.account-points-result').getByText('+10 分').count(), 1);
    assert.equal(await pageA.locator('.account-points-result').getByText('更新后总积分').count(), 1);
    await pageA.getByText('MATCH ACCURACY / 比赛识别统计').waitFor();

    await pageB.goto(site + '/#room=' + roomId);
    await pageB.locator('.account-points-result').waitFor();
    assert.equal(await pageB.locator('.account-points-result').getByText('-10 分').count(), 1);

    await pageA.setViewportSize({ width: 390, height: 844 });
    await pageA.getByRole('button', { name: '个人中心', exact: true }).click();
    await visible(pageA.locator('form[data-form="friend-search"]'));
    assert(await pageA.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await pageA.screenshot({ path: path.join(artifacts, 'social-points-mobile.png'), fullPage: true, animations: 'disabled' });
    await pageB.screenshot({ path: path.join(artifacts, 'social-points-result-desktop.png'), fullPage: true, animations: 'disabled' });

    assert.deepEqual(errors, []);
  } finally {
    await contextA.close();
    await contextB.close();
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
