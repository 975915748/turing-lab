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
async function screenshot(page, name) {
  await page.screenshot({ path: path.join(artifacts, name), fullPage: true, animations: 'disabled' });
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
      const roomCreated = page.waitForResponse(response =>
        response.url().endsWith('/api/rooms') && response.request().method() === 'POST');
      await clickSubmit(page, 'room');
      const createdRoom = await roomCreated;
      assert.equal(createdRoom.status(), 200, await createdRoom.text());
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
      assert.equal(await judgePage.locator('.chat-card').count(), 0);
      assert.equal(await judgePage.getByText('公共聊天', { exact: true }).count(), 0);
      const blind = await (await judgeContext.request.get(site + base)).json();
      assert.equal(blind.is_judge, true);
      assert.equal(Object.hasOwn(blind, 'humans'), false);
      assert.equal(Object.hasOwn(blind, 'invite_code'), false);
      assert.equal(Object.hasOwn(blind, 'judges'), false);

      // Host sees the roster update; the self-service account is not exposed to judges.
      await page.reload();
      await page.locator('[data-action="room-view"][data-view="human-judges"]').click();
      await page.getByText('UID：TL00000002', { exact: true }).waitFor();
      assert.equal(await page.locator('form[data-form="add-judge"]').count(), 0);
      assert.equal(await page.locator('form[data-form="assign-judge"]').count(), 0);

      // Visual coverage: the independent human-judge management page in all requested viewport/theme combinations.
      await page.setViewportSize({ width: 1440, height: 1000 });
      assert.equal(await page.locator('body').getAttribute('data-theme'), 'light');
      await page.screenshot({ path: path.join(artifacts, 'human-judges-desktop-light.png'), fullPage: true, animations: 'disabled' });
      await page.locator('#themeBtn').click();
      assert.equal(await page.locator('body').getAttribute('data-theme'), 'dark');
      await page.screenshot({ path: path.join(artifacts, 'human-judges-desktop-dark.png'), fullPage: true, animations: 'disabled' });
      await page.setViewportSize({ width: 390, height: 844 });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(artifacts, 'human-judges-mobile-dark.png'), fullPage: true, animations: 'disabled' });
      await page.locator('#themeBtn').click();
      assert.equal(await page.locator('body').getAttribute('data-theme'), 'light');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(artifacts, 'human-judges-mobile-light.png'), fullPage: true, animations: 'disabled' });
      await page.setViewportSize({ width: 1440, height: 1000 });

      await page.locator('[data-action="room-view"][data-view="ai-judges"]').click();
      assert.equal(await page.locator('form[data-form="add-ai-judge"]').count(), 1);
      await page.getByText('服务商可能按实际调用量计费。', { exact: false }).first().waitFor();

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
      await page.getByText('个人模型 API', { exact: true }).waitFor();
      const modelText = await page.locator('#app').innerText();
      assert.equal(modelText.includes('Temperature'), false);
      assert.equal(modelText.includes('Base URL 填到'), false);
      assert.equal(modelText.includes('MODEL_CREDENTIAL_KEY'), false);
      assert.equal(modelText.includes('Fernet.generate_key'), false);
      assert.equal(modelText.includes('Railway'), false);
      const modelState = await (await context.request.get(site + '/api/me/models')).json();
      if (!modelState.model_api_enabled) {
        await page.getByText('模型 API 暂不可用，请联系管理员。', { exact: true }).waitFor();
        assert.equal(await page.locator('form[data-form="personal-model-create"]').count(), 0);
      } else {
        await page.locator('form[data-form="personal-model-create"]').waitFor();
        await page.getByText('采样温度', { exact: true }).waitFor();
      }

      // Personal center visual coverage in all requested viewport/theme combinations.
      assert.equal(await page.locator('body').getAttribute('data-theme'), 'light');
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.screenshot({ path: path.join(artifacts, 'personal-center-desktop-light.png'), fullPage: true, animations: 'disabled' });
      await page.locator('#themeBtn').click();
      assert.equal(await page.locator('body').getAttribute('data-theme'), 'dark');
      await page.screenshot({ path: path.join(artifacts, 'personal-center-desktop-dark.png'), fullPage: true, animations: 'disabled' });
      await page.setViewportSize({ width: 390, height: 844 });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(artifacts, 'personal-center-mobile-dark.png'), fullPage: true, animations: 'disabled' });
      await page.locator('#themeBtn').click();
      assert.equal(await page.locator('body').getAttribute('data-theme'), 'light');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(artifacts, 'personal-center-mobile-light.png'), fullPage: true, animations: 'disabled' });

      await judgePage.locator('[data-action="back"]').click();
      await judgePage.setViewportSize({ width: 390, height: 844 });
      const mobileJoin = judgePage.locator('form[data-form="join"]');
      const joinBox = await mobileJoin.boundingBox();
      const roleBox = await mobileJoin.locator('select[name="role"]').boundingBox();
      assert(roleBox.width > joinBox.width * 0.8, '移动端身份选择框应随表单宽度展开');
      assert(await judgePage.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await judgePage.screenshot({ path: path.join(artifacts, 'self-service-judge-lobby-mobile.png'), fullPage: true, animations: 'disabled' });

      await judgeContext.close();
    } else if (scenario === 'models') {
      await page.getByRole('button', { name: '使用旧登录名', exact: true }).click();
      await page.locator('[name="username"]').fill('browser_admin');
      await page.locator('[name="password"]').fill('browser-password');
      await clickSubmit(page, 'login');
      await uidIs(page, 'TL00000001');
      await page.locator('[data-action="personal-center"]').click();
      await page.locator('details[data-model-config]').first().waitFor();

      const profiles = await (await context.request.get(site + '/api/me/models')).json();
      assert.equal(profiles.model_api_enabled, true);
      assert.equal(profiles.models.length, 4);
      const ids = profiles.models.map(p => p.id);
      const first = page.locator('details[data-model-config][data-id="' + ids[0] + '"]');
      const second = page.locator('details[data-model-config][data-id="' + ids[1] + '"]');
      const longName = page.locator('details[data-model-config][data-id="' + ids[2] + '"]');
      const repair = page.locator('details[data-model-config][data-id="' + ids[3] + '"]');
      assert.equal(await first.locator('summary strong').innerText(), '重复配置');
      assert.equal(await second.locator('summary strong').innerText(), '重复配置');
      for (const card of [first, second, longName, repair]) {
        assert.equal(await card.evaluate(el => el.open), false, '已有配置应默认收起');
        assert.equal(await card.locator('form[data-form="personal-model-edit"]').isVisible(), false);
      }
      await page.screenshot({ path: path.join(artifacts, 'model-config-desktop-collapsed-light.png'), fullPage: true, animations: 'disabled' });

      // Same-name cards are keyed by profile ID and expand independently.
      await first.locator('summary').click();
      await page.waitForFunction(id => state.personalModelExpanded[id] === true, ids[0]);
      await first.locator('form[data-form="personal-model-edit"]').waitFor({ state: 'visible' });
      assert.equal(await second.evaluate(el => el.open), false);

      // A real mocked connection test is separate from the stored credential status.
      assert.equal(await first.getByText('API Key 已保存（可解密）', { exact: true }).count(), 1);
      assert.equal(await first.getByText('API Key 已验证', { exact: true }).count(), 0);

      // Test the saved credential while a separate unsaved draft is being edited.
      await first.locator('[name="model"]').fill('draft-model-first');
      await first.locator('[name="api_key"]').fill('sk-unsaved-only');
      assert.equal(await first.locator('[data-model-test-result]').innerText(), '');
      await first.locator('[data-action="test-personal-model"]').click();
      await first.locator('[data-model-test-result]').getByText('已保存配置连接测试成功', { exact: false }).waitFor();
      assert.equal(await first.locator('[name="model"]').inputValue(), 'draft-model-first');
      assert.equal(await first.locator('[name="api_key"]').inputValue(), 'sk-unsaved-only');

      // Collapse / expand retains unsaved fields.
      await first.locator('summary').focus();
      await page.keyboard.press('Enter');
      await page.waitForFunction(id => state.personalModelExpanded[id] === false, ids[0]);
      await page.keyboard.press('Space');
      await page.waitForFunction(id => state.personalModelExpanded[id] === true, ids[0]);
      assert.equal(await first.locator('[name="model"]').inputValue(), 'draft-model-first');
      assert.equal(await first.locator('[name="api_key"]').inputValue(), 'sk-unsaved-only');

      // A second card may be edited independently while the first is saved.
      await second.locator('summary').click();
      await page.waitForFunction(id => state.personalModelExpanded[id] === true, ids[1]);
      await second.locator('[name="model"]').fill('second-unsaved-model');
      await second.locator('[name="api_key"]').fill('sk-second-draft');
      await first.locator('[name="model"]').fill('saved-model-first');
      await first.locator('[name="api_key"]').fill('sk-new-saved-key');

      // A slow test of the old saved profile must not overwrite the new profile
      // or a newer test started after the profile has been saved.
      const testUrl = '**/api/me/models/' + ids[0] + '/test';
      let releaseFirstTest, releaseSaveWindowTest, releaseNewTest;
      let firstTestStartedResolve, saveWindowTestStartedResolve, newTestStartedResolve;
      const firstTestGate = new Promise(resolve => { releaseFirstTest = resolve; });
      const saveWindowTestGate = new Promise(resolve => { releaseSaveWindowTest = resolve; });
      const newTestGate = new Promise(resolve => { releaseNewTest = resolve; });
      const firstTestStarted = new Promise(resolve => { firstTestStartedResolve = resolve; });
      const saveWindowTestStarted = new Promise(resolve => { saveWindowTestStartedResolve = resolve; });
      const newTestStarted = new Promise(resolve => { newTestStartedResolve = resolve; });
      let testRequestCount = 0;
      await page.route(testUrl, async route => {
        const index = ++testRequestCount;
        const response = await route.fetch();
        if (index === 1) {
          firstTestStartedResolve();
          await firstTestGate;
        } else if (index === 2) {
          saveWindowTestStartedResolve();
          await saveWindowTestGate;
        } else if (index === 3) {
          newTestStartedResolve();
          await newTestGate;
        }
        await route.fulfill({ response });
      });
      const oldTestResponsePromise = page.waitForResponse(response =>
        response.url().endsWith('/api/me/models/' + ids[0] + '/test') && response.request().method() === 'POST');
      await first.locator('[data-action="test-personal-model"]').click();
      await firstTestStarted;

      const saveUrl = '**/api/me/models/' + ids[0];
      let releaseSave, saveStartedResolve;
      const saveGate = new Promise(resolve => { releaseSave = resolve; });
      const saveStarted = new Promise(resolve => { saveStartedResolve = resolve; });
      await page.route(saveUrl, async route => {
        saveStartedResolve();
        await saveGate;
        const response = await route.fetch();
        await route.fulfill({ response });
      });
      const saveResponsePromise = page.waitForResponse(response =>
        response.url().endsWith('/api/me/models/' + ids[0]) && response.request().method() === 'PUT');
      await first.locator('form[data-form="personal-model-edit"] [type="submit"]').click();
      await saveStarted;

      const testButtonDuringSave = first.locator('[data-action="test-personal-model"]');
      assert.equal(await testButtonDuringSave.isDisabled(), true,
        '保存 API Key 期间必须禁用连接测试');
      assert.equal(await testButtonDuringSave.innerText(), '保存中…');
      assert.equal(await page.evaluate(id => beginPersonalModelTest(id), ids[0]), null,
        '保存期间不能创建新的模型测试请求');
      await testButtonDuringSave.evaluate(button =>
        button.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })));
      await page.waitForTimeout(30);
      assert.equal(testRequestCount, 1, '保存期间的点击不能发起新的测试请求');

      // Reproduce a request that slips into the save window through an already-
      // queued caller. Its server-side test uses the old credential and returns late.
      const saveWindowRequest = await page.evaluate(id => {
        const key = String(id), version = nextPersonalModelTestVersion(key);
        const request = { version, epoch: state.personalModelEpoch, uid: state.me?.user?.uid,
          profileSnapshot: personalModelTestFingerprint(state.personalModels.find(p => Number(p.id) === Number(id))) };
        state.personalModelTestPending[key] = version;
        return request;
      }, ids[0]);
      const saveWindowTestApiPromise = page.evaluate(id =>
        api('/api/me/models/' + id + '/test', { method: 'POST' }), ids[0]);
      await saveWindowTestStarted;
      assert.equal(testRequestCount, 2, '竞态探针应在保存请求尚未提交时发起第二个模型测试');

      // Keep both old-credential test responses pending until the new test starts.
      releaseSave();
      const saveResponse = await saveResponsePromise;
      assert.equal(saveResponse.status(), 200, await saveResponse.text());
      await page.locator('#toast').filter({ hasText: '个人模型配置已更新' }).waitFor();
      assert.equal(await page.evaluate(({id, request}) => isCurrentPersonalModelTest(id, request),
        { id: ids[0], request: saveWindowRequest }), false,
        '保存成功后必须使保存期间产生的测试请求失效');
      await page.unroute(saveUrl);

      const firstAfterSave = page.locator('details[data-model-config][data-id="' + ids[0] + '"]');
      const secondAfterSave = page.locator('details[data-model-config][data-id="' + ids[1] + '"]');
      const newTestResponsePromise = page.waitForResponse(response =>
        response.url().endsWith('/api/me/models/' + ids[0] + '/test') && response.request().method() === 'POST');
      await firstAfterSave.locator('[data-action="test-personal-model"]').click();
      await newTestStarted;

      // Both the pre-save request and the request started during save return
      // while the new-profile test is pending. Neither may write test state.
      releaseFirstTest();
      const oldTestResponse = await oldTestResponsePromise;
      assert.equal(oldTestResponse.status(), 200);
      releaseSaveWindowTest();
      const saveWindowTestResult = await saveWindowTestApiPromise;
      const saveWindowAccepted = await page.evaluate(({id, request, result}) =>
        updateModelTestResult(id, { ok: true, model: result.model, latency_ms: result.latency_ms }, request),
        { id: ids[0], request: saveWindowRequest, result: saveWindowTestResult });
      assert.equal(saveWindowAccepted, false,
        '保存期间发起的旧凭据测试结果必须被保存完成后的失效标记拒绝');
      await page.waitForTimeout(30);
      assert.equal(await page.evaluate(id => state.modelTestResults[id] ?? null, ids[0]), null,
        '旧配置的迟到测试结果不能写入新配置状态');
      assert.equal(await firstAfterSave.locator('[data-action="test-personal-model"]').isDisabled(), true,
        '旧请求结束不能提前解除新测试的加载状态');

      releaseNewTest();
      const newTestResponse = await newTestResponsePromise;
      assert.equal(newTestResponse.status(), 200);
      await firstAfterSave.locator('[data-model-test-result]').getByText('已保存配置连接测试成功', { exact: false }).waitFor();
      assert.equal(await page.evaluate(id => state.modelTestResults[id]?.profileSnapshot ===
        JSON.stringify([id, '重复配置', 'https://api.example.com/v1', 'saved-model-first', 0.7, 512, 30, true, 'ok']), ids[0]), true,
        '最新测试结果必须绑定到已保存的新配置快照');
      await page.unroute(testUrl);

      assert.equal(await firstAfterSave.evaluate(el => el.open), true, '保存后应保留第一条配置的展开状态');
      assert.equal(await secondAfterSave.evaluate(el => el.open), true, '保存其他配置时不应折叠第二条配置');
      assert.equal(await firstAfterSave.locator('[name="model"]').inputValue(), 'saved-model-first');
      assert.equal(await secondAfterSave.locator('[name="model"]').inputValue(), 'second-unsaved-model');
      assert.equal(await secondAfterSave.locator('[name="api_key"]').inputValue(), 'sk-second-draft');
      const savedProfiles = await (await context.request.get(site + '/api/me/models')).json();
      const savedFirst = savedProfiles.models.find(p => p.id === ids[0]);
      assert.equal(savedFirst.model, 'saved-model-first');
      assert.equal(savedFirst.has_api_key, true, '保存新 API Key 后仍应保留密钥状态');
      assert.equal(Object.hasOwn(savedFirst, 'api_key'), false);
      const localValues = await page.evaluate(() => Object.keys(localStorage).map(k => localStorage.getItem(k)).join('|'));
      assert.equal(localValues.includes('sk-unsaved-only'), false);
      assert.equal(localValues.includes('sk-new-saved-key'), false);
      assert.equal(localValues.includes('sk-second-draft'), false);

      // A credential-repair profile remains editable and requires a newly entered key.
      await repair.locator('summary').click();
      await page.waitForFunction(id => state.personalModelExpanded[id] === true, ids[3]);
      assert.equal(await repair.getByText('缺少 API Key', { exact: true }).count(), 1);
      assert.equal(await repair.locator('[name="api_key"]').getAttribute('required'), '');
      await repair.locator('[name="api_key"]').fill('sk-browser-repaired');
      const repairSaved = page.waitForResponse(response =>
        response.url().endsWith('/api/me/models/' + ids[3]) && response.request().method() === 'PUT');
      await repair.locator('form[data-form="personal-model-edit"] [type="submit"]').click();
      const repairResponse = await repairSaved;
      assert.equal(repairResponse.status(), 200, await repairResponse.text());
      await page.locator('#toast').filter({ hasText: '个人模型配置已更新' }).waitFor();
      const repaired = page.locator('details[data-model-config][data-id="' + ids[3] + '"]');
      const repairedState = await (await context.request.get(site + '/api/me/models')).json();
      const repairedProfile = repairedState.models.find(profile => profile.id === ids[3]);
      assert.equal(repairedProfile.credential_status, 'ok', JSON.stringify(repairedProfile));
      assert.equal(await repaired.getByText('API Key 已保存（可解密）', { exact: true }).count(), 1,
        '修复后的状态标签未更新：' + await repaired.innerText());
      assert.equal(await repaired.locator('[data-action="test-personal-model"]').isEnabled(), true);

      // Long profile names wrap inside the summary instead of widening the card.
      await longName.locator('summary').click();
      await page.waitForFunction(id => state.personalModelExpanded[id] === true, ids[2]);
      await page.setViewportSize({ width: 390, height: 844 });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      const wrapStyle = await longName.locator('summary strong').evaluate(el => getComputedStyle(el).overflowWrap);
      assert.equal(wrapStyle, 'anywhere');
      await page.screenshot({ path: path.join(artifacts, 'model-config-mobile-light-expanded.png'), fullPage: true, animations: 'disabled' });

      // The same fold / expanded layout must remain readable in dark mode.
      await page.locator('#themeBtn').click();
      assert.equal(await page.locator('body').getAttribute('data-theme'), 'dark');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(artifacts, 'model-config-mobile-dark-expanded.png'), fullPage: true, animations: 'disabled' });
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.screenshot({ path: path.join(artifacts, 'model-config-desktop-dark-expanded.png'), fullPage: true, animations: 'disabled' });
      await page.locator('#themeBtn').click();
      assert.equal(await page.locator('body').getAttribute('data-theme'), 'light');
      await page.screenshot({ path: path.join(artifacts, 'model-config-desktop-light-expanded.png'), fullPage: true, animations: 'disabled' });

      // Delete remains available inside the expanded card.
      page.once('dialog', dialog => dialog.accept());
      await longName.locator('[data-action="delete-personal-model"]').click();
      await longName.waitFor({ state: 'detached' });
      const afterDelete = await (await context.request.get(site + '/api/me/models')).json();
      assert.equal(afterDelete.models.length, 3);

      // Logging out clears account-scoped drafts, expansion state and test results.
      await page.locator('#logoutBtn').click();
      await page.locator('form[data-form="login"]').waitFor();
      const cleared = await page.evaluate(() => ({
        drafts: state.personalModelDrafts,
        expanded: state.personalModelExpanded,
        tests: state.modelTestResults,
        pending: state.personalModelTestPending,
        versions: state.personalModelTestVersions,
        saving: state.personalModelSavePending,
      }));
      assert.deepEqual(cleared, { drafts: {}, expanded: {}, tests: {}, pending: {}, versions: {}, saving: {} });
    } else if (scenario === 'questions') {
      await page.getByRole('button', { name: '使用旧登录名', exact: true }).click();
      await page.locator('[name="username"]').fill('browser_admin');
      await page.locator('[name="password"]').fill('browser-password');
      await clickSubmit(page, 'login');
      await uidIs(page, 'TL00000001');
      await page.locator('form[data-form="room"] [name="title"]').fill('AI 裁判出题链路验收');
      const roomCreated = page.waitForResponse(response =>
        response.url().endsWith('/api/rooms') && response.request().method() === 'POST');
      await clickSubmit(page, 'room');
      const createdRoom = await roomCreated;
      assert.equal(createdRoom.status(), 200, await createdRoom.text());

      const roomInfo = await (await context.request.get(site + '/api/me')).json();
      const roomId = roomInfo.rooms[0].id;
      const base = '/api/rooms/' + roomId;
      const room = await (await context.request.get(site + base)).json();
      const profiles = await (await context.request.get(site + '/api/me/models')).json();
      const usableProfile = profiles.models.find(profile => profile.credential_status === 'ok');
      assert(usableProfile, '出题验收需要一个有效的测试模型配置');

      await page.locator('[data-action="room-view"][data-view="ai-judges"]').click();
      const addJudge = page.locator('form[data-form="add-ai-judge"]');
      await addJudge.waitFor();
      await addJudge.locator('[name="name"]').fill('自动出题 AI 裁判');
      await addJudge.locator('[name="model_profile_id"]').selectOption(String(usableProfile.id));
      await addJudge.locator('[type="submit"]').click();
      await page.locator('.feed-item').filter({ hasText: '自动出题 AI 裁判' }).waitFor();

      const addManualJudge = page.locator('form[data-form="add-ai-judge"]');
      await addManualJudge.locator('[name="name"]').fill('手动转录 AI 裁判');
      await addManualJudge.locator('[name="model_profile_id"]').selectOption('');
      await addManualJudge.locator('[type="submit"]').click();
      await page.locator('#app').getByText('手动转录 AI 裁判', { exact: false }).first().waitFor();

      assert.equal(await page.locator('[data-action="generate-ai-questions"]').isDisabled(), true);
      await page.locator('[data-action="room-view"][data-view="admin"]').click();
      await page.locator('[data-action="start-round"]').waitFor();
      assert.equal(await page.locator('[data-action="start-round"]').isDisabled(), true);
      assert((await page.locator('#app').innerText()).includes('当前仅有 0 位参赛者；至少需要两名在场参赛者才能开始统一作答。'));
      await screenshot(page, 'ai-question-zero-participants-blocked.png');

      const playerContexts = [];
      for (const username of ['question_human_a', 'question_human_b']) {
        const playerContext = await browser.newContext();
        playerContexts.push(playerContext);
        const joined = await playerContext.request.post(site + '/api/register', {
          data: { username, password: 'question-human-password', code: room.invite_code },
        });
        assert.equal(joined.status(), 200, await joined.text());
      }
      await page.reload();
      await page.waitForFunction(id => state.room?.id === id && state.room?.humans?.length === 2, roomId);
      await page.locator('[data-action="room-view"][data-view="admin"]').click();
      await page.waitForFunction(() => {
        const button = document.querySelector('[data-action="start-round"]');
        return button && !button.disabled;
      });

      // Entry point 1: starting the round must generate the bound judge's question first.
      page.once('dialog', dialog => dialog.accept());
      await page.locator('[data-action="start-round"]').click();
      await page.locator('#toast').filter({ hasText: '服务端开局任务已提交' }).waitFor();
      await page.waitForFunction(() => {
        const round = state.room?.rounds?.find(x => x.number === state.room.current_round);
        return round?.status === 'collecting' && round.questions?.some(q => q.text === 'OK');
      });
      const firstRound = await page.evaluate(() => state.room.rounds.find(x => x.number === state.room.current_round));
      assert.equal(firstRound.questions.filter(q => q.kind === 'ai').length, 1,
        'the bound AI judge should be generated by the server workflow before the round starts');
      await screenshot(page, 'ai-question-start-round-autogeneration.png');

      // Complete round 1, then open round 2 directly so the explicit generation button can be tested.
      for (let index = 0; index < playerContexts.length; index++) {
        const playerContext = playerContexts[index];
        const me = await (await playerContext.request.get(site + '/api/me')).json();
        const answer = await playerContext.request.post(site + base + '/answers', {
          data: { text: '第二阶段验收回答 ' + (index + 1) },
          headers: {
            'X-CSRF-Token': me.user.csrf,
            'X-Match-Number': String(room.match_number),
          },
        });
        assert.equal(answer.status(), 200, await answer.text());
      }
      const adminMe = await (await context.request.get(site + '/api/me')).json();
      const next = await context.request.post(site + '/api/rooms/' + roomId + '/next', {
        data: {},
        headers: {
          'X-CSRF-Token': adminMe.user.csrf,
          'X-Match-Number': String(room.match_number),
        },
      });
      assert.equal(next.status(), 200, await next.text());
      await page.reload();
      await page.waitForFunction(() => {
        const round = state.room?.rounds?.find(x => x.number === 2);
        return state.room?.current_round === 2 && round?.status === 'draft' &&
          round.questions?.some(q => q.text === 'OK');
      });
      await page.locator('[data-action="room-view"][data-view="ai-judges"]').click();

      // The next-round prepare-only workflow generates the bound AI judge's question
      // without opening the round; the unbound judge remains available for transcription.
      await page.locator('form[data-form="ai-judge-question"]').waitFor();
      await page.locator('form[data-form="ai-judge-question"] textarea[name="text"]').fill('第二轮手动转录问题。');
      const manualJudgeId = await page.locator('form[data-form="ai-judge-question"] select[name="judge_id"]').inputValue();
      const manualQuestionSaved = page.waitForResponse(response =>
        response.url().endsWith('/ai-judges/' + manualJudgeId + '/questions') && response.request().method() === 'POST');
      await page.locator('form[data-form="ai-judge-question"] [type="submit"]').click();
      const manualResponse = await manualQuestionSaved;
      assert.equal(manualResponse.status(), 200, await manualResponse.text());

      const finalRoom = await (await context.request.get(site + base)).json();
      const secondRound = finalRoom.rounds.find(round => round.number === 2);
      assert.equal(secondRound.questions.length, 2);
      assert(secondRound.questions.some(question => question.text === 'OK'));
      assert(secondRound.questions.some(question => question.text === '第二轮手动转录问题。'));
      await screenshot(page, 'ai-question-manual-generation-and-transcription.png');

      for (const playerContext of playerContexts) await playerContext.close();
    } else if (scenario === 'copy') {
      await page.getByRole('button', { name: '使用旧登录名', exact: true }).click();
      await page.locator('[name="username"]').fill('browser_admin');
      await page.locator('[name="password"]').fill('browser-password');
      await clickSubmit(page, 'login');
      await uidIs(page, 'TL00000001');
      await page.locator('form[data-form="room"] [name="title"]').fill('P2-P7 文案验收');
      const roomCreated = page.waitForResponse(response =>
        response.url().endsWith('/api/rooms') && response.request().method() === 'POST');
      await clickSubmit(page, 'room');
      const createdRoom = await roomCreated;
      assert.equal(createdRoom.status(), 200, await createdRoom.text());

      const roomInfo = await (await context.request.get(site + '/api/me')).json();
      const roomId = roomInfo.rooms[0].id;
      const base = '/api/rooms/' + roomId;
      const room = await (await context.request.get(site + base)).json();
      const profiles = await (await context.request.get(site + '/api/me/models')).json();
      assert.equal(profiles.model_api_enabled, true);
      const usableProfile = profiles.models.find(profile => profile.credential_status === 'ok');
      assert(usableProfile, '文案验收需要一个有效的测试模型配置');

      await page.locator('[data-action="room-view"][data-view="admin"]').click();
      const addAi = page.locator('form[data-form="add-ai"]');
      await addAi.waitFor();
      assert.equal(await addAi.locator('[name="model_profile_id"] option[value="' + usableProfile.id + '"]').count(), 1);
      assert.equal(await addAi.locator('.hint').count(), 0, 'AI 参赛位模型选择框下不应再显示授权 / 计费说明');
      const initialAdminText = await page.locator('#app').innerText();
      for (const phrase of [
        '模型接入的 AI 参赛者可自动回答；AI 裁判可自动出题和判断。未接入模型的 AI 席位仍可手动转录。',
        '可以添加手动 AI 席位，也可以先在个人中心配置自己的模型 API。',
        '选择模型配置即授权本房间房主和全站管理员通过比赛操作触发调用；服务商可能按实际调用量计费。',
        '房主也可使用邀请码加入，以人类身份回答。',
        '点击轮次可查看历史记录。',
        '查看本轮问题与回答',
        '查看真人裁判名单',
        '配置 AI 裁判席位',
        '管理参赛者与比赛',
        '管理名字、密码和个人模型 API',
      ]) assert.equal(initialAdminText.includes(phrase), false, '不应显示已删除文案：' + phrase);
      const navButtons = await page.locator('.room-view-menu [data-action="room-view"]').all();
      assert.equal(navButtons.length, 4);
      for (let index = 0; index < navButtons.length; index++) {
        const expected = ['比赛现场', '真人裁判管理', 'AI 裁判管理', '参赛者管理'][index];
        assert.equal((await navButtons[index].innerText()).trim(), expected);
      }
      await screenshot(page, 'copy-cleanup-admin-desktop-light.png');

      await addAi.locator('[name="name"]').fill('文案验收 AI 参赛者');
      await addAi.locator('[name="model_profile_id"]').selectOption(String(usableProfile.id));
      await addAi.locator('[type="submit"]').click();
      await page.locator('.roster-person').filter({ hasText: '文案验收 AI 参赛者' }).waitFor();
      const playerBindingText = await page.locator('#app').innerText();
      assert.equal(playerBindingText.includes('绑定模型配置后，本房间房主和全站管理员可通过比赛操作触发该配置的模型调用，服务商可能按实际调用量计费。'), false);
      assert(playerBindingText.includes('更换或解除绑定不会删除 AI 席位及其匿名代号、历史回答。'));

      await page.locator('[data-action="room-view"][data-view="human-judges"]').click();
      await page.getByRole('heading', { name: '真人裁判管理', exact: true }).waitFor();
      await page.getByText('0 位真人裁判', { exact: true }).waitFor();
      const humanJudgeText = await page.locator('#app').innerText();
      assert.equal(humanJudgeText.includes('尚无真人裁判。'), false);
      assert.equal(humanJudgeText.includes('真人裁判自行注册并使用 UID 登录，再从大厅输入本房间邀请码，选择“真人裁判”身份加入。房主不创建账号，也不绑定已有账号。'), false);
      assert.equal(humanJudgeText.includes('同一 UID 同时只能担任一个未完赛房间的真人裁判。裁判加入、出题和独立判断流程保持不变。'), false);
      await screenshot(page, 'copy-cleanup-empty-human-judges-desktop-light.png');

      await page.locator('[data-action="room-view"][data-view="ai-judges"]').click();
      const addJudge = page.locator('form[data-form="add-ai-judge"]');
      await addJudge.waitFor();
      await addJudge.locator('[name="name"]').fill('文案验收 AI 裁判');
      await addJudge.locator('[type="submit"]').click();
      await page.locator('.feed-item').filter({ hasText: '文案验收 AI 裁判' }).waitFor();
      await screenshot(page, 'copy-cleanup-ai-judges-desktop-light.png');

      await page.locator('[data-action="room-view"][data-view="admin"]').click();
      await page.getByRole('heading', { name: '统一作答与揭晓', exact: true }).waitFor();
      const workflowText = await page.locator('#app').innerText();
      for (const phrase of [
        '仍需完成全部轮次。',
        '等待所有裁判锁定最终判断。',
        '所有轮次已完成。',
        '所有裁判均已锁定判断。',
        '所有真人 / AI 裁判的问题会统一汇总；由房主在此开启作答。',
      ]) assert.equal(workflowText.includes(phrase), false, '不应显示已删除文案：' + phrase);
      assert.equal(await page.locator('[data-action="start-round"]').isDisabled(), true,
        '人数不足时必须保留统一作答的实际限制');
      assert(workflowText.includes('当前仅有 1 位参赛者；至少需要两名在场参赛者才能开始统一作答。'));
      assert.equal(await page.locator('[data-action="admin-join"]').count(), 1,
        '参赛者管理中的实际参赛入口必须保留');
      await screenshot(page, 'copy-cleanup-workflow-desktop-light.png');

      await page.locator('[data-action="room-view"][data-view="match"]').click();
      const matchText = await page.locator('#app').innerText();
      assert.equal(matchText.includes('人类玩家互相可见真实账号身份；裁判包仅含匿名代号。'), false);
      assert.equal(matchText.includes('房主若希望亲自参赛，请在右侧点击“作为人类玩家加入”（仅开局前）。'), false);
      assert.equal(matchText.includes('在参赛者管理选择以人类身份加入。'), true);
      assert.equal(matchText.includes('点击轮次可查看历史记录。'), false);
      assert.equal(await page.locator('.roster-person').count(), 1, '精简说明不能删除参赛者身份卡片');
      await screenshot(page, 'copy-cleanup-match-desktop-light.png');
    } else if (scenario === 'personas') {
      await page.getByRole('button', { name: '使用旧登录名', exact: true }).click();
      await page.locator('[name="username"]').fill('browser_admin');
      await page.locator('[name="password"]').fill('browser-password');
      await clickSubmit(page, 'login');
      await uidIs(page, 'TL00000001');
      await page.locator('form[data-form="room"] [name="title"]').fill('AI 席位人设浏览器验收');
      const roomCreated = page.waitForResponse(response =>
        response.url().endsWith('/api/rooms') && response.request().method() === 'POST');
      await clickSubmit(page, 'room');
      const createdRoom = await roomCreated;
      assert.equal(createdRoom.status(), 200, await createdRoom.text());

      const baseInfo = await (await context.request.get(site + '/api/me')).json();
      const roomId = baseInfo.rooms[0].id;
      const base = '/api/rooms/' + roomId;
      const room = await (await context.request.get(site + base)).json();
      const models = await (await context.request.get(site + '/api/me/models')).json();
      const profile = models.models.find(item => item.credential_status === 'ok');
      assert(profile, '人设浏览器验收需要有效的模型配置');
      const headers = { 'X-CSRF-Token': baseInfo.user.csrf };
      const addAI = await context.request.post(site + base + '/ai', {
        headers, data: { name: 'Persona Participant', model_profile_id: profile.id },
      });
      assert.equal(addAI.status(), 200, await addAI.text());
      const addJudge = await context.request.post(site + base + '/ai-judges', {
        headers, data: { name: 'Persona Judge', model_profile_id: profile.id },
      });
      const judgeSeat = await addJudge.json();
      assert.equal(addJudge.status(), 200, JSON.stringify(judgeSeat));

      const playerContexts = [];
      for (const username of ['persona_ui_human_a', 'persona_ui_human_b']) {
        const playerContext = await browser.newContext();
        playerContexts.push(playerContext);
        const joined = await playerContext.request.post(site + '/api/register', {
          data: { username, display_name: username, password: 'persona-ui-password', code: room.invite_code },
        });
        assert.equal(joined.status(), 200, await joined.text());
      }

      const judgeContext = await browser.newContext();
      const judgeRegistered = await judgeContext.request.post(site + '/api/register', {
        data: { username: 'persona_ui_human_judge', display_name: '人设验收裁判',
          password: 'persona-ui-password' },
      });
      assert.equal(judgeRegistered.status(), 200, await judgeRegistered.text());
      const judgeAccount = await (await judgeContext.request.get(site + '/api/me')).json();
      const judgeJoin = await judgeContext.request.post(site + '/api/join', {
        headers: { 'X-CSRF-Token': judgeAccount.user.csrf },
        data: { code: room.invite_code, role: 'judge' },
      });
      assert.equal(judgeJoin.status(), 200, await judgeJoin.text());
      const judgePage = await judgeContext.newPage();
      await judgePage.goto(site + '#room=' + roomId);
      await judgePage.locator('h1').waitFor();
      assert.equal(await judgePage.getByRole('heading', {
        name: '本场 AI 席位人设', exact: true,
      }).count(), 0, '正式揭晓前真人裁判界面不得展示 AI 人设');

      await page.locator('[data-action="sync"]').click();
      await page.locator('[data-action="room-view"][data-view="admin"]').click();
      const participantDetails = page.locator(
        'details.persona-editor:has(form[data-form="seat-persona"][data-seat-type="participant"])');
      await participantDetails.evaluate(element => { element.open = true; });
      const participantPersona = participantDetails.locator('form[data-form="seat-persona"]');
      await participantPersona.waitFor();
      assert.equal(await participantPersona.getByText('回答历史轮数（0～100）', { exact: true }).count(), 1);
      await participantPersona.locator('[name="preset_id"]').selectOption('custom');
      await participantPersona.locator('[name="custom_system_prompt"]').fill('临时自定义风格');
      await participantPersona.locator('[name="preset_id"]').selectOption('analytical');
      assert.equal(await participantPersona.locator('[name="custom_system_prompt"]').inputValue(), '',
        '切换到预设人设时应清空不适用的自定义提示词');
      await participantPersona.locator('[name="output_length"]').selectOption('medium');
      await participantPersona.locator('[name="history_rounds"]').fill('12');
      const participantSaved = page.waitForResponse(response =>
        response.url().endsWith('/persona') && response.request().method() === 'PUT');
      await participantPersona.locator('[type="submit"]').click();
      const participantSaveResponse = await participantSaved;
      assert.equal(participantSaveResponse.status(), 200, await participantSaveResponse.text());

      await page.locator('[data-action="room-view"][data-view="ai-judges"]').click();
      const judgeDetails = page.locator(
        'details.persona-editor:has(form[data-form="seat-persona"][data-seat-type="judge"])');
      await judgeDetails.evaluate(element => { element.open = true; });
      const judgePersona = judgeDetails.locator('form[data-form="seat-persona"]');
      await judgePersona.waitFor();
      assert.equal(await judgePersona.getByText('出题历史轮数（0～100）', { exact: true }).count(), 1);
      await judgePersona.locator('[name="preset_id"]').selectOption('language_detail');
      await judgePersona.locator('[name="output_length"]').selectOption('short');
      await judgePersona.locator('[name="history_rounds"]').fill('0');
      const judgeSaved = page.waitForResponse(response =>
        response.url().endsWith('/persona') && response.request().method() === 'PUT');
      await judgePersona.locator('[type="submit"]').click();
      const judgeSaveResponse = await judgeSaved;
      assert.equal(judgeSaveResponse.status(), 200, await judgeSaveResponse.text());

      const generate = page.locator('[data-action="generate-ai-questions"]');
      await generate.waitFor();
      const generated = page.waitForResponse(response =>
        response.url().endsWith('/ai-judges/questions/generate') && response.request().method() === 'POST');
      await generate.click();
      const generatedResponse = await generated;
      assert.equal(generatedResponse.status(), 200, await generatedResponse.text());
      let frozenRoom = await (await context.request.get(site + base)).json();
      assert.equal(frozenRoom.personas_frozen, true);
      assert.equal(frozenRoom.ais[0].persona.preset_id, 'analytical');
      assert.equal(frozenRoom.judges.find(item => item.kind === 'ai').persona.preset_id, 'language_detail');
      await page.waitForFunction(() => {
        const field = document.querySelector('form[data-form="seat-persona"][data-seat-type="judge"] [name="preset_id"]');
        return field && field.disabled;
      });
      assert.equal(await judgePersona.locator('[name="preset_id"]').isDisabled(), true);

      await page.locator('[data-action="room-view"][data-view="admin"]').click();
      const resetButton = page.locator('[data-action="reset-persona-preparation"]');
      await resetButton.waitFor();
      assert.equal(await participantPersona.locator('[name="preset_id"]').isDisabled(), true);
      page.once('dialog', dialog => dialog.accept());
      const reset = page.waitForResponse(response =>
        response.url().endsWith('/personas/reset-preparation') && response.request().method() === 'POST');
      await resetButton.click();
      const resetResponse = await reset;
      assert.equal(resetResponse.status(), 200, await resetResponse.text());
      frozenRoom = await (await context.request.get(site + base)).json();
      assert.equal(frozenRoom.personas_frozen, false);
      assert.equal(frozenRoom.rounds[0].questions.length, 0);
      await page.waitForFunction(() => {
        const field = document.querySelector('form[data-form="seat-persona"][data-seat-type="participant"] [name="preset_id"]');
        return field && !field.disabled;
      });
      assert.equal(await participantDetails.evaluate(element => element.open), true,
        '取消准备和房间刷新后应保留已展开的人设编辑器');
      await participantPersona.locator('[name="preset_id"]').waitFor({ state: 'visible' });
      assert.equal(await participantPersona.locator('[name="preset_id"]').isDisabled(), false);

      await participantPersona.locator('[name="preset_id"]').selectOption('custom');
      const customStyle = '用简短自然的口语直接回应，不使用分点列表。<img src=x onerror=alert(1)>';
      await participantPersona.locator('[name="custom_system_prompt"]').fill(customStyle);
      const customSaved = page.waitForResponse(response =>
        response.url().endsWith('/persona') && response.request().method() === 'PUT');
      await participantPersona.locator('[type="submit"]').click();
      const customResponse = await customSaved;
      assert.equal(customResponse.status(), 200, await customResponse.text());
      const finalRoom = await (await context.request.get(site + base)).json();
      assert.equal(finalRoom.ais[0].persona.preset_id, 'custom');
      assert.equal(finalRoom.ais[0].persona.custom_system_prompt, customStyle);

      // Complete the match and verify that a human judge sees the frozen
      // persona details only after formal reveal.
      const manualQuestion = await context.request.post(
        site + base + `/ai-judges/${judgeSeat.id}/questions`,
        { headers, data: { text: '请描述一次你修正错误判断的经历。' } });
      assert.equal(manualQuestion.status(), 200, await manualQuestion.text());
      const started = await context.request.post(site + base + '/start', { headers });
      assert.equal(started.status(), 200, await started.text());
      const liveRoom = await (await context.request.get(site + base)).json();
      for (let i = 0; i < playerContexts.length; i++) {
        const playerMe = await (await playerContexts[i].request.get(site + '/api/me')).json();
        const answer = await playerContexts[i].request.post(site + base + '/answers', {
          headers: { 'X-CSRF-Token': playerMe.user.csrf },
          data: { text: '这是第 ' + (i + 1) + ' 位人类参赛者的回答。' },
        });
        assert.equal(answer.status(), 200, await answer.text());
      }
      const aiAnswer = await context.request.post(site + base + '/ai/answers/generate', { headers });
      assert.equal(aiAnswer.status(), 200, await aiAnswer.text());
      const aliases = [...liveRoom.humans.map(item => item.alias), ...liveRoom.ais.map(item => item.alias)];
      const predictions = Object.fromEntries(aliases.map((alias, index) =>
        [alias, index === 0 ? 'Human' : 'AI']));
      const judgeVerdict = await judgeContext.request.post(site + base + '/judge-verdict', {
        headers: { 'X-CSRF-Token': judgeAccount.user.csrf },
        data: { predictions, evidence: '依据匿名回答作出判断。', confidence: 80 },
      });
      assert.equal(judgeVerdict.status(), 200, await judgeVerdict.text());
      const aiVerdict = await context.request.post(
        site + base + `/ai-judges/${judgeSeat.id}/verdict`,
        { headers, data: { predictions, evidence: '依据匿名回答作出判断。', confidence: 80 } });
      assert.equal(aiVerdict.status(), 200, await aiVerdict.text());
      const revealed = await context.request.post(site + base + '/reveal', { headers });
      assert.equal(revealed.status(), 200, await revealed.text());

      await judgePage.reload();
      await judgePage.getByRole('heading', { name: '本场 AI 席位人设', exact: true }).waitFor();
      const revealedJudgeText = await judgePage.locator('#app').innerText();
      assert(revealedJudgeText.includes(customStyle));
      assert(revealedJudgeText.includes(profile.model), '正式揭晓后应展示冻结的模型名称');
      assert(revealedJudgeText.includes('回答历史轮数：12'));
      assert(revealedJudgeText.includes('出题历史轮数：0'));
      assert.equal(revealedJudgeText.includes('api.example.com'), false);
      assert.equal(revealedJudgeText.includes('sk-browser-'), false);
      assert.equal(await judgePage.locator('#app img').count(), 0,
        '自定义 System Prompt 必须作为文本展示，不能执行 HTML');
      await judgePage.close();
      await judgeContext.close();

      await screenshot(page, 'ai-seat-personas-desktop-light.png');
      await page.setViewportSize({ width: 390, height: 844 });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await screenshot(page, 'ai-seat-personas-mobile-light.png');
      for (const playerContext of playerContexts) await playerContext.close();
    } else {
      throw new Error('Unknown scenario: ' + scenario);
    }
    assert.deepEqual(errors, []);
    console.log('Browser acceptance passed: ' + scenario);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
