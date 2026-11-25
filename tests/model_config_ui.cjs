const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { chromium } = require('playwright-core');
const site = process.argv[2];
const artifacts = path.resolve(__dirname, '../artifacts');
fs.mkdirSync(artifacts, { recursive: true });

async function mutate(context, route, data, method = 'POST') {
  const meResponse = await context.request.get(site + '/api/me');
  const me = meResponse.status() === 200 ? await meResponse.json() : null;
  const headers = me?.user ? { 'X-CSRF-Token': me.user.csrf } : {};
  const response = await context.request.fetch(site + route, { method, data, headers });
  if (response.status() !== 200) {
    throw new Error(method + ' ' + route + ': ' + response.status() + ' ' + await response.text());
  }
  return response.json();
}

async function createProfile(context, name, model, apiKey) {
  return mutate(context, '/api/me/models', {
    name,
    base_url: 'https://api.example.com/v1',
    model,
    api_key: apiKey,
    temperature: 0.4,
    max_tokens: 512,
    timeout_seconds: 30,
  });
}

async function registerPlayer(browser, username, displayName) {
  const context = await browser.newContext();
  await mutate(context, '/api/register', {
    username,
    display_name: displayName,
    password: 'browser-player-password',
  });
  return context;
}

(async () => {
  const options = { headless: true };
  if (process.env.PLAYWRIGHT_CHANNEL !== 'default') {
    options.channel = process.env.PLAYWRIGHT_CHANNEL || 'msedge';
  }
  const browser = await chromium.launch(options);
  const hostContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await hostContext.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  page.on('dialog', dialog => dialog.accept());

  try {
    await mutate(hostContext, '/api/login', {
      username: 'browser_admin',
      password: 'browser-password',
    });
    const profileOne = await createProfile(hostContext, '同名配置', 'fold-model-one', 'sk-fold-one');
    const profileTwo = await createProfile(hostContext, '同名配置', 'fold-model-two', 'sk-fold-two');

    await page.goto(site);
    await page.locator('[data-action="personal-center"]').click();
    const first = page.locator('details[data-personal-model][data-id="' + profileOne.id + '"]');
    const second = page.locator('details[data-personal-model][data-id="' + profileTwo.id + '"]');
    await first.waitFor();
    await second.waitFor();
    assert.equal(await first.locator('summary').innerText(), '同名配置');
    assert.equal(await second.locator('summary').innerText(), '同名配置');
    assert.equal(await first.evaluate(el => el.open), false);
    assert.equal(await second.evaluate(el => el.open), false);
    assert.equal(await page.getByText('同名配置', { exact: true }).count(), 2);
    assert.equal(await first.locator('summary').getAttribute('aria-expanded'), 'false');
    assert.equal(await first.locator('form[data-form="personal-model-edit"]').isVisible(), false);
    await first.locator('summary').focus();
    await page.keyboard.press('Enter');
    assert.equal(await first.evaluate(el => el.open), true);
    assert.equal(await first.locator('summary').getAttribute('aria-expanded'), 'true');
    await page.keyboard.press('Space');
    assert.equal(await first.evaluate(el => el.open), false);
    assert.equal(await first.locator('summary').getAttribute('aria-expanded'), 'false');

    await first.locator('summary').click();
    await first.locator('[name="name"]').fill('正在编辑的配置');
    await first.locator('[name="model"]').fill('draft-model-one');
    await first.locator('[name="api_key"]').fill('sk-unsaved-browser-secret');
    await first.locator('[data-action="test-personal-model"]').click();
    await page.locator('#toast').filter({ hasText: '连接成功' }).waitFor();
    assert.equal(await first.locator('[name="name"]').inputValue(), '正在编辑的配置');
    assert.equal(await first.locator('[name="model"]').inputValue(), 'draft-model-one');
    assert.equal(await first.locator('[name="api_key"]').inputValue(), 'sk-unsaved-browser-secret');

    await page.evaluate(() => renderPersonalCenter());
    assert.equal(await first.evaluate(el => el.open), true);
    assert.equal(await first.locator('[name="model"]').inputValue(), 'draft-model-one');
    assert.equal(await first.locator('[name="api_key"]').inputValue(), 'sk-unsaved-browser-secret');

    await first.locator('summary').click();
    await second.locator('summary').click();
    await second.locator('[name="model"]').fill('unsaved-model-two');
    await second.locator('[name="api_key"]').fill('sk-unsaved-second-secret');
    await second.locator('[data-action="test-personal-model"]').click();
    await page.locator('#toast').filter({ hasText: '模拟连接失败' }).waitFor();
    assert.equal(await second.locator('[name="model"]').inputValue(), 'unsaved-model-two');
    assert.equal(await second.locator('[name="api_key"]').inputValue(), 'sk-unsaved-second-secret');
    assert.equal(await second.locator('[data-action="test-personal-model"]').isEnabled(), true);
    await page.evaluate(() => renderPersonalCenter());
    assert.equal(await first.evaluate(el => el.open), false);
    assert.equal(await second.evaluate(el => el.open), true);
    assert.equal(await second.locator('[name="model"]').inputValue(), 'unsaved-model-two');
    assert.equal(await second.locator('[name="api_key"]').inputValue(), 'sk-unsaved-second-secret');

    await first.locator('summary').click();
    await first.locator('[name="name"]').fill('已保存的配置');
    await first.locator('[name="model"]').fill('saved-model-one');
    await first.locator('[name="api_key"]').fill('sk-saved-browser-key');
    await first.locator('[type="submit"]').click();
    await page.locator('#toast').filter({ hasText: '个人模型配置已更新' }).waitFor();
    assert.equal(await first.locator('summary').innerText(), '已保存的配置');
    assert.equal(await first.locator('[name="model"]').inputValue(), 'saved-model-one');
    assert.equal(await second.evaluate(el => el.open), true);
    assert.equal(await second.locator('[name="model"]').inputValue(), 'unsaved-model-two');
    assert.equal(await second.locator('[name="api_key"]').inputValue(), 'sk-unsaved-second-secret');

    await second.locator('[data-action="delete-personal-model"]').click();
    await first.waitFor();
    assert.equal(await page.locator('details[data-personal-model]').count(), 1);
    assert.equal(await first.evaluate(el => el.open), true);
    assert.equal(await first.locator('[name="model"]').inputValue(), 'saved-model-one');
    assert.equal(await first.getByText('API Key 可解密', { exact: true }).count(), 1);
    assert.equal(await page.getByText('API Key 已验证', { exact: true }).count(), 0);

    await first.locator('summary').click();
    await page.screenshot({ path: path.join(artifacts, 'model-config-desktop-light.png'), fullPage: true });
    await page.locator('#themeBtn').click();
    await page.screenshot({ path: path.join(artifacts, 'model-config-desktop-dark.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator('#themeBtn').click();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(artifacts, 'model-config-mobile-light.png'), fullPage: true });
    await page.locator('#themeBtn').click();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(artifacts, 'model-config-mobile-dark.png'), fullPage: true });
    await first.locator('summary').click();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(artifacts, 'model-config-mobile-expanded.png'), fullPage: true });

    const stored = await page.evaluate(() =>
      [...Object.values(localStorage), ...Object.values(sessionStorage)].join('\n'));
    assert.equal(stored.includes('sk-unsaved-browser-secret'), false);
    assert.equal(stored.includes('sk-unsaved-second-secret'), false);

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator('#themeBtn').click();
    const roomAuto = await mutate(hostContext, '/api/rooms', { title: '自动出题 UI 验收' });
    const roomManual = await mutate(hostContext, '/api/rooms', { title: '手动生成 UI 验收' });
    await mutate(hostContext, '/api/rooms/' + roomAuto.room_id + '/ai-judges', {
      name: '自动出题 AI 裁判', model_profile_id: profileOne.id,
    });
    await mutate(hostContext, '/api/rooms/' + roomManual.room_id + '/ai-judges', {
      name: '手动出题 AI 裁判', model_profile_id: profileOne.id,
    });

    const playerOne = await registerPlayer(browser, 'question_player_one', '问题玩家一');
    const playerTwo = await registerPlayer(browser, 'question_player_two', '问题玩家二');

    await page.goto(site + '#room=' + roomAuto.room_id);
    assert.equal(await page.getByText('人类玩家互相可见真实账号身份；裁判包仅含匿名代号。', { exact: true }).count(), 0);
    assert.equal(await page.getByText('在房主管理台选择以人类身份加入。', { exact: true }).count(), 1);
    await page.screenshot({ path: path.join(artifacts, 'room-player-p5-p6.png'), fullPage: true });

    await page.locator('[data-action="room-view"][data-view="human-judges"]').click();
    assert.equal(await page.getByText('0 位真人裁判', { exact: true }).count(), 1);
    assert.equal(await page.getByText('尚无真人裁判。', { exact: true }).count(), 0);
    assert.equal(await page.getByText(/真人裁判自行注册并使用 UID 登录/).count(), 0);
    assert.equal(await page.getByText(/同一 UID 同时只能担任一个未完赛房间/).count(), 0);
    await page.screenshot({ path: path.join(artifacts, 'room-human-judges-p7.png'), fullPage: true });

    await page.locator('[data-action="room-view"][data-view="admin"]').click();
    const removedCopy = [
      '模型接入的 AI 参赛者可自动回答；AI 裁判可自动出题和判断。未接入模型的 AI 席位仍可手动转录。',
      '可以添加手动 AI 席位，也可以先在个人中心配置自己的模型 API。',
      '房主也可使用邀请码加入，以人类身份回答。',
      '点击轮次可查看历史记录。',
      '查看本轮问题与回答',
      '查看真人裁判名单',
      '配置 AI 裁判席位',
      '管理参赛者与比赛',
      '管理名字、密码和个人模型 API',
      '仍需完成全部轮次。等待所有裁判锁定最终判断。',
      '所有真人 / AI 裁判的问题会统一汇总；由房主在此开启作答。',
    ];
    for (const copy of removedCopy) {
      assert.equal(await page.getByText(copy, { exact: true }).count(), 0, 'stale copy remains: ' + copy);
    }
    await page.screenshot({ path: path.join(artifacts, 'room-admin-p2-p3-p4.png'), fullPage: true });
    let startButton = page.locator('[data-action="start-round"]');
    await startButton.waitFor();
    assert.equal(await startButton.isDisabled(), true);
    assert.equal(await page.getByText(/至少需要两名在场参赛者/).count(), 1);

    await page.locator('[data-action="room-view"][data-view="ai-judges"]').click();
    let generateButton = page.locator('[data-action="generate-ai-questions"]');
    await generateButton.waitFor();
    assert.equal(await generateButton.isDisabled(), true);
    assert.equal(await page.getByText(/至少需要两名在场参赛者/).count(), 1);

    await mutate(playerOne, '/api/join', { code: roomAuto.code, role: 'player' });
    await page.locator('[data-action="sync"]').click();
    await page.locator('[data-action="room-view"][data-view="admin"]').click();
    startButton = page.locator('[data-action="start-round"]');
    assert.equal(await startButton.isDisabled(), true);

    await mutate(playerTwo, '/api/join', { code: roomAuto.code, role: 'player' });
    await page.locator('[data-action="sync"]').click();
    await page.locator('[data-action="room-view"][data-view="admin"]').click();
    startButton = page.locator('[data-action="start-round"]');
    assert.equal(await startButton.isEnabled(), true);
    await startButton.click();
    await page.waitForFunction(() => state.room?.rounds?.[0]?.status === 'collecting');
    const autoRoom = await (await hostContext.request.get(site + '/api/rooms/' + roomAuto.room_id)).json();
    assert.equal(autoRoom.rounds[0].questions.length, 1);
    assert.equal(autoRoom.rounds[0].status, 'collecting');

    await mutate(playerOne, '/api/join', { code: roomManual.code, role: 'player' });
    await mutate(playerTwo, '/api/join', { code: roomManual.code, role: 'player' });
    await page.goto(site + '#room=' + roomManual.room_id);
    await page.locator('[data-action="room-view"][data-view="ai-judges"]').click();
    generateButton = page.locator('[data-action="generate-ai-questions"]');
    assert.equal(await generateButton.isEnabled(), true);
    await generateButton.click();
    await page.waitForFunction(() => state.room?.rounds?.[0]?.questions?.length === 1);
    const manualRoom = await (await hostContext.request.get(site + '/api/rooms/' + roomManual.room_id)).json();
    assert.equal(manualRoom.rounds[0].questions.length, 1);
    assert.equal(manualRoom.rounds[0].status, 'draft');

    await page.locator('#logoutBtn').click();
    await page.locator('form[data-form="login"]').waitFor();
    const cleared = await page.evaluate(() => ({
      drafts: Object.keys(state.personalModelDrafts).length,
      expanded: Object.keys(state.personalModelExpanded).length,
      owner: state.personalModelDraftOwner,
    }));
    assert.deepEqual(cleared, { drafts: 0, expanded: 0, owner: null });
    assert.deepEqual(errors, []);
    console.log('Model configuration folding and AI judge question browser checks passed.');
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
