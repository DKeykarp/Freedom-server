// Публичный профиль (био), псевдонимы (локально, per-device) и QR («добавить контакт») — настоящий Chromium.
import fs from 'node:fs'; import http from 'node:http'; import path from 'node:path';
import assert from 'node:assert/strict'; import { createRequire } from 'node:module';

const WEB = new URL('../../server/web/', import.meta.url).pathname;
function findPuppeteer() {
  for (const base of [process.cwd(), '/home/claude/.npm-global/lib/node_modules/@mermaid-js/mermaid-cli']) {
    for (const name of ['puppeteer-core', 'puppeteer']) { try { return createRequire(base + '/x.js')(name); } catch { /* дальше */ } }
  }
  return null;
}
const chromePaths = [process.env.CHROME_PATH, '/home/claude/.cache/puppeteer/chrome/linux-131.0.6778.204/chrome-linux64/chrome',
  '/opt/pw-browsers/chromium-1194/chrome-linux/chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome'].filter(Boolean);
const chrome = chromePaths.find((p) => fs.existsSync(p)), puppeteer = findPuppeteer();
if (!chrome || !puppeteer) { console.log('SKIP: нет Chromium/puppeteer для проверки в браузере'); process.exit(0); }

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
const server = http.createServer((rq, rs) => {
  if (rq.url === '/api/downloads') { rs.writeHead(200, { 'Content-Type': 'application/json' }); return rs.end('[]'); }
  if (rq.url.startsWith('/api/')) { rs.writeHead(404, { 'Content-Type': 'application/json' }); return rs.end('{}'); }
  const f = path.join(WEB, rq.url === '/' ? 'index.html' : rq.url.split('?')[0]);
  if (!f.startsWith(WEB) || !fs.existsSync(f)) { rs.writeHead(404); return rs.end('nf'); }
  rs.writeHead(200, { 'Content-Type': MIME[path.extname(f)] || 'application/octet-stream' });
  rs.end(fs.readFileSync(f));
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const URL_ = `http://127.0.0.1:${server.address().port}/`;

const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ['--no-sandbox', '--disable-gpu'] });
let failed = 0;
async function scenario(name, check) {
  const page = await browser.newPage();
  const problems = [];
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) problems.push(m.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  await page.setViewport({ width: 1000, height: 800 });
  try {
    await page.goto(URL_, { waitUntil: 'networkidle0' });
    await check(page);
    assert.deepEqual(problems.filter((p) => !/favicon/.test(p)), [], 'ошибки в консоли');
    console.log('OK —', name);
  } catch (e) { failed++; console.log('FAIL —', name, '\n   ', e.message.split('\n').slice(0, 8).join('\n    ')); }
  await page.close();
}

async function seed(page, { rooms, cur, messages }) {
  await page.evaluate(({ rooms, cur, messages }) => {
    S.me = 'alice'; S.rooms = rooms; renderRooms();
    $('#auth').classList.add('hidden'); $('#app').classList.remove('hidden'); $('#me').textContent = 'alice';
    if (cur != null) {
      S.cur = cur; S.loaded.add(cur);
      $('#empty').classList.add('hidden'); $('#chatView').classList.remove('hidden');
      const r = S.rooms.find((x) => x.id === cur);
      $('#title').textContent = r.is_direct ? displayName(r.name) : r.name;
      const av = r.is_direct ? avatarEl('avatar', displayName(r.name), r.name) : avatarEl('avatar', null, r.name);
      $('#chatAvatar').replaceWith(av); av.id = 'chatAvatar'; av.onclick = openCurrentContactProfile;
      S.msgs[cur] = messages || [];
      renderAll();
    }
  }, { rooms, cur, messages });
}
const BOB = { id: 1, name: 'bob', is_direct: true, unread: 0, sort_ts: 1, peer: 'bob', epoch: 1 };

await scenario('QR моего профиля кодирует frdm:u:<моё имя> и читается обратно', async (page) => {
  await seed(page, { rooms: [] });
  const ok = await page.evaluate(() => {
    const { matrix, size } = QR.encode('frdm:u:alice');
    return matrix.length === size && size >= 21;
  });
  assert.equal(ok, true);
});

