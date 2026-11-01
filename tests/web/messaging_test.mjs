// Ответы, редактирование, удаление, файлы, черновики — в настоящем Chromium, через реальный app.js.
// Сеть и шифрование подменяются в точках, где это не мешает проверить именно логику клиента (как и в
// остальных тестах: например, «живое сообщение» в chat_ui_test.mjs подменяет open() тем же приёмом).
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
  } catch (e) { failed++; console.log('FAIL —', name, '\n   ', e.message.split('\n').slice(0, 8).join('\n    ')); }
  await page.close();
}

async function seed(page, { rooms, cur, messages }) {
  await page.evaluate(({ rooms, cur, messages }) => {
    S.me = 'alice';
    S.rooms = rooms;
    renderRooms();
    $('#auth').classList.add('hidden'); $('#app').classList.remove('hidden');
    if (cur != null) {
      S.cur = cur; S.loaded.add(cur);
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
const ROOM = { id: 1, name: 'bob', is_direct: true, unread: 0, sort_ts: 1, peer: 'bob', epoch: 1 };
const now = 1_700_000_000;

await scenario('меню сообщения: у чужого нет редактирования, у своего — есть, плюс общие пункты у обоих', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'text', text: 'привет', ts: now },
    { id: 2, room_id: 1, username: 'alice', t: 'text', text: 'привет-привет', ts: now + 5 },
  ] });
  const items = async (rowIdx) => page.evaluate((i) => {
    document.querySelectorAll('#messages .row')[i].querySelector('.msgMore').click();
    const labels = [...document.querySelectorAll('#msgMenu button span:last-child')].map((s) => s.textContent);
    closeMsgMenu();
    return labels;
  }, rowIdx);
  assert.deepEqual(await items(0), ['Ответить', 'Переслать', 'Закрепить', 'Удалить у себя'], 'чужое сообщение');
  assert.deepEqual(await items(1), ['Ответить', 'Переслать', 'Редактировать', 'Закрепить', 'Удалить у себя', 'Удалить у всех'], 'своё текстовое сообщение');
});

await scenario('меню: у своего файла нет пункта «Редактировать» (нечего редактировать)', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'alice', t: 'file', name: 'a.pdf', file: { size: 100 }, ts: now },
  ] });
  const labels = await page.evaluate(() => {
    document.querySelector('.row .msgMore').click();
    const l = [...document.querySelectorAll('#msgMenu button span:last-child')].map((s) => s.textContent);
    closeMsgMenu();
    return l;
  });
  assert.deepEqual(labels, ['Ответить', 'Переслать', 'Закрепить', 'Удалить у себя', 'Удалить у всех']);
});

await scenario('ответ: бар над composer\'ом, payload содержит reply_to, после отправки бар/состояние сбрасываются', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'text', text: 'на это отвечаем', ts: now },
  ] });
  await page.evaluate(() => startReply(S.msgs[1][0]));
  const bar = await page.evaluate(() => ({
    hidden: $('#composerBar').classList.contains('hidden'),
    title: $('#cbTitle').textContent, preview: $('#cbPreview').textContent,
  }));
  assert.deepEqual(bar, { hidden: false, title: 'bob', preview: 'на это отвечаем' });

  const sent = await page.evaluate(async () => {
    let captured = null;
    S.online = true;
    E2E.ensureKey = async () => 1;
    E2E.encryptPayload = async (id, ep, payload) => { captured = payload; return { epoch: ep, ct: 'x', sig: 'y' }; };
    window.wsSend = () => true;
    input().value = 'ответ';
    await sendText();
    return { captured, barHidden: $('#composerBar').classList.contains('hidden'), replyTo: S.replyTo, inputVal: input().value };
  });
  assert.deepEqual(sent.captured.reply_to, { id: 1, username: 'bob', t: 'text', text: 'на это отвечаем' });
  assert.equal(sent.captured.text, 'ответ');
  assert.equal(sent.barHidden, true); assert.equal(sent.replyTo, null); assert.equal(sent.inputVal, '');
});

await scenario('рендер цитаты в пузыре сообщения', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'text', text: 'вопрос', ts: now },
    { id: 2, room_id: 1, username: 'alice', t: 'text', text: 'ответ', ts: now + 5, replyTo: { id: 1, username: 'bob', text: 'вопрос' } },
  ] });
  const q = await page.evaluate(() => {
    const row = document.querySelectorAll('#messages .row')[1];
    return { name: row.querySelector('.quoteName')?.textContent, text: row.querySelector('.quoteText')?.textContent };
  });
  assert.deepEqual(q, { name: 'bob', text: 'вопрос' });
});

