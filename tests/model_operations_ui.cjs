// Real-browser coverage for match readiness, per-seat task recovery and personal usage.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright-core');

(async () => {
  const [site] = process.argv.slice(2);
  const artifacts = path.resolve(__dirname, '../artifacts');
  fs.mkdirSync(artifacts, { recursive: true });
  const browser = await chromium.launch({ headless: true,
    ...(process.env.PLAYWRIGHT_CHANNEL === 'default' ? {} : { channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' }) });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  page.on('dialog', dialog => dialog.accept());
  try {
    await page.goto(site);
    await page.getByRole('button', { name: '使用旧登录名', exact: true }).click();
    await page.locator('[name="username"]').fill('browser_admin');
    await page.locator('[name="password"]').fill('browser-password');
    await page.locator('form[data-form="login"] button[type="submit"]').click();
    await page.locator('[data-action="personal-center"]').first().waitFor({ state: 'visible' }).catch(() => {});

    const me = await page.evaluate(async () => (await fetch('/api/me')).json());
    const csrf = me.user.csrf;
    const api = async (method, url, data) => {
      const response = await context.request.fetch(site + url, {
        method,
        headers: method === 'GET' ? {} : { 'X-CSRF-Token': csrf },
        data,
      });
      const text = await response.text();
      assert(response.ok(), method + ' ' + url + ' failed: ' + text);
      return text ? JSON.parse(text) : {};
    };
    const room = await api('POST', '/api/rooms', { title: '模型调用管理浏览器验收' });
    const models = await api('GET', '/api/me/models');
    assert(models.models.length > 0, 'model_site should provide at least one profile');
    const profileId = models.models[0].id;

    for (const username of ['ops_browser_player_a', 'ops_browser_player_b']) {
      const playerContext = await browser.newContext();
      const response = await playerContext.request.post(site + '/api/register', {
        data: { username, display_name: username, password: 'browser-player-password', code: room.code },
      });
      assert.equal(response.status(), 200, await response.text());
      await playerContext.close();
    }
    for (const name of ['浏览器额度裁判 A', '浏览器额度裁判 B']) {
      await api('POST', '/api/rooms/' + room.room_id + '/ai-judges', {
        name, model_profile_id: profileId,
      });
    }
    await api('PUT', '/api/rooms/' + room.room_id + '/model-request-limit', { limit: 1 });

    await page.goto(site + '/#room=' + room.room_id);
    await page.getByRole('heading', { name: '模型调用管理浏览器验收', exact: true }).waitFor();
    await page.locator('[data-action="room-view"][data-view="admin"]').click();
    await page.getByRole('heading', { name: '本轮准备', exact: true }).waitFor();
    assert((await page.locator('.readiness-grid').innerText()).includes('2 人已加入'));
    assert((await page.locator('.readiness-grid').innerText()).includes('已尝试 0 / 1'));

    await page.locator('[data-action="room-view"][data-view="ai-judges"]').click();
    await page.locator('[data-action="generate-ai-questions"]').click();
    await page.locator('.operation-status.blocked').waitFor();
    assert.equal(await page.locator('.operation-status.completed').count(), 1);
    assert.equal(await page.locator('.operation-status.blocked').count(), 1);
    assert((await page.locator('#app').innerText()).includes('本场模型请求次数已达上限'));

    await page.locator('[data-action="room-view"][data-view="admin"]').click();
    const limitForm = page.locator('form[data-form="model-request-limit"]');
    await limitForm.locator('[name="limit"]').fill('2');
    await limitForm.locator('button[type="submit"]').click();
    await page.getByText('本场模型请求上限已更新为 2', { exact: true }).waitFor();

    await page.locator('[data-action="room-view"][data-view="ai-judges"]').click();
    const retry = page.locator('[data-action="retry-model-operation"]');
    assert.equal(await retry.count(), 1, 'only the blocked AI judge should be retryable');
    await retry.click();
    await page.waitForFunction(() => document.querySelectorAll('.operation-status.completed').length === 2);
    assert.equal(await page.locator('.operation-status.blocked').count(), 1,
      'the original blocked attempt remains in history');

    await page.locator('[data-action="room-view"][data-view="admin"]').click();
    const start = page.locator('[data-action="start-round"][data-generate-questions="false"]');
    await start.waitFor();
    await start.click();
    await page.getByText('本轮已开始', { exact: false }).waitFor();
    const roomState = await api('GET', '/api/rooms/' + room.room_id);
    assert.equal(roomState.rounds[0].status, 'collecting');
    assert.equal(roomState.model_request_count, 2);

    await page.locator('[data-action="room-view"][data-view="ai-judges"]').click();
    const phaseHistory = page.locator('details[data-model-operation-history]');
    await phaseHistory.waitFor();
    assert.equal(await phaseHistory.evaluate(node => node.open), false,
      'previous phase model calls should be collapsed by default');
    assert.equal(await phaseHistory.locator('.operation-record').count(), 3);
    assert.equal(await phaseHistory.locator('[data-action="retry-model-operation"]').count(), 0,
      'previous phase tasks must not be retried from the current phase');

    await page.locator('[data-action="personal-center"]').click();
    const recordsPanel = page.locator('[data-model-operations-panel]');
    await recordsPanel.waitFor();
    await recordsPanel.locator('summary').click();
    await page.locator('.operation-record').first().waitFor();
    assert.equal(await page.locator('.operation-record').count(), 3);
    assert((await page.locator('.model-operations-body').innerText()).includes('已知 Token 合计：9'));

    const filters = page.locator('form[data-form="model-operation-filters"]');
    await filters.locator('[name="purpose"]').selectOption('judge_question');
    await filters.locator('button[type="submit"]').click();
    assert.equal(await page.locator('.operation-record').count(), 3);
    await filters.locator('[name="status"]').selectOption('completed');
    await filters.locator('button[type="submit"]').click();
    assert.equal(await page.locator('.operation-record').count(), 2);

    await page.screenshot({ path: path.join(artifacts, 'model-operations-desktop.png'),
      fullPage: true, animations: 'disabled' });
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      'personal operation records must not overflow a narrow viewport');
    await page.screenshot({ path: path.join(artifacts, 'model-operations-mobile.png'),
      fullPage: true, animations: 'disabled' });
    assert.deepEqual(errors, []);
    console.log('Model operations browser acceptance passed');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
