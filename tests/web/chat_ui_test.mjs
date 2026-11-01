// Дизайн интерфейса чата в настоящем Chromium: аватары, группировка сообщений, «хвостики», поиск, composer.
// Проверяет настоящий app.js (через реальные функции renderRooms/renderAll/msgEl), не переписанную копию.
// Без Chromium/puppeteer проверка пропускается — как и в browser_test.mjs.
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
  await page.setViewport({ width: 1280, height: 900 });
  try {
    await page.goto(URL_, { waitUntil: 'networkidle0' });
    await check(page);
    assert.deepEqual(problems.filter((p) => !/favicon/.test(p)), [], 'ошибки в консоли');
    console.log('OK —', name);
  } catch (e) { failed++; console.log('FAIL —', name, '\n   ', e.message.split('\n').slice(0, 6).join('\n    ')); }
  await page.close();
}

/** Заводит комнаты/сообщения напрямую в состоянии приложения (без сети и E2EE) и рендерит их настоящими функциями. */
async function seed(page, { rooms, cur, messages }) {
  await page.evaluate(({ rooms, cur, messages }) => {
    S.me = 'alice';
    S.rooms = rooms;
    renderRooms();
    $('#auth').classList.add('hidden'); $('#app').classList.remove('hidden');
    if (cur != null) {
      S.cur = cur;
      S.loaded.add(cur);
      $('#empty').classList.add('hidden'); $('#chatView').classList.remove('hidden');
      const r = S.rooms.find((x) => x.id === cur);
      $('#title').textContent = r.name;
      const av = r.is_direct ? avatarEl('avatar', r.name, r.name) : avatarEl('avatar', null, r.name);
      $('#chatAvatar').replaceWith(av); av.id = 'chatAvatar';
      S.msgs[cur] = messages;
      renderAll();
    }
  }, { rooms, cur, messages });
}

const DAY = 86400;
await scenario('аватары: инициалы для личных чатов, «#» для комнат, разные градиенты', async (page) => {
  await seed(page, { rooms: [
    { id: 1, name: 'bob', is_direct: true, unread: 0, sort_ts: 1 },
    { id: 2, name: 'Комната', is_direct: false, unread: 0, sort_ts: 2 },
  ] });
  const info = await page.$$eval('#rooms .avatar', (els) => els.map((e) => ({ text: e.textContent, bg: getComputedStyle(e).backgroundImage })));
  assert.equal(info[0].text, 'B');
  assert.equal(info[1].text, '#');
  assert.ok(info[0].bg.includes('gradient') && info[1].bg.includes('gradient'));
  assert.notEqual(info[0].bg, info[1].bg, 'аватары разных чатов должны получать разный цвет');
});

await scenario('поиск фильтрует список чатов вживую', async (page) => {
  await seed(page, { rooms: [
    { id: 1, name: 'bob', is_direct: true, unread: 0, sort_ts: 1 },
    { id: 2, name: 'Команда разработки', is_direct: false, unread: 0, sort_ts: 2 },
    { id: 3, name: 'dave', is_direct: true, unread: 0, sort_ts: 3 },
  ] });
  const visible = async () => page.$$eval('#rooms .room', (els) => els.filter((e) => !e.classList.contains('hide')).length);
  assert.equal(await visible(), 3);
  await page.type('#roomSearch', 'команда');
  assert.equal(await visible(), 1);
  await page.$eval('#roomSearch', (i) => { i.value = ''; i.dispatchEvent(new Event('input')); });
  assert.equal(await visible(), 3);
});

await scenario('группировка: аватар/имя только у первого сообщения пачки, «хвостик» — у последнего', async (page) => {
  const now = 1_700_000_000;
  await seed(page, {
    rooms: [{ id: 5, name: 'Комната', is_direct: false, unread: 0, sort_ts: 1 }], cur: 5,
    messages: [
      { id: 1, room_id: 5, username: 'bob', t: 'text', text: 'первое', ts: now },
      { id: 2, room_id: 5, username: 'bob', t: 'text', text: 'второе', ts: now + 5 },
      { id: 3, room_id: 5, username: 'carol', t: 'text', text: 'третье, другой автор', ts: now + 10 },
    ],
  });
  const rows = await page.$$eval('#messages .row', (els) => els.map((e) => ({
    grouped: e.classList.contains('grouped'), tail: e.classList.contains('tail'),
    hasAvatar: !!e.querySelector('.avatarSlot .avatar'), hasSlot: !!e.querySelector('.avatarSlot'),
    hasName: !!e.querySelector('.bubble .name'),
  })));
  assert.equal(rows.length, 3);
  assert.deepEqual(rows[0], { grouped: false, tail: false, hasAvatar: true, hasSlot: true, hasName: true }, 'первое: аватар и имя есть, хвостика нет');
  assert.deepEqual(rows[1], { grouped: true, tail: true, hasAvatar: false, hasSlot: true, hasName: false }, 'второе: без аватара/имени, но с хвостиком (конец пачки bob)');
  assert.deepEqual(rows[2], { grouped: false, tail: true, hasAvatar: true, hasSlot: true, hasName: true }, 'третье: новый автор — снова аватар, имя и хвостик');
});