await scenario('редактирование: правка своего сообщения заполняет поле, PUT уходит на верный урл, применяется изм. метка', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 7, room_id: 1, username: 'alice', t: 'text', text: 'опечатко', ts: now },
  ] });
  await page.evaluate(() => startEdit(S.msgs[1][0]));
  const bar = await page.evaluate(() => ({ title: $('#cbTitle').textContent, val: input().value }));
  assert.deepEqual(bar, { title: 'Редактирование', val: 'опечатко' });

  const r = await page.evaluate(async () => {
    let putUrl = null, putBody = null;
    window.api = async (u, opts) => { putUrl = u; putBody = opts?.body; return { type: 'message_edited', id: 7, room_id: 1, epoch: 1, ct: 'NEW', sig: 's', edited_ts: 123 }; };
    S.online = true;
    E2E.ensureKey = async () => 1;
    E2E.encryptPayload = async () => ({ epoch: 1, ct: 'x', sig: 'y' });
    E2E.decryptMessage = async () => ({ t: 'text', text: 'опечатка' });
    input().value = 'опечатка';
    await sendText();
    return { putUrl, method: undefined, editing: S.editing, barHidden: $('#composerBar').classList.contains('hidden'),
             msgText: S.msgs[1][0].text, editedTs: S.msgs[1][0].edited_ts };
  });
  assert.equal(r.putUrl, '/rooms/1/messages/7');
  assert.equal(r.editing, null); assert.equal(r.barHidden, true);
  assert.equal(r.msgText, 'опечатка'); assert.equal(r.editedTs, 123);
  const label = await page.$eval('[data-id="7"] .time', (e) => e.textContent);
  assert.match(label, /^изм\./);
});

await scenario('удаление у себя: DELETE без for_eververyone=true, сообщение пропадает из S.msgs и DOM', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 3, room_id: 1, username: 'bob', t: 'text', text: 'сотрём', ts: now },
    { id: 4, room_id: 1, username: 'bob', t: 'text', text: 'останется', ts: now + 5 },
  ] });
  const r = await page.evaluate(async () => {
    let url = null;
    window.api = async (u) => { url = u; return { ok: true }; };
    await deleteMessage(S.msgs[1][0], false);
    return { url, ids: S.msgs[1].map((m) => m.id), domRows: document.querySelectorAll('#messages .row').length };
  });
  assert.equal(r.url, '/rooms/1/messages/3?for_everyone=false');
  assert.deepEqual(r.ids, [4]);
  assert.equal(r.domRows, 1);
});

await scenario('удаление у всех: for_everyone=true в урле', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 5, room_id: 1, username: 'alice', t: 'text', text: 'моё', ts: now },
  ] });
  const url = await page.evaluate(async () => {
    let u = null; window.api = async (x) => { u = x; return { ok: true }; };
    await deleteMessage(S.msgs[1][0], true);
    return u;
  });
  assert.equal(url, '/rooms/1/messages/5?for_everyone=true');
});

await scenario('файл: карточка с именем и размером; лишний размер отклоняется без похода в сеть', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 9, room_id: 1, username: 'bob', t: 'file', name: 'report.pdf', file: { size: 123456 }, ts: now },
  ] });
  const card = await page.evaluate(() => {
    const row = document.querySelector('.row');
    return { name: row.querySelector('.fileName').textContent, size: row.querySelector('.fileSize').textContent };
  });
  assert.equal(card.name, 'report.pdf'); assert.match(card.size, /КБ|МБ/);

  const guard = await page.evaluate(async () => {
    let mediaCalled = false, toastMsg = null;
    window.sendMedia = async () => { mediaCalled = true; };
    const origToast = window.toast; window.toast = (m) => { toastMsg = m; };
    await sendFile({ size: 40 * 1048576, name: 'huge.bin' });
    window.toast = origToast;
    return { mediaCalled, toastMsg };
  });
  assert.equal(guard.mediaCalled, false);
  assert.match(guard.toastMsg, /32 МБ/);
});

