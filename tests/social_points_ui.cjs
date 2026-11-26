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
  await page.locator(`form[data-form="${form}"] button[type="submit"]`).click();
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
    if (!response.ok) throw new Error(`${method} ${route}: ${response.status} ${JSON.stringify(data)}`);
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
function friendDmButton(page, uid) {
  return page.locator('#socialPanel .feed-item [data-action="dm-open"][data-uid="' + uid + '"]');
}

(async () => {
  const browserOptions = { headless: true };
  if (process.env.PLAYWRIGHT_CHANNEL !== 'default') browserOptions.channel = process.env.PLAYWRIGHT_CHANNEL || 'msedge';
  const browser = await chromium.launch(browserOptions);
  const contextA = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const contextB = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const contextC = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const pageA = await contextA.newPage();
  const pageB = await contextB.newPage();
  const pageC = await contextC.newPage();
  for (const page of [pageA, pageB, pageC]) page.setDefaultTimeout(10000);
  const errors = [];
  const dmRequests = [];
  pageA.on('request', request => {
    if (request.url().includes('/api/messages/')) dmRequests.push({ method: request.method(), url: request.url() });
  });
  for (const page of [pageA, pageB, pageC]) page.on('pageerror', error => errors.push(String(error)));
  try {
    await pageA.goto(site);
    await pageB.goto(site);
    await pageC.goto(site);
    const uidA = await register(pageA, '好友测试甲');
    const uidB = await register(pageB, '好友测试乙');
    const uidC = await register(pageC, '独立裁判丙');
    assert.equal(uidA, 'TL00000002');
    assert.equal(uidB, 'TL00000003');
    assert.equal(uidC, 'TL00000004');

    await pageA.locator('form[data-form="room"] [name="title"]').fill('好友与积分浏览器验收');
    await clickSubmit(pageA, 'room');
    await pageA.waitForFunction(() => /^#room=\d+$/.test(location.hash));
    const roomHash = await pageA.evaluate(() => location.hash);
    const roomId = Number(roomHash.match(/room=(\d+)/)?.[1]);
    assert(roomId > 0);
    const room = await api(pageA, 'GET', `/api/rooms/${roomId}`);
    const base = `/api/rooms/${roomId}`;
    const hostJoin = await api(pageA, 'POST', '/api/join', { code: room.invite_code, role: 'player' });
    const playerJoin = await api(pageB, 'POST', '/api/join', { code: room.invite_code, role: 'player' });
    const judgeJoin = await api(pageC, 'POST', '/api/join', { code: room.invite_code, role: 'judge' });
    assert(hostJoin.alias && playerJoin.alias);
    assert.equal(judgeJoin.role, 'judge');

    await pageA.locator('[data-action="friend-from-room"]').waitFor();
    assert.equal(await pageA.locator('[data-action="personal-center"]').count(), 1);
    assert(await pageA.getByText('参赛者管理', { exact: false }).count() > 0);
    assert.equal(await pageA.getByText('房主管理台', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('人类玩家互相可见真实账号身份；裁判包仅含匿名代号。', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('真人裁判自行注册并使用 UID 登录', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('同一 UID 同时只能担任一个未完赛房间的真人裁判', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('所有真人 / AI 裁判的问题会统一汇总', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('仅本房间的人类参赛者、房主和管理员可见', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('实时同步中', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('仍需完成全部轮次', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('所有轮次已完成', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('所有裁判均已锁定判断', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('全部裁判已锁定', { exact: false }).count(), 0);
    assert.equal(await pageA.getByText('当前独立裁判的题目将在房主开启本轮作答后汇总', { exact: false }).count(), 0);

    await api(pageC, 'POST', base + '/judge-questions', { text: '独立裁判统一问题' }, 1);
    await api(pageA, 'POST', base + '/start', {}, 1);
    const judgeChatStatus = await pageC.evaluate(async roomId => {
      const response = await fetch('/api/rooms/' + roomId + '/chat?match_number=1');
      return response.status;
    }, roomId);
    assert.equal(judgeChatStatus, 403);
    await openPersonalCenter(pageA);
    await openPersonalCenter(pageB);
    await openPersonalCenter(pageC);
    await pageA.waitForTimeout(500);
    await pageB.waitForTimeout(500);
    await pageC.waitForTimeout(500);
    const nameA = pageA.locator('form[data-form="profile-name"] input[name="display_name"]');
    const nameB = pageB.locator('form[data-form="profile-name"] input[name="display_name"]');
    const nameC = pageC.locator('form[data-form="profile-name"] input[name="display_name"]');
    for (const page of [pageA, pageB, pageC]) await visible(page.getByText('比赛进行中，暂不能修改名字'));
    assert.equal(await nameA.isDisabled(), true);
    assert.equal(await nameB.isDisabled(), true);
    assert.equal(await nameC.isDisabled(), true);
    assert.equal(await pageA.locator('form[data-form="profile-name"] button[type="submit"]').isDisabled(), true);

    await pageA.locator('form[data-form="friend-search"] input[name="uid"]').fill(uidB);
    await clickSubmit(pageA, 'friend-search');
    await pageA.locator('#friendSearchResult [data-action="friend-add"]').click();
    await pageB.locator('[data-action="friend-accept"]').waitFor();
    await pageB.locator('[data-action="friend-accept"]').click();
    await pageA.locator(`[data-action="friend-note"][data-uid="${uidB}"]`).waitFor();
    pageA.once('dialog', dialog => dialog.accept('仅我可见的备注'));
    await pageA.locator(`[data-action="friend-note"][data-uid="${uidB}"]`).click();
    await visible(pageA.getByText('仅我可见的备注').first());
    await pageA.locator(`[data-action="friend-reset-note"][data-uid="${uidB}"]`).click();
    await visible(pageA.locator('#toast').filter({ hasText: '已恢复为对方当前名字' }));
    await visible(pageA.getByText('好友测试乙').first());

    await pageC.locator('form[data-form="friend-search"] input[name="uid"]').fill(uidB);
    await clickSubmit(pageC, 'friend-search');
    await pageC.locator('#friendSearchResult [data-action="friend-add"]').click();
    await pageB.locator('[data-action="friend-accept"]').waitFor();
    await pageB.locator('[data-action="friend-accept"]').first().click();
    await friendDmButton(pageC, uidB).waitFor();

    await closePersonalCenter(pageA);
    await closePersonalCenter(pageB);
    await pageA.waitForFunction(() => state.dmSocket === null);
    await pageB.waitForFunction(() => state.dmSocket === null);

    for (let index = 1; index <= 51; index++) {
      const sequence = String(index).padStart(2, '0');
      await api(pageB, 'POST', '/api/messages/' + encodeURIComponent(uidA), {
        text: '分页消息 ' + sequence,
        client_message_id: 'social-points-pagination-' + sequence,
      });
    }

    await openPersonalCenter(pageA);
    await friendDmButton(pageA, uidB).click();
    const dmFormA = pageA.locator('form[data-form="dm-send"]');
    await visible(dmFormA);
    await pageA.waitForFunction(() => state.dmMessages.length === 50 && state.dmHasMore);
    await pageA.locator('[data-action="dm-older"]').click();
    await pageA.waitForFunction(() => state.dmMessages.length === 51 && !state.dmHasMore);
    assert.equal(await pageA.evaluate(() => state.dmMessages[0]?.text), '分页消息 01');
    assert.equal(await pageA.evaluate(() => state.dmMessages.at(-1)?.text), '分页消息 51');

    const passwordFormA = pageA.locator('form[data-form="change-password"]');
    await passwordFormA.locator('[name="current_password"]').fill('current-password-probe');
    await passwordFormA.locator('[name="new_password"]').fill('new-password-probe');
    await passwordFormA.locator('[name="confirm_password"]').fill('confirm-password-probe');
    const messageText = '<img src=x onerror=alert(1)>私信纯文本';
    await dmFormA.locator('textarea[name="text"]').fill(messageText);
    await passwordFormA.locator('[name="new_password"]').focus();
    const incomingDraftProbe = '来自乙的实时同步测试';
    await api(pageB, 'POST', '/api/messages/' + encodeURIComponent(uidA), {
      text: incomingDraftProbe,
      client_message_id: 'social-points-browser-draft-probe',
    });
    await pageA.waitForFunction(({ uid, draft, incoming }) =>
      state.dmUid === uid &&
      state.dmMessages.some(message => message.text === incoming) &&
      document.querySelector('form[data-form="dm-send"] textarea[name="text"]')?.value === draft,
      { uid: uidB, draft: messageText, incoming: incomingDraftProbe });
    assert.equal(await passwordFormA.locator('[name="current_password"]').inputValue(), 'current-password-probe');
    assert.equal(await passwordFormA.locator('[name="new_password"]').inputValue(), 'new-password-probe');
    assert.equal(await passwordFormA.locator('[name="confirm_password"]').inputValue(), 'confirm-password-probe');
    assert.equal(await pageA.evaluate(() => document.activeElement?.getAttribute('name')), 'new_password');
    await pageA.evaluate(() => {
      window.__dmSubmitEvents = [];
      document.querySelector('#app')?.addEventListener('submit', event => {
        const form = event.target.closest('form[data-form]');
        if (form?.dataset.form === 'dm-send') {
          window.__dmSubmitEvents.push({
            uid: form.dataset.uid,
            textLength: form.querySelector('textarea[name="text"]')?.value.length,
            defaultPrevented: event.defaultPrevented,
          });
        }
      }, true);
    });
    const sendResponsePromise = pageA.waitForResponse(response =>
      response.url().endsWith('/api/messages/' + encodeURIComponent(uidB)) && response.request().method() === 'POST');
    await dmFormA.locator('button[type="submit"]').click();
    let sendResponse;
    try {
      sendResponse = await sendResponsePromise;
    } catch (error) {
      const debug = await pageA.evaluate(() => ({
        dmUid: state.dmUid,
        dmPending: state.dmPending,
        dmMessages: state.dmMessages,
        toast: document.querySelector('#toast')?.textContent || '',
        submitEvents: window.__dmSubmitEvents || [],
        formCount: document.querySelectorAll('form[data-form="dm-send"]').length,
        form: document.querySelector('form[data-form="dm-send"]')?.outerHTML || null,
        buttonDisabled: document.querySelector('form[data-form="dm-send"] button[type="submit"]')?.disabled ?? null,
        textareaValue: document.querySelector('form[data-form="dm-send"] textarea[name="text"]')?.value ?? null,
        cryptoType: typeof crypto,
        randomUUIDType: typeof crypto?.randomUUID,
      }));
      throw new Error('DM send request missing: ' + JSON.stringify({ debug, dmRequests, pageErrors: errors, cause: error.message }));
    }
    const sendResult = await sendResponse.json();
    assert.equal(sendResponse.status(), 200, JSON.stringify(sendResult));
    const persistedMessages = await api(pageA, 'GET', '/api/messages/' + encodeURIComponent(uidB));
    assert(persistedMessages.messages.some(message => message.text.includes('私信纯文本')), JSON.stringify(persistedMessages));
    await pageA.waitForFunction(uid => state.dmUid === uid && state.dmMessages.some(message => message.text.includes('私信纯文本')), uidB).catch(async error => {
      const debug = await pageA.evaluate(() => ({
        dmUid: state.dmUid,
        dmMessages: state.dmMessages,
        dmLoadVersion: state.dmLoadVersion,
        dmPending: state.dmPending,
        dmSocketState: state.dmSocket?.readyState,
        toast: document.querySelector('#toast')?.textContent || '',
        conversations: state.social?.conversations || [],
      }));
      throw new Error('DM client state did not update: ' + JSON.stringify(debug) + '; ' + error.message);
    });
    await pageA.locator('.dm-message.mine').filter({ hasText: '私信纯文本' }).waitFor();
    assert.equal(await pageA.locator('#dmPanel img').count(), 0);

    await openPersonalCenter(pageB);
    await friendDmButton(pageB, uidA).click();
    await pageB.locator('.dm-message:not(.mine)').filter({ hasText: '私信纯文本' }).waitFor();
    assert.equal(await pageB.locator('#dmPanel img').count(), 0);

    await api(pageA, 'POST', base + '/answers', { text: '房主回答' }, 1);
    await api(pageB, 'POST', base + '/answers', { text: '玩家回答' }, 1);
    await api(pageC, 'POST', base + '/judge-verdict', {
      predictions: { [hostJoin.alias]: 'AI', [playerJoin.alias]: 'Human' },
      evidence: '浏览器验收独立裁判判决',
      confidence: 75,
    }, 1);
    await api(pageA, 'POST', base + '/reveal', { match_number: 1 }, 1);
    await waitEnabled(pageA, nameA);
    await waitEnabled(pageB, nameB);
    await waitEnabled(pageC, nameC);
    for (const page of [pageA, pageB, pageC]) {
      assert.equal(await page.getByText('比赛进行中，暂不能修改名字').count(), 0);
    }

    await closePersonalCenter(pageA);
    const judgeNotice = pageA.locator('#judgeNotice');
    await judgeNotice.waitFor({ state: 'visible' });
    await pageA.locator('#judgeNoticeClose').click();
    await judgeNotice.waitFor({ state: 'hidden' });
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
    const themeButton = pageA.locator('#themeBtn');
    await themeButton.click();
    assert.equal(await pageA.locator('body').getAttribute('data-theme'), 'dark');
    assert(await pageA.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await pageA.screenshot({ path: path.join(artifacts, 'social-points-mobile-dark.png'), fullPage: true, animations: 'disabled' });
    await themeButton.click();
    assert.equal(await pageA.locator('body').getAttribute('data-theme'), 'light');
    await pageA.screenshot({ path: path.join(artifacts, 'social-points-mobile.png'), fullPage: true, animations: 'disabled' });
    await pageB.screenshot({ path: path.join(artifacts, 'social-points-result-desktop.png'), fullPage: true, animations: 'disabled' });
    await pageC.screenshot({ path: path.join(artifacts, 'social-points-independent-judge.png'), fullPage: true, animations: 'disabled' });

    // Reopen A-B before delaying its send response, then switch this browser to C and open C-B.
    await friendDmButton(pageA, uidB).click();
    const delayedDmForm = pageA.locator('form[data-form="dm-send"]');
    await visible(delayedDmForm);
    const delayedText = 'A 账号切换后的迟到私信';
    let releaseDelayedResponse, signalDelayedResponseReady;
    const delayedGate = new Promise(resolve => { releaseDelayedResponse = resolve; });
    const delayedReady = new Promise(resolve => { signalDelayedResponseReady = resolve; });
    const delayedResponsePromise = pageA.waitForResponse(response =>
      response.url().endsWith('/api/messages/' + encodeURIComponent(uidB)) &&
      response.request().method() === 'POST');
    await pageA.route('**/api/messages/' + uidB, async route => {
      if (route.request().method() !== 'POST') { await route.continue(); return; }
      const response = await route.fetch();
      signalDelayedResponseReady();
      await delayedGate;
      await route.fulfill({ response });
    });
    await delayedDmForm.locator('textarea[name="text"]').fill(delayedText);
    await delayedDmForm.locator('button[type="submit"]').click();
    await delayedReady;
    await pageA.locator('#logoutBtn').click();
    await pageA.locator('form[data-form="login"]').waitFor();
    await pageA.locator('form[data-form="login"] [name="uid"]').fill(uidC);
    await pageA.locator('form[data-form="login"] [name="password"]').fill('browser-password');
    await clickSubmit(pageA, 'login');
    await pageA.waitForFunction(uid => state.me?.user?.uid === uid, uidC);
    await openPersonalCenter(pageA);
    await friendDmButton(pageA, uidB).click();
    await pageA.waitForFunction(uid => state.dmUid === uid, uidB);
    assert.equal(await pageA.locator('.dm-message').filter({ hasText: delayedText }).count(), 0);
    releaseDelayedResponse();
    const delayedResponse = await delayedResponsePromise;
    assert.equal(delayedResponse.status(), 200);
    await pageA.waitForTimeout(250);
    assert.equal(await pageA.evaluate(text => state.dmMessages.some(message => message.text === text), delayedText), false);
    assert.equal(await pageA.locator('.dm-message').filter({ hasText: delayedText }).count(), 0);
    assert.equal(await pageA.evaluate(() => state.me?.user?.uid), uidC);
    await pageA.unroute('**/api/messages/' + uidB);
    assert.deepEqual(errors, []);
  } finally {
    await contextA.close();
    await contextB.close();
    await contextC.close();
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