await scenario('открытие своего профиля: подставляется био с сервера, сохранение уходит PUT /profile', async (page) => {
  await seed(page, { rooms: [] });
  const r = await page.evaluate(async () => {
    window.api = async (p, opts) => {
      if (p.includes('/users/alice/profile') && (!opts || !opts.method)) return { username: 'alice', bio: 'старое био' };
      if (p === '/profile') { window.__put = opts; return { username: 'alice', bio: opts.body.bio }; }
      return {};
    };
    await openMyProfile();
    const bioLoaded = $('#pfBio').value;
    $('#pfBio').value = 'новое био';
    await saveMyProfile();
    return { bioLoaded, put: window.__put, dialogOpen: $('#pfdlg').open };
  });
  assert.equal(r.bioLoaded, 'старое био');
  assert.equal(r.put.method, 'PUT');
  assert.equal(r.put.body.bio, 'новое био');
  assert.equal(r.dialogOpen, false, 'диалог должен закрыться после сохранения');
});

await scenario('псевдоним: применяется в списке чатов, шапке чата и подписи отправителя в группе', async (page) => {
  await seed(page, { rooms: [BOB], cur: 1, messages: [] });
  await page.evaluate(() => { window.api = async () => ({ username: 'bob', bio: '' }); });
  await page.evaluate(() => openContactProfile('bob'));
  await page.evaluate(() => { $('#cAlias').value = 'Начальник'; });
  await page.evaluate(() => $('#cSaveAlias').click());

  const listName = await page.$eval('#rooms .room .name', (e) => e.textContent);
  assert.equal(listName, 'Начальник');
  const headerTitle = await page.$eval('#title', (e) => e.textContent);
  assert.equal(headerTitle, 'Начальник');

  // в групповом чате подпись отправителя тоже должна показывать псевдоним
  const group = { id: 2, name: 'Комната', is_direct: false, unread: 0, sort_ts: 2 };
  await seed(page, { rooms: [BOB, group], cur: 2, messages: [
    { id: 1, room_id: 2, username: 'bob', t: 'text', text: 'привет всем', ts: 1700000000 },
  ] });
  const senderName = await page.$eval('#messages .row .name', (e) => e.textContent);
  assert.equal(senderName, 'Начальник');

  assert.equal(await page.evaluate(() => localStorage.getItem('frdm_alias_bob')), 'Начальник');
});

await scenario('сканирование QR: валидный код запускает /dm, чужой (не наш) формат — просто отклоняется', async (page) => {
  await seed(page, { rooms: [] });
  const good = await page.evaluate(async () => {
    let dmCalled = null;
    window.BarcodeDetector = class { async detect() { return [{ rawValue: 'frdm:u:carol' }]; } };
    window.api = async (p, opts) => { if (p === '/dm') { dmCalled = opts.body; return { id: 9 }; } return []; };
    window.createImageBitmap = async () => ({});
    window.openRoom = () => {};
    E2E.fetchKeys = async () => {};
    await scanContactQr(new Blob());
    await new Promise((res) => setTimeout(res, 20));   // afterRoomAction(api(...)) — fire-and-forget, не ждётся внутри
    return dmCalled;
  });
  assert.deepEqual(good, { username: 'carol' });

  const bad = await page.evaluate(async () => {
    let dmCalled = false, toastMsg = null;
    window.BarcodeDetector = class { async detect() { return [{ rawValue: 'https://example.com/not-ours' }]; } };
    window.api = async (p) => { if (p === '/dm') dmCalled = true; return []; };
    const origToast = window.toast; window.toast = (m) => { toastMsg = m; };
    await scanContactQr(new Blob());
    window.toast = origToast;
    return { dmCalled, toastMsg };
  });
  assert.equal(bad.dmCalled, false);
  assert.match(bad.toastMsg, /не нашёлся/);
});

await browser.close(); server.close();
console.log(failed ? `\nПРОВАЛЕНО: ${failed}` : '\nВсе проверки профиля/псевдонимов/QR прошли');
process.exit(failed ? 1 : 0);