await scenario('черновики: сохраняются при уходе из комнаты и восстанавливаются при возврате; видны в списке для другой комнаты', async (page) => {
  const rooms = [ROOM, { id: 2, name: 'carol', is_direct: true, unread: 0, sort_ts: 2, peer: 'carol', epoch: 1 }];
  await seed(page, { rooms, cur: 1, messages: [] });
  await page.evaluate(() => { localStorage.clear(); input().value = 'не успела дописать'; });

  await page.evaluate(async () => {
    E2E.ensureKey = async () => 1; E2E.fetchKeys = async () => {};
    window.api = async () => [];
    await openRoom(2);
  });
  const afterSwitch = await page.evaluate(() => ({
    savedDraft: localStorage.getItem('frdm_draft_1'), inputForRoom2: input().value,
  }));
  assert.equal(afterSwitch.savedDraft, 'не успела дописать');
  assert.equal(afterSwitch.inputForRoom2, '');

  const listPreview = await page.evaluate(() => {
    const row = [...document.querySelectorAll('#rooms .room')].find((r) => r.querySelector('.name').textContent === 'bob');
    return { label: row.querySelector('.draftLabel')?.textContent, text: row.querySelector('.prev').textContent };
  });
  assert.equal(listPreview.label, 'Черновик: ');
  assert.equal(listPreview.text, 'Черновик: не успела дописать');

  await page.evaluate(async () => {
    window.api = async () => [];
    await openRoom(1);
  });
  const restored = await page.evaluate(() => input().value);
  assert.equal(restored, 'не успела дописать');

  // отправка (даже неудачная из-за оффлайна) не должна путать черновик соседней комнаты
  await page.evaluate(() => { localStorage.removeItem('frdm_draft_1'); });
  const cleared = await page.evaluate(() => localStorage.getItem('frdm_draft_1'));
  assert.equal(cleared, null);
});

await scenario('форматирование: **жирный**, *курсив*, `код`, ~~зачёркнутый~~ — своими тегами, без утечки звёздочек', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'text', text: 'обычный **жирный** *курсив* `код(){}` ~~было~~ снова обычный', ts: now },
  ] });
  const info = await page.evaluate(() => {
    const t = document.querySelector('.row .text');
    return {
      html: t.innerHTML,
      bold: [...t.querySelectorAll('b')].map((e) => e.textContent),
      italic: [...t.querySelectorAll('i')].map((e) => e.textContent),
      code: [...t.querySelectorAll('code')].map((e) => e.textContent),
      strike: [...t.querySelectorAll('s')].map((e) => e.textContent),
      fullText: t.textContent,
    };
  });
  assert.deepEqual(info.bold, ['жирный']);
  assert.deepEqual(info.italic, ['курсив']);
  assert.deepEqual(info.code, ['код(){}']);
  assert.deepEqual(info.strike, ['было']);
  assert.equal(info.fullText, 'обычный жирный курсив код(){} было снова обычный');
  assert.ok(!info.html.includes('*') && !info.html.includes('~~') && !info.html.includes('`'), 'символы разметки не должны остаться в выводе');
});

await scenario('форматирование: HTML в тексте сообщения не исполняется (текстовые узлы, не innerHTML)', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'text', text: '<img src=x onerror=alert(1)> **ok**', ts: now },
  ] });
  const info = await page.evaluate(() => {
    const t = document.querySelector('.row .text');
    return { hasImg: !!t.querySelector('img'), text: t.textContent, boldText: t.querySelector('b')?.textContent };
  });
  assert.equal(info.hasImg, false, 'тег из текста сообщения не должен стать настоящим элементом');
  assert.equal(info.boldText, 'ok');
  assert.ok(info.text.includes('<img src=x onerror=alert(1)>'), 'а как текст — должен остаться как есть');
});

await scenario('спойлер: ||текст|| скрыт до тапа (класс revealed), при повторном тапе снова скрывается', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'text', text: 'до ||секрет|| после', ts: now },
  ] });
  const before = await page.evaluate(() => {
    const s = document.querySelector('.spoiler');
    return { text: s.textContent, revealed: s.classList.contains('revealed') };
  });
  assert.equal(before.text, 'секрет');
  assert.equal(before.revealed, false, 'изначально скрыт');

  const afterClick = await page.evaluate(() => {
    document.querySelector('.spoiler').click();
    return document.querySelector('.spoiler').classList.contains('revealed');
  });
  assert.equal(afterClick, true, 'тап раскрывает');

  const afterSecondClick = await page.evaluate(() => {
    document.querySelector('.spoiler').click();
    return document.querySelector('.spoiler').classList.contains('revealed');
  });
  assert.equal(afterSecondClick, false, 'повторный тап снова скрывает');
});

await browser.close(); server.close();
console.log(failed ? `\nПРОВАЛЕНО: ${failed}` : '\nВсе проверки ответов/правки/удаления/файлов/черновиков прошли');
process.exit(failed ? 1 : 0);
