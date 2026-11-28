// Real-browser acceptance for friend invitations, public discovery and the judge question bank.
const assert = require('node:assert/strict');
const { chromium } = require('playwright-core');
const [site] = process.argv.slice(2);

async function visible(locator) {
  await locator.waitFor({ state: 'visible' });
}

async function clickSubmit(page, formName) {
  await page.locator(`form[data-form="${formName}"] button[type="submit"]`).click();
}

async function register(page, username) {
  await page.getByRole('button', { name: '注册账号', exact: true }).click();
  await page.locator('[name="username"]').fill(username);
  await page.locator('[name="password"]').fill('browser-password');
  await clickSubmit(page, 'register');
  await page.waitForFunction(() => !!state.me?.user?.uid);
  return page.evaluate(() => state.me.user.uid);
}

async function api(page, method, route, body = {}) {
  return page.evaluate(async ({ method, route, body }) => {
    const meResponse = await fetch('/api/me');
    const me = meResponse.ok ? await meResponse.json() : null;
    const headers = {};
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      headers['X-CSRF-Token'] = me?.user?.csrf || '';
    }
    const response = await fetch(route, {
      method,
      headers,
      body: method === 'GET' ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(`${method} ${route}: ${response.status} ${JSON.stringify(data)}`);
    return data;
  }, { method, route, body });
}

async function makeFriend(host, friend, friendUid) {
  const request = await api(host, 'POST', '/api/friends/requests', { uid: friendUid });
  await api(friend, 'POST', `/api/friends/requests/${request.request_id}/accept`);
}

async function createRoom(page, title, isPublic) {
  const form = page.locator('form[data-form="room"]');
  await visible(form.locator('[name="public_listing"]'));
  await form.locator('[name="title"]').fill(title);
  if (isPublic) await form.locator('[name="public_listing"]').check();
  await form.locator('[name="public_description"]').fill(isPublic ? '公开大厅浏览器验收房间' : '');
  await form.locator('button[type="submit"]').click();
  await page.waitForFunction(() => /^#room=\d+$/.test(location.hash));
  return page.evaluate(() => Number(location.hash.match(/room=(\d+)/)?.[1]));
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.PLAYWRIGHT_CHANNEL === 'default' ? {} : { channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' }),
  });
  const hostContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const playerContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const judgeContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const host = await hostContext.newPage();
  const player = await playerContext.newPage();
  const judge = await judgeContext.newPage();
  for (const page of [host, player, judge]) page.setDefaultTimeout(12000);
  const pageErrors = [];
  for (const page of [host, player, judge]) page.on('pageerror', error => pageErrors.push(String(error)));

  try {
    await Promise.all([host.goto(site), player.goto(site), judge.goto(site)]);
    const hostUid = await register(host, '社区浏览器房主');
    const playerUid = await register(player, '社区浏览器参赛者');
    const judgeUid = await register(judge, '社区浏览器裁判');
    await Promise.all([host, player, judge].map(page =>
      page.waitForFunction(() => state.dmSocket?.readyState === WebSocket.OPEN)));

    await makeFriend(host, player, playerUid);
    await makeFriend(host, judge, judgeUid);
    await host.waitForFunction(() => (state.social?.friends || []).length === 2);

    // A public room is visible to a logged-in non-member, and the public join action works.
    const publicRoomId = await createRoom(host, '公开大厅浏览器测试', true);
    await player.locator('#communityPublicRooms [data-community-action="public-refresh"]').click();
    const publicJoin = player.locator(`#communityPublicRooms [data-community-action="public-join"][data-id="${publicRoomId}"][data-role="player"]`);
    await visible(publicJoin);
    await publicJoin.click();
    await player.waitForFunction(id => state.roomId === id, publicRoomId);
    await player.waitForFunction(() => state.dmSocket?.readyState === WebSocket.OPEN);
    await player.locator('[data-action="back"]').click();
    await player.waitForFunction(() => !state.roomId && !!document.querySelector('#communityPublicRooms'));
    await player.waitForFunction(() => state.dmSocket?.readyState === WebSocket.OPEN);

    // The owner closes the first room, then creates a private room and invites friends by role.
    host.once('dialog', dialog => dialog.accept());
    await host.locator('[data-action="back"]').click();
    await host.waitForFunction(() => !state.roomId && !!document.querySelector('form[data-form="room"]'));
    const privateRoomId = await createRoom(host, '好友邀请与题库浏览器测试', false);
    await host.locator('[data-action="room-view"][data-view="admin"]').click();
    const inviteToggle = host.locator('#communityRoomControls [data-community-action="invite-panel-toggle"]');
    await visible(inviteToggle);
    await inviteToggle.click();
    const inviteForm = host.locator('form[data-community-form="room-invite"]');
    await visible(inviteForm);
    await inviteForm.locator(`input[name="uids"][value="${playerUid}"]`).check();
    await inviteForm.locator('select[name="role"]').selectOption('player');
    await inviteForm.locator('button[type="submit"]').click();

    const incomingPlayerInvite = player.locator('#communityRoomInvites [data-community-action="invite-accept"]');
    await visible(incomingPlayerInvite);
    await incomingPlayerInvite.click();
    await player.waitForFunction(id => state.roomId === id, privateRoomId);

    await host.locator('#communityRoomControls [data-community-action="invite-panel-toggle"]').click();
    const judgeInviteForm = host.locator('form[data-community-form="room-invite"]');
    await visible(judgeInviteForm);
    await judgeInviteForm.locator('select[name="role"]').selectOption('judge');
    await judgeInviteForm.locator(`input[name="uids"][value="${judgeUid}"]`).check();
    await judgeInviteForm.locator('button[type="submit"]').click();

    const inviteCode = await host.evaluate(() => state.room.invite_code);
    await api(host, 'POST', '/api/join', { code: inviteCode, role: 'player' });
    const incomingJudgeInvite = judge.locator('#communityRoomInvites [data-community-action="invite-accept"]');
    await visible(incomingJudgeInvite);
    await incomingJudgeInvite.click();
    await judge.waitForFunction(id => state.roomId === id && state.room?.is_judge, privateRoomId);

    // The question bank fills the judge's draft only; it does not submit a question.
    const questionForm = judge.locator('form[data-form="judge-question"]');
    await visible(questionForm);
    await questionForm.locator('[data-community-action="question-open"]').click();
    await visible(judge.locator('#communityQuestionBank'));
    await judge.locator('[data-community-question-category]').selectOption('logic');
    await judge.locator('[data-community-question-search]').fill('专家');
    const fillButton = judge.locator('#communityQuestionResults [data-community-action="question-fill"]');
    await visible(fillButton);
    await fillButton.click();
    const draft = await questionForm.locator('textarea[name="text"]').inputValue();
    assert(draft.includes('专家'));
    const room = await api(judge, 'GET', `/api/rooms/${privateRoomId}`);
    assert.equal(room.rounds[0].questions.length, 0, 'selecting a prompt must not submit it');
    assert.equal(room.rounds[0].question, '');

    assert.deepEqual(pageErrors, []);
    console.log(JSON.stringify({
      hostUid, playerUid, judgeUid, publicRoomId, privateRoomId,
      accountSocketPersistsOutsidePersonalCenter: true,
      publicJoin: true, friendInvites: true, judgeQuestionBankFillOnly: true,
    }));
  } finally {
    await browser.close();
  }
})();
