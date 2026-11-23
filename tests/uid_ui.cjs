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
async function register(page) {
  await page.getByRole('button', { name: '注册账号', exact: true }).click();
  assert.equal(await page.locator('form[data-form="register"] input[name="code"]').count(), 0);
  await page.locator('[name="username"]').fill('重复名字');
  await page.locator('[name="password"]').fill('browser-password');
  await clickSubmit(page, 'register');
}
async function logout(page) {
  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  await visible(page.locator('form[data-form="login"]'));
}

(async () => {
  const browserOptions = { headless: true };\n  if (process.env.PLAYWRIGHT_CHANNEL !== 'default') browserOptions.channel = process.env.PLAYWRIGHT_CHANNEL || 'msedge';\n  const browser = await chromium.launch(browserOptions);
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
      await page.getByRole('button', { name: '使用旧登录名', exact: true }).click();
      await page.locator('[name="username"]').fill('browser_admin');
      await page.locator('[name="password"]').fill('browser-password');
      await clickSubmit(page, 'login');
      await uidIs(page, 'TL00000001');
      await page.locator('form[data-form="room"] [name="title"]').fill('UID 浏览器验收');
      await clickSubmit(page, 'room');
      await page.locator('[data-action="room-view"][data-view="judges"]').click();
      const form = page.locator('form[data-form="add-judge"]');
      await form.locator('[name="username"]').fill('同名裁判');
      await form.locator('[name="display_name"]').fill('裁判一');
      await form.locator('[name="password"]').fill('judge-password');
      await clickSubmit(page, 'add-judge');
      const copy = page.locator('[data-action="copy-uid"][data-uid="TL00000002"]');
      await visible(copy);
      await copy.click();
      assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'TL00000002');
      // API requests share cookies; restore admin login before reloading.
      const other = await context.request.post(site + '/api/register', {
        data: { username: '同名裁判', password: 'other-password' } });
      assert.equal(other.status(), 200);
      const uid = (await other.json()).uid;
      assert.equal((await context.request.post(site + '/api/login', {
        data: { uid: 'TL00000001', password: 'browser-password' } })).status(), 200);
      await page.reload();
      await page.locator('[data-action="room-view"][data-view="judges"]').click();
      const assign = page.locator('form[data-form="assign-judge"]');
      assert((await assign.locator('[name="uid"]').boundingBox()).width >
        (await assign.boundingBox()).width * 0.8, 'UID 输入框应随表单宽度展开');
      await assign.locator('[name="uid"]').fill(uid);
      await assign.locator('[name="label"]').fill('裁判二');
      await clickSubmit(page, 'assign-judge');
      await visible(page.locator('[data-action="copy-uid"][data-uid="TL00000003"]'));
      await page.screenshot({ path: path.join(artifacts, 'uid-judges-desktop.png'), fullPage: true, animations: 'disabled' });
    } else {
      throw new Error('Unknown scenario: ' + scenario);
    }
    assert.deepEqual(errors, []);
    console.log('Browser acceptance passed: ' + scenario);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