await scenario('личный чат: аватары и имена у сообщений не показываются вовсе', async (page) => {
  const now = 1_700_000_000;
  await seed(page, {
    rooms: [{ id: 6, name: 'bob', is_direct: true, unread: 0, sort_ts: 1 }], cur: 6,
    messages: [{ id: 1, room_id: 6, username: 'bob', t: 'text', text: 'привет', ts: now }],
  });
  const row = await page.$eval('#messages .row', (e) => ({ hasSlot: !!e.querySelector('.avatarSlot'), hasName: !!e.querySelector('.bubble .name') }));
  assert.deepEqual(row, { hasSlot: false, hasName: false });
});

await scenario('разделитель дат появляется между сообщениями разных дней', async (page) => {
  const now = 1_700_000_000;
  await seed(page, {
    rooms: [{ id: 7, name: 'bob', is_direct: true, unread: 0, sort_ts: 1 }], cur: 7,
    messages: [
      { id: 1, room_id: 7, username: 'bob', t: 'text', text: 'вчера', ts: now - DAY },
      { id: 2, room_id: 7, username: 'bob', t: 'text', text: 'сегодня', ts: now },
    ],
  });
  const seps = await page.$$eval('#messages .datesep', (els) => els.map((e) => e.textContent));
  assert.equal(seps.length, 2, seps);
});

await scenario('живое сообщение: хвостик перескакивает с прошлой реплики на новую, если автор тот же', async (page) => {
  const now = 1_700_000_000;
  await seed(page, {
    rooms: [{ id: 8, name: 'bob', is_direct: true, unread: 0, sort_ts: 1 }], cur: 8,
    messages: [{ id: 1, room_id: 8, username: 'bob', t: 'text', text: 'первое', ts: now }],
  });
  let tails = await page.$$eval('#messages .row.tail', (els) => els.length);
  assert.equal(tails, 1);
  await page.evaluate((now) => {
    // open() расшифровывает через E2E — в этом тесте нет ни ключей, ни сети, подменяем на заглушку,
    // чтобы проверить именно логику группировки/хвостика в onMessage, а не крипто-стек целиком
    window.open = async (m) => { m.t = 'text'; m.text = m.text || ''; return m; };
    return onMessage({ id: 2, room_id: 8, username: 'bob', kind: 'e2e', text: 'второе', epoch: 1, ct: '', sig: '', ts: now + 3 });
  }, now);
  const rows = await page.$$eval('#messages .row', (els) => els.map((e) => e.classList.contains('tail')));
  assert.deepEqual(rows, [false, true], 'у первой реплики хвостик убран, у новой — есть');
});

await scenario('composer: пустое поле — иконка микрофона, есть текст — иконка отправки', async (page) => {
  await seed(page, { rooms: [{ id: 9, name: 'bob', is_direct: true, unread: 0, sort_ts: 1 }], cur: 9, messages: [] });
  const state = async () => page.evaluate(() => ({
    mic: !$('#micBtn').classList.contains('hidden'), send: !$('#sendBtn').classList.contains('hidden'),
  }));
  assert.deepEqual(await state(), { mic: true, send: false });
  await page.type('#input', 'привет');
  assert.deepEqual(await state(), { mic: false, send: true });
});

await scenario('иконки: кнопки шапки и composer содержат SVG, а не эмодзи-текст', async (page) => {
  await seed(page, { rooms: [] });
  const ids = ['btnDm', 'btnNewRoom', 'btnDiscover', 'btnLogout', 'micBtn', 'vidBtn'];
  const info = await page.evaluate((ids) => ids.map((id) => {
    const e = document.getElementById(id);
    return { id, svg: !!e.querySelector('svg'), text: e.textContent.trim() };
  }), ids);
  for (const i of info) { assert.ok(i.svg, `${i.id}: нет svg`); assert.equal(i.text, '', `${i.id}: остался текст "${i.text}"`); }
});

await browser.close(); server.close();
console.log(failed ? `\nПРОВАЛЕНО: ${failed}` : '\nВсе проверки интерфейса чата прошли');
process.exit(failed ? 1 : 0);
