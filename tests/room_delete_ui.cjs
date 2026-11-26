const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require('playwright-core');
const site = process.argv[2];
async function call(context, route, data, method = 'POST') {
  const me = await (await context.request.get(site + '/api/me')).json();
  const result = await context.request.fetch(site + route, { method, data,
    headers: me.user ? { 'X-CSRF-Token':me.user.csrf } : {} });
  assert.equal(result.status(), 200, await result.text());
  return result.json();
}
async function shot(page, name) {
  await page.screenshot({ path:path.resolve(__dirname, '../artifacts', name), fullPage:true, animations:'disabled' });
}
(async () => {
  const browserOptions = { headless: true };
  if (process.env.PLAYWRIGHT_CHANNEL !== 'default') browserOptions.channel = process.env.PLAYWRIGHT_CHANNEL || 'msedge';
  const browser = await chromium.launch(browserOptions);
  try {
    const host = await browser.newContext({ viewport:{width:1440,height:1000} });
    const player = await browser.newContext();
    const other = await browser.newContext();
    const admin = await browser.newContext();
    await call(host, '/api/register', {username:'房主',password:'host-password'});
    await call(player, '/api/register', {username:'玩家',password:'user-password'});
    await call(other, '/api/register', {username:'其他房主',password:'user-password'});
    await call(admin, '/api/login', {uid:'TL00000001',password:'browser-password'});
    const room = await call(host, '/api/rooms', {title:'快照折叠与删除验收'});
    const another = await call(other, '/api/rooms', {title:'其他用户的房间'});
    const base = `/api/rooms/${room.room_id}`;
    await call(host, '/api/join', {code:another.code});
    await call(player, '/api/join', {code:room.code});
    await call(host, base + '/ai', {name:'手工AI'});
    await call(host, base + '/question', {text:'折叠不会改变快照原文'});
    await call(player, base + '/answers', {text:'匿名回答原文'});
    await call(host, base + '/ai/B/answer', {text:'AI原文'});
    await call(host, base + '/packets', {});
    await call(host, base + '/packets', {});
    const page = await host.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    let accept = true;
    page.on('dialog', dialog => accept ? dialog.accept() : dialog.dismiss());
    await page.goto(site + `/#room=${room.room_id}`);
    await page.locator('#judgeNotice[open]').waitFor();
    await page.locator('#judgeNoticeClose').click();
    await page.locator('[data-action="room-view"][data-view="admin"]').click();
    const panels = page.locator('details[data-packet-panel]');
    await panels.waitFor();
    assert.equal(await panels.getAttribute('open'), null);
    await panels.locator('summary').focus();
    await page.keyboard.press('Enter');
    await panels.locator('[data-action="mark-sent"]').first().waitFor();
    await panels.locator('[data-action="mark-sent"]').first().click();
    await panels.getByText('已确认发送', {exact:true}).waitFor();
    assert.notEqual(await panels.getAttribute('open'), null);
    const popupPromise = page.waitForEvent('popup');
    await panels.locator('[data-action="view-packet"]').first().click();
    const popup = await popupPromise;
    await popup.locator('pre').waitFor();
    assert((await popup.locator('pre').textContent()).includes('匿名回答原文'));
    await popup.close();
    await shot(page, 'packets-expanded-desktop.png');
    await panels.locator('summary').click();
    await page.locator('[data-action="sync"]').click();
    assert.equal(await panels.getAttribute('open'), null);
    await panels.locator('summary').click();
    await page.locator('[data-action="room-view"][data-view="match"]').click();
    await page.locator('[data-action="room-view"][data-view="admin"]').click();
    assert.notEqual(await panels.getAttribute('open'), null);
    await panels.locator('summary').click();
    await page.setViewportSize({width:390,height:844});
    await page.getByRole('button', {name:'深色',exact:false}).click();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await shot(page, 'packets-collapsed-mobile-dark.png');
    // Returning to the lobby now closes an unfinished owner's room. Finish first
    // so this scenario can continue to test clearing completed records.
    await call(host, base + '/verdict', {predictions:{A:'Human',B:'AI'},evidence:'依据',confidence:50});
    await call(host, base + '/reveal', {});
    await page.locator('[data-action="back"]').click();
    const eraseRoom = page.locator(`[data-action="delete-room"][data-id="${room.room_id}"]`);
    await eraseRoom.waitFor();
    assert.equal(await page.locator(`[data-action="delete-room"][data-id="${another.room_id}"]`).count(), 0);
    accept = false;
    await eraseRoom.click();
    assert.equal((await host.request.get(site + base)).status(), 200);
    accept = true;
    await shot(page, 'room-delete-lobby-mobile-dark.png');
    await page.reload();
    await page.locator(`[data-action="delete-records"][data-id="${room.room_id}"]`).click();
    await page.getByRole('heading', {name:'已完赛房间记录',exact:true}).waitFor({state:'detached'});
    assert.equal(await page.getByText('记录已清空', {exact:false}).count(), 0);
    const playerPage = await player.newPage();
    playerPage.on('pageerror', error => errors.push(String(error)));
    await playerPage.goto(site + `/#room=${room.room_id}`);
    await playerPage.getByRole('heading', {name:'等待再次开赛',exact:true}).waitFor();
    await eraseRoom.click();
    await eraseRoom.waitFor({state:'detached'});
    await playerPage.locator('form[data-form="room"]').waitFor();
    assert.equal(await playerPage.evaluate(() => location.hash), '');
    assert.equal((await player.request.get(site + base)).status(), 404);
    const otherPage = await other.newPage();
    otherPage.on('pageerror', error => errors.push(String(error)));
    await otherPage.goto(site + `/#room=${another.room_id}`);
    await otherPage.locator('[data-action="room-view"]').first().waitFor();
    const adminPage = await admin.newPage();
    adminPage.on('pageerror', error => errors.push(String(error)));
    adminPage.on('dialog', dialog => dialog.accept());
    await adminPage.goto(site);
    await adminPage.locator(`[data-action="delete-room"][data-id="${another.room_id}"]`).click();
    await otherPage.locator('form[data-form="room"]').waitFor();
    assert.equal((await other.request.get(site + `/api/rooms/${another.room_id}`)).status(), 404);
    assert.deepEqual(errors, []);
    console.log('Room deletion, hidden cleared records and accessible packet folding passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

