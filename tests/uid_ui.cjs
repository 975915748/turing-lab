// Real-browser acceptance: no mocked requests or replacement UI components.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright-core');
const [site, scenario] = process.argv.slice(2);
const artifacts = path.resolve(__dirname, '../artifacts');
fs.mkdirSync(artifacts, { recursive: true });

async function visible(locator) { await locator.waitFor({ state: 'visible' }); }
async function uidIs(page, uid) {
  await visible(page.locator('#accountUid'));
  assert.equal(await page.locator('#accountUid').textContent(), uid);
}
async function clickSubmit(page, form) {
  await page.locator(`form[data-form="${form}"] button[type="submit"]`).click();
}
async function register(page, username = '重复名字') {
  await page.getByRole('button', { name: '注册账号', exact: true }).click();
  assert.equal(await page.locator('form[data-form="register"] input[name="code"]').count(), 0);
  await page.locator('[name="username"]').fill(username);
  await page.locator('[name="password"]').fill('browser-password');
  await clickSubmit(page, 'register');
}
async function logout(page) {
  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  await visible(page.locator('form[data-form="login"]'));
}

(async () => {
  const browserOptions = { headless: true };
  if (process.env.PLAYWRIGHT_CHANNEL !== 'default') browserOptions.channel = process.env.PLAYWRIGHT_CHANNEL || 'msedge';
  const browser = await chromium.launch(browserOptions);
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 },
    permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  try {
    await page.goto(site);
    if (scenario === 'register') {
      await register(page);
      await uidIs(page, 'TL00000002');
      await page.getByRole('button', { name: '复制 UID', exact: true }).click();
      assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'TL00000002');
      assert.equal(await page.getByText('尚未加入任何房间', { exact: false }).count(), 1);
      await page.screenshot({ path: path.join(artifacts, 'uid-lobby-desktop.png'), fullPage: true, animations: 'disabled' });
      await logout(page);
      await register(page);
      await uidIs(page, 'TL00000003');
      await logout(page);
      await page.locator('[name="uid"]').fill('TL00000002');
      await page.locator('[name="password"]').fill('browser-password');
      await clickSubmit(page, 'login');
      await uidIs(page, 'TL00000002');
      await page.getByRole('button', { name: '深色', exact: false }).click();
      await page.setViewportSize({ width: 390, height: 844 });
      assert.equal(await page.locator('body').getAttribute('data-theme'), 'dark');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(artifacts, 'uid-lobby-mobile-dark.png'), fullPage: true, animations: 'disabled' });
      await logout(page);
      await page.getByRole('button', { name: '注册账号', exact: true }).click();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(artifacts, 'uid-register-mobile-dark.png'), fullPage: true, animations: 'disabled' });
    } else if (scenario === 'judges') {
      // Host creates a room and retains the AI judge management workflow.
      await page.getByRole('button', { name: '使用旧登录名', exact: true }).click();
      await page.locator('[name="username"]').fill('browser_admin');
      await page.locator('[name="password"]').fill('browser-password');
      await clickSubmit(page, 'login');
      await uidIs(page, 'TL00000001');
      await page.locator('form[data-form="room"] [name="title"]').fill('真人裁判自助入房验收');
      await clickSubmit(page, 'room');
      const info = await (await context.request.get(site + '/api/me')).json();
      const rid = info.rooms[0].id;
      const base = `/api/rooms/${rid}`;
      const room = await (await context.request.get(site + base)).json();
      assert(room.invite_code);

      await page.locator('[data-action="room-view"][data-view="human-judges"]').click();
      await page.locator('h2', { hasText: '真人裁判管理' }).waitFor();
      await page.locator('form[data-form="add-judge"]').count().then(n => assert.equal(n, 0));
      await page.locator('form[data-form="assign-judge"]').count().then(n => assert.equal(n, 0));
      await page.locator('[data-action="room-view"][data-view="ai-judges"]').click();
      await page.locator('form[data-form="add-ai-judge"]').waitFor();
      await page.locator('form[data-form="add-ai-judge"] [name="name"]').fill('保留的手动 AI 裁判');
      await clickSubmit(page, 'add-ai-judge');
      await page.locator('.feed-item').filter({ hasText: '保留的手动 AI 裁判' }).waitFor();

      // A normal user self-registers, logs in by UID, and chooses the judge role in the lobby.
      const judgeContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
      const judgePage = await judgeContext.newPage();
      judgePage.setDefaultTimeout(10000);
      judgePage.on('pageerror', error => errors.push(String(error)));
      await judgePage.goto(site);
      await register(judgePage, '自助真人裁判');
      await uidIs(judgePage, 'TL00000002');
      await logout(judgePage);
      await judgePage.locator('[name="uid"]').fill('TL00000002');
      await judgePage.locator('[name="password"]').fill('browser-password');
      await clickSubmit(judgePage, 'login');
      await uidIs(judgePage, 'TL00000002');

      const join = judgePage.locator('form[data-form="join"]');
      assert.equal(await join.locator('select[name="role"] option[value="judge"]').count(), 1);
      await join.locator('[name="code"]').fill(room.invite_code);
      await join.locator('[name="role"]').selectOption('judge');
      await clickSubmit(judgePage, 'join');
      await judgePage.locator('.pagehead .pill').filter({ hasText: '独立裁判' }).waitFor();
      const blind = await (await judgeContext.request.get(site + base)).json();
      assert.equal(blind.is_judge, true);
      assert.equal(Object.hasOwn(blind, 'humans'), false);
      assert.equal(Object.hasOwn(blind, 'invite_code'), false);
      assert.equal(Object.hasOwn(blind, 'judges'), false);

      // Host sees the roster update; the self-service account is not exposed to judges.
      await page.reload();
      await page.locator('[data-action="room-view"][data-view="human-judges"]').click();
      await page.getByText('真人裁判 · TL00000002', { exact: true }).waitFor();
      assert.equal(await page.locator('form[data-form="add-judge"]').count(), 0);
      assert.equal(await page.locator('form[data-form="assign-judge"]').count(), 0);
      await page.locator('[data-action="room-view"][data-view="ai-judges"]').click();
      assert.equal(await page.locator('form[data-form="add-ai-judge"]').count(), 1);

      // A repeated join from the same UID is idempotent.
      const judgeMe = await (await judgeContext.request.get(site + '/api/me')).json();
      const repeated = await judgeContext.request.post(site + '/api/join', {
        data: { code: room.invite_code, role: 'judge' },
        headers: { 'X-CSRF-Token': judgeMe.user.csrf },
      });
      assert.equal(repeated.status(), 200, await repeated.text());
      const hostRoom = await (await context.request.get(site + base)).json();
      assert.equal(hostRoom.judges.filter(j => j.kind === 'human').length, 1);

      // Personal center owns the profile, password, and model configuration sections.
      await page.locator('[data-action="personal-center"]').first().click();
      await page.locator('form[data-form="profile-name"]').waitFor();
      await page.locator('form[data-form="personal-model-create"]').waitFor();
      await page.getByText('采样温度', { exact: true }).waitFor();
      const modelText = await page.locator('#app').innerText();
      assert.equal(modelText.includes('Temperature'), false);
      assert.equal(modelText.includes('Base URL 填到'), false);
      assert.equal(modelText.includes('MODEL_CREDENTIAL_KEY'), false);
      assert.equal(modelText.includes('Fernet.generate_key'), false);
      assert.equal(modelText.includes('Railway'), false);
      const modelState = await (await context.request.get(site + '/api/me/models')).json();
      if (!modelState.model_api_enabled) {
        await page.getByText('模型 API 暂不可用，请联系管理员。', { exact: true }).waitFor();
        assert.equal(await page.locator('form[data-form="personal-model-create"] button[type="submit"]').isDisabled(), true);
      }

      // Check both themes and the 390px layout in a real browser.
      await page.screenshot({ path: path.join(artifacts, 'self-service-judges-desktop-light.png'), fullPage: true, animations: 'disabled' });
      assert.equal(await page.locator('body').getAttribute('data-theme'), 'light');
      await page.setViewportSize({ width: 390, height: 844 });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(artifacts, 'self-service-judges-mobile-light.png'), fullPage: true, animations: 'disabled' });
      await page.locator('#themeBtn').click();
      assert.equal(await page.locator('body').getAttribute('data-theme'), 'dark');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(artifacts, 'self-service-judges-mobile-dark.png'), fullPage: true, animations: 'disabled' });

      await judgePage.locator('[data-action="back"]').click();
      await judgePage.setViewportSize({ width: 390, height: 844 });
      const mobileJoin = judgePage.locator('form[data-form="join"]');
      const joinBox = await mobileJoin.boundingBox();
      const roleBox = await mobileJoin.locator('select[name="role"]').boundingBox();
      assert(roleBox.width > joinBox.width * 0.8, '移动端身份选择框应随表单宽度展开');
      assert(await judgePage.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await judgePage.screenshot({ path: path.join(artifacts, 'self-service-judge-lobby-mobile.png'), fullPage: true, animations: 'disabled' });

      await judgeContext.close();
    } else {
      throw new Error('Unknown scenario: ' + scenario);
    }
    assert.deepEqual(errors, []);
    console.log('Browser acceptance passed: ' + scenario);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
