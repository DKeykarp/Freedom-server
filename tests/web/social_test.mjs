// Реакции, пересылка, закреплённые сообщения, самоудаление — настоящий Chromium, реальный app.js.
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
    S.me = 'alice'; S.online = true; S.rooms = rooms; renderRooms();
    $('#auth').classList.add('hidden'); $('#app').classList.remove('hidden');
    if (cur != null) {
      S.cur = cur; S.loaded.add(cur);
      $('#empty').classList.add('hidden'); $('#chatView').classList.remove('hidden');
      const r = S.rooms.find((x) => x.id === cur);
      $('#title').textContent = r.name;
      const av = r.is_direct ? avatarEl('avatar', r.name, r.name) : avatarEl('avatar', null, r.name);
      $('#chatAvatar').replaceWith(av); av.id = 'chatAvatar';
      S.msgs[cur] = messages || [];
      renderAll();
    }
  }, { rooms, cur, messages });
}
const ROOM = { id: 1, name: 'bob', is_direct: true, unread: 0, sort_ts: 1, peer: 'bob', epoch: 1 };
const ROOM2 = { id: 2, name: 'carol', is_direct: true, unread: 0, sort_ts: 2, peer: 'carol', epoch: 1 };
const now = 1_700_000_000;

function menuClick(idx, label) {
  return `(() => {
    const rows = document.querySelectorAll('#messages .row');
    rows[${idx}].querySelector('.msgMore').click();
    const btn = [...document.querySelectorAll('#msgMenu button')].find((b) => b.querySelector('span')?.textContent === ${JSON.stringify(label)});
    btn?.click();
  })()`;
}

await scenario('реакция: тап по эмодзи в меню ставит её, PUT уходит с epoch/ct/sig, пилюля появляется', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'text', text: 'привет', ts: now },
  ] });
  const r = await page.evaluate(async () => {
    let putUrl = null, putBody = null;
    E2E.ensureKey = async () => 1;
    E2E.encryptPayload = async (id, ep, payload) => { window.__payload = payload; return { epoch: ep, ct: 'CT1', sig: 'S1' }; };
    E2E.decryptMessage = async (raw) => ({ emoji: '👍' });
    window.api = async (u, opts) => { putUrl = u; putBody = opts?.body; return { type: 'reaction_set', room_id: 1, message_id: 1, username: 'alice', epoch: 1, ct: 'CT1', sig: 'S1' }; };
    document.querySelector('.row .msgMore').click();
    document.querySelector('.reactionPicker .reactionPick').click();
    await new Promise((res) => setTimeout(res, 30));
    return { putUrl, putBody, payload: window.__payload, pill: document.querySelector('.reactionPill')?.textContent };
  });
  assert.equal(r.putUrl, '/rooms/1/messages/1/reaction');
  assert.equal(r.putBody.epoch, 1);
  assert.equal(r.payload.emoji, '👍');
  assert.equal(r.pill, '👍 1');
});

await scenario('реакция: повторный тап тем же эмодзи снимает её (DELETE), другим — заменяет (тот же PUT)', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'text', text: 'привет', ts: now, reactionsDecoded: [{ username: 'alice', emoji: '👍' }] },
  ] });
  await page.evaluate(() => renderAll());
  const r = await page.evaluate(async () => {
    let method = null, url = null;
    window.api = async (u, opts) => { url = u; method = opts?.method; return {}; };
    await toggleReaction(S.msgs[1][0], '👍'); // тот же эмодзи -> снять
    return { url, method };
  });
  assert.equal(r.method, 'DELETE');
  assert.equal(r.url, '/rooms/1/messages/1/reaction');
});

await scenario('пересылка: диалог со списком комнат; текстовое сообщение уходит через wsSend с forwarded_from', async (page) => {
  await seed(page, { rooms: [ROOM, ROOM2], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'text', text: 'важное', ts: now },
  ] });
  const r = await page.evaluate(async () => {
    document.querySelector('.row .msgMore').click();
    const fwBtn = [...document.querySelectorAll('#msgMenu button')].find((b) => b.querySelector('span')?.textContent === 'Переслать');
    fwBtn.click();
    const roomBtns = [...document.querySelectorAll('#fwList button')].map((b) => b.textContent);
    let captured = null;
    E2E.ensureKey = async () => 5;
    E2E.encryptPayload = async (id, ep, payload) => { captured = { id, ep, payload }; return { epoch: ep, ct: 'x', sig: 'y' }; };
    let wsPayload = null;
    window.wsSend = (p) => { wsPayload = p; return true; };
    document.querySelectorAll('#fwList button')[1].click(); // carol — вторая комната в списке
    await new Promise((res) => setTimeout(res, 30));
    return { roomBtns, captured, wsPayload, dialogOpen: $('#fwdlg').open };
  });
  assert.deepEqual(r.roomBtns, ['bob', 'carol']);
  assert.equal(r.captured.id, 2, 'должно шифроваться под ключ комнаты carol, а не текущей');
  assert.equal(r.captured.payload.text, 'важное');
  assert.deepEqual(r.captured.payload.forwarded_from, { username: 'bob' });
  assert.equal(r.wsPayload.room_id, 2);
  assert.equal(r.dialogOpen, false);
});

await scenario('закрепление: пункт меняется на «Открепить», бейдж появляется в сообщении', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'alice', t: 'text', text: 'важное', ts: now, pinned: false },
  ] });
  const r1 = await page.evaluate(async () => {
    window.api = async () => ({ type: 'message_pinned', room_id: 1, message_id: 1, pinned_by: 'alice' });
    document.querySelector('.row .msgMore').click();
    const btn = [...document.querySelectorAll('#msgMenu button')].find((b) => b.querySelector('span')?.textContent === 'Закрепить');
    btn.click();
    await new Promise((res) => setTimeout(res, 30));
    return { pinned: S.msgs[1][0].pinned, badge: !!document.querySelector('.pinBadge') };
  });
  assert.equal(r1.pinned, true);
  assert.equal(r1.badge, true);

  const r2 = await page.evaluate(async () => {
    document.querySelector('.row .msgMore').click();
    const labels = [...document.querySelectorAll('#msgMenu button span:last-child')].map((s) => s.textContent);
    closeMsgMenu();
    return labels;
  });
  assert.ok(r2.includes('Открепить') && !r2.includes('Закрепить'));
});

await scenario('самоудаление: выбор в диалоге безопасности уходит PUT-ом на /autodelete с нужным значением', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [] });
  const r = await page.evaluate(async () => {
    let putUrl = null, putBody = null;
    window.api = async (u, opts) => { putUrl = u; putBody = opts?.body; return {}; };
    E2E.safetyNumber = async () => '00000 00000 00000 00000 00000 00000 00000 00000';
    await showSecurity();
    const btn = [...document.querySelectorAll('#adList button')].find((b) => b.textContent === '1 день');
    btn.click();
    await new Promise((res) => setTimeout(res, 30));
    return { putUrl, putBody, roomValue: room(1).autodelete_seconds };
  });
  assert.equal(r.putUrl, '/rooms/1/autodelete');
  assert.deepEqual(r.putBody, { seconds: 86400 });
  assert.equal(r.roomValue, 86400);
});

await scenario('поиск: находит совпадения в загруженной истории, подсвечивает, клик по результату скроллит к сообщению', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'text', text: 'привет как дела с отчётом', ts: now },
    { id: 2, room_id: 1, username: 'alice', t: 'text', text: 'отчёт почти готов', ts: now + 5 },
    { id: 3, room_id: 1, username: 'bob', t: 'text', text: 'без совпадений тут', ts: now + 10 },
    { id: 4, room_id: 1, username: 'bob', t: 'image', file: { size: 1 }, media: '/qr.js', ts: now + 15 }, // не текст — не должен попасть в поиск
  ] });
  const r = await page.evaluate(() => {
    toggleChatSearch();
    runChatSearch('отчёт');
    const hits = [...document.querySelectorAll('.searchHit')].map((h) => h.querySelector('.shText').textContent);
    const marks = [...document.querySelectorAll('.searchHit mark')].map((m) => m.textContent);
    return { hits, marks, panelHidden: $('#searchPanel').classList.contains('hidden') };
  });
  assert.deepEqual(r.hits, ['отчёт почти готов', 'привет как дела с отчётом'], 'новые сверху, найдены оба, картинка и «без совпадений» — не попали');
  assert.deepEqual(r.marks, ['отчёт', 'отчёт']);
  assert.equal(r.panelHidden, false);

  const r2 = await page.evaluate(() => {
    document.querySelectorAll('.searchHit')[1].click(); // «привет как дела с отчётом» (id=1)
    return { panelHidden: $('#searchPanel').classList.contains('hidden'), flashed: document.querySelector('[data-id="1"]').classList.contains('flash') };
  });
  assert.equal(r2.panelHidden, true, 'клик по результату закрывает панель поиска');
  assert.equal(r2.flashed, true, 'найденное сообщение подсвечивается');

  const empty = await page.evaluate(() => {
    toggleChatSearch(); runChatSearch('нетнигдетакого');
    return document.querySelector('.searchEmpty')?.textContent;
  });
  assert.match(empty, /Ничего не найдено/);
});

await scenario('галочки: одиночная — отправлено, двойная — прочитано собеседником (и только у своих сообщений)', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'alice', t: 'text', text: 'моё, прочитано', ts: now },
    { id: 2, room_id: 1, username: 'alice', t: 'text', text: 'моё, ещё нет', ts: now + 5 },
    { id: 3, room_id: 1, username: 'bob', t: 'text', text: 'чужое сообщение', ts: now + 10 },
  ] });
  const r = await page.evaluate(() => {
    S.readState[1] = { bob: 1 };
    renderAll();
    const rows = [...document.querySelectorAll('#messages .row')];
    return rows.map((row) => ({
      id: row.dataset.id,
      hasTick: !!row.querySelector('.tick'),
      read: row.querySelector('.tick')?.classList.contains('read') ?? null,
    }));
  });
  assert.deepEqual(r, [
    { id: '1', hasTick: true, read: true },
    { id: '2', hasTick: true, read: false },
    { id: '3', hasTick: false, read: null },
  ]);
});

await scenario('галочки: групповой чат — «прочитано» только когда прочли ВСЕ участники', async (page) => {
  const group = { id: 1, name: 'Команда', is_direct: false, unread: 0, sort_ts: 1 };
  await seed(page, { rooms: [group], cur: 1, messages: [
    { id: 5, room_id: 1, username: 'alice', t: 'text', text: 'всем привет', ts: now },
  ] });
  const partial = await page.evaluate(() => {
    S.readState[1] = { bob: 5, carol: 0 };
    renderAll();
    return document.querySelector('.tick').classList.contains('read');
  });
  assert.equal(partial, false, 'carol ещё не читала — должна быть одиночная галочка');

  const full = await page.evaluate(() => {
    S.readState[1] = { bob: 5, carol: 5 };
    renderAll();
    return document.querySelector('.tick').classList.contains('read');
  });
  assert.equal(full, true, 'прочли все — двойная галочка');
});

await scenario('галочки: read_state с сервера (открытие чата) и живое обновление по WS сходятся в один эффект', async (page) => {
  await seed(page, { rooms: [ROOM], cur: null, messages: [] });
  const r = await page.evaluate(async () => {
    window.api = async (u) => {
      if (u.includes('/read_state')) return [{ username: 'bob', last_read_id: 0 }];
      if (u.includes('/messages')) return [{ id: 1, room_id: 1, username: 'alice', kind: 'e2e', text: '', epoch: 1, ct: 'x', sig: 'y', media: null, ts: Date.now() / 1000, edited_ts: null, reactions: [], pinned: false }];
      return [];
    };
    E2E.ensureKey = async () => 1;
    E2E.decryptMessage = async () => ({ t: 'text', text: 'проверка' });
    await openRoom(1);
    const before = document.querySelector('.tick')?.classList.contains('read');
    applyReadState({ type: 'read_state', room_id: 1, username: 'bob', message_id: 1 });
    const after = document.querySelector('.tick')?.classList.contains('read');
    return { before, after };
  });
  assert.equal(r.before, false, 'изначально bob ничего не читал');
  assert.equal(r.after, true, 'после read_state по WS — прочитано');
});

await scenario('упоминания: @имя в тексте рендерится кликабельным и открывает профиль этого человека', async (page) => {
  await seed(page, { rooms: [{ id: 1, name: 'Команда', is_direct: false, unread: 0, sort_ts: 1 }], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'text', text: 'привет @carol, глянь плиз @nikita_99', ts: now },
  ] });
  const r = await page.evaluate(async () => {
    const mentions = [...document.querySelectorAll('.mention')].map((s) => s.textContent);
    let opened = null;
    window.openContactProfile = (u) => { opened = u; };
    document.querySelector('.mention').click();
    return { mentions, opened };
  });
  assert.deepEqual(r.mentions, ['@carol', '@nikita_99']);
  assert.equal(r.opened, 'carol');
});

await scenario('упоминания: email-подобное user@site не считается упоминанием, короткое @ab — тоже (мин. 3 символа)', async (page) => {
  await seed(page, { rooms: [{ id: 1, name: 'Команда', is_direct: false, unread: 0, sort_ts: 1 }], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'text', text: 'пишите на user@site.com или тут, но не @ab и не @toolongusernamewaytoolong1234567890', ts: now },
  ] });
  const mentions = await page.evaluate(() => [...document.querySelectorAll('.mention')].map((s) => s.textContent));
  assert.deepEqual(mentions, [], 'ни email, ни слишком короткое/длинное имя не должны стать упоминанием');
});

await scenario('автодополнение @: только в групповых чатах, фильтрует по префиксу, не предлагает себя', async (page) => {
  await seed(page, { rooms: [{ id: 1, name: 'Команда', is_direct: false, unread: 0, sort_ts: 1 }], cur: 1, messages: [] });
  const r = await page.evaluate(async () => {
    window.api = async () => [{ username: 'alice' }, { username: 'carolina' }, { username: 'carl' }, { username: 'dave' }];
    input().value = 'привет @car';
    input().setSelectionRange(11, 11);
    await checkMentionTrigger();
    const items = [...document.querySelectorAll('#mentionList button span:last-child')].map((s) => s.textContent);
    const listVisible = !$('#mentionList').classList.contains('hidden');
    return { items, listVisible };
  });
  assert.deepEqual(r.items, ['carolina', 'carl'], 'подходят только начинающиеся на car, себя (alice) в списке участников для автодополнения нет вовсе');
  assert.equal(r.listVisible, true);
});

await scenario('автодополнение @: клик по варианту вставляет @имя с пробелом на место частичного ввода', async (page) => {
  await seed(page, { rooms: [{ id: 1, name: 'Команда', is_direct: false, unread: 0, sort_ts: 1 }], cur: 1, messages: [] });
  const r = await page.evaluate(async () => {
    window.api = async () => [{ username: 'carolina' }];
    input().value = 'привет @car';
    input().setSelectionRange(11, 11);
    await checkMentionTrigger();
    document.querySelector('#mentionList button').click();
    return { value: input().value, listHidden: $('#mentionList').classList.contains('hidden') };
  });
  assert.equal(r.value, 'привет @carolina ');
  assert.equal(r.listHidden, true);
});

await scenario('автодополнение @: в личном чате не появляется вообще', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [] });
  const r = await page.evaluate(async () => {
    window.api = async () => [{ username: 'bob' }];
    input().value = 'привет @b';
    input().setSelectionRange(9, 9);
    await checkMentionTrigger();
    return $('#mentionList').classList.contains('hidden');
  });
  assert.equal(r, true, 'в ЛС упоминать больше некого, кроме собеседника — автодополнение ни к чему');
});

await scenario('заглушить чат: скрывает браузерное уведомление о новом сообщении, включить звук обратно — возвращает', async (page) => {
  await seed(page, { rooms: [ROOM], cur: null, messages: [] });
  const r = await page.evaluate(async () => {
    Object.defineProperty(document, 'hidden', { value: true, configurable: true });
    let notified = 0;
    window.Notification = class { constructor() { notified++; } static permission = 'granted'; };
    const msg = { id: 1, room_id: 1, username: 'bob', kind: 'e2e', text: '', epoch: 1, ct: 'x', sig: 'y', media: null, ts: Date.now() / 1000, edited_ts: null, reactions: [], pinned: false };
    E2E.decryptMessage = async () => ({ t: 'text', text: 'привет' });

    setMuted(1, true);
    await onMessage({ ...msg, id: 1 });
    const whileMuted = notified;

    setMuted(1, false);
    await onMessage({ ...msg, id: 2 });
    const afterUnmute = notified;

    return { whileMuted, afterUnmute };
  });
  assert.equal(r.whileMuted, 0, 'пока заглушено — уведомления не будет');
  assert.equal(r.afterUnmute, 1, 'после включения звука — уведомление придёт');
});

await scenario('архивировать чат: пропадает из основного списка, появляется под «Архив», разворачивается по тапу', async (page) => {
  await seed(page, { rooms: [ROOM, ROOM2], cur: null, messages: [] });
  const r1 = await page.evaluate(() => {
    document.querySelector('.roomMore').click();
    const btn = [...document.querySelectorAll('#msgMenu button')].find((b) => b.querySelector('span')?.textContent === 'Архивировать');
    btn.click();
    return {
      names: [...document.querySelectorAll('.room .name')].map((n) => n.textContent.trim()),
      archiveBar: document.querySelector('.archiveBar')?.textContent,
    };
  });
  assert.deepEqual(r1.names, ['carol'], 'bob архивирован и не виден в основном списке');
  assert.match(r1.archiveBar, /Архив \(1\)/);

  const r2 = await page.evaluate(() => {
    document.querySelector('.archiveBar').click();
    return [...document.querySelectorAll('.room .name')].map((n) => n.textContent.trim());
  });
  assert.deepEqual(r2.sort(), ['bob', 'carol'].sort(), 'по тапу на «Архив» оба чата видны');

  const r3 = await page.evaluate(() => {
    const bobRow = [...document.querySelectorAll('.room')].find((r) => r.querySelector('.name').textContent.trim() === 'bob');
    bobRow.querySelector('.roomMore').click();
    const btn = [...document.querySelectorAll('#msgMenu button')].find((b) => b.querySelector('span')?.textContent === 'Вернуть из архива');
    btn.click();
    return isArchived(1);
  });
  assert.equal(r3, false, 'после «вернуть из архива» чат больше не архивный');
});

await scenario('запланировать: шифрует текст и уходит POST с send_at, поле очищается, панель обновляется', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [] });
  const r = await page.evaluate(async () => {
    input().value = 'напомнить о встрече';
    toggleSend();
    let postUrl = null, postBody = null;
    E2E.ensureKey = async () => 3;
    E2E.encryptPayload = async (id, ep, payload) => { window.__payload = payload; return { epoch: ep, ct: 'CT', sig: 'SIG' }; };
    let scheduledCallCount = 0;
    window.api = async (u, opts) => {
      if (opts?.method === 'POST' && u.includes('/schedule')) { postUrl = u; postBody = opts.body; return {}; }
      if (u.includes('/scheduled')) { scheduledCallCount++; return scheduledCallCount > 0 ? [{ id: 1, send_at: Date.now() / 1000 + 3600 }] : []; }
      return [];
    };
    openScheduleDialog();
    const dialogOpen = $('#scheddlg').open;
    const future = new Date(Date.now() + 3600000);
    $('#schedAt').value = future.toISOString().slice(0, 16);
    await confirmSchedule();
    return { dialogOpen, postUrl, sendAt: postBody?.send_at, payload: window.__payload, inputAfter: input().value, dialogClosedAfter: !$('#scheddlg').open };
  });
  assert.equal(r.dialogOpen, true);
  assert.equal(r.postUrl, '/rooms/1/schedule');
  assert.ok(r.sendAt > Date.now() / 1000, 'send_at в будущем');
  assert.equal(r.payload.text, 'напомнить о встрече');
  assert.equal(r.payload.t, 'text');
  assert.equal(r.inputAfter, '', 'поле очищается после планирования');
  assert.equal(r.dialogClosedAfter, true);
});

await scenario('запланировать: пустое или прошедшее время — понятная ошибка, никакого запроса', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [] });
  const r = await page.evaluate(async () => {
    input().value = 'текст';
    let called = false;
    window.api = async () => { called = true; return {}; };
    $('#schedAt').value = '';
    await confirmSchedule();
    const emptyErr = $('#schedErr').textContent;
    const past = new Date(Date.now() - 3600000);
    $('#schedAt').value = past.toISOString().slice(0, 16);
    await confirmSchedule();
    const pastErr = $('#schedErr').textContent;
    return { called, emptyErr, pastErr };
  });
  assert.equal(r.called, false, 'ни один из некорректных вариантов не должен дойти до сервера');
  assert.match(r.emptyErr, /Укажите время/);
  assert.match(r.pastErr, /будущем/);
});

await scenario('панель «Запланировано»: показывает счётчик, тап открывает список, отмена убирает строку и обновляет счётчик', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [] });
  let scheduled = [{ id: 5, room_id: 1, send_at: Date.now() / 1000 + 1800, created: Date.now() / 1000 }];
  const r1 = await page.evaluate(async (list) => {
    window.api = async () => list;
    await refreshScheduledBar(1);
    return { hidden: $('#scheduledBar').classList.contains('hidden'), text: $('#scheduledBar').textContent };
  }, scheduled);
  assert.equal(r1.hidden, false);
  assert.match(r1.text, /Запланировано: 1/);

  const r2 = await page.evaluate(async () => {
    document.querySelector('#scheduledBar').click();
    await new Promise((res) => setTimeout(res, 20));
    return { dialogOpen: $('#schedlistdlg').open, rows: document.querySelectorAll('.schedRow').length };
  });
  assert.equal(r2.dialogOpen, true);
  assert.equal(r2.rows, 1);

  const r3 = await page.evaluate(async () => {
    let deleteUrl = null;
    window.api = async (u, opts) => {
      if (opts?.method === 'DELETE') { deleteUrl = u; return {}; }
      return [];   // после отмены сервер бы вернул пустой список
    };
    document.querySelector('.schedRow button').click();
    await new Promise((res) => setTimeout(res, 20));
    return { deleteUrl, barHiddenAfter: $('#scheduledBar').classList.contains('hidden'), listEmpty: document.querySelector('.searchEmpty')?.textContent };
  });
  assert.equal(r3.deleteUrl, '/rooms/1/scheduled/5');
  assert.equal(r3.barHiddenAfter, true, 'счётчик пропадает, когда планировать больше нечего');
  assert.match(r3.listEmpty, /Нет запланированных/);
});

await scenario('опрос: карточка показывает вопрос, проценты, число голосов и подсветку своего варианта', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'poll', question: 'Когда?', options: ['Пн', 'Ср'],
      votesDecoded: [{ username: 'alice', option: 0 }, { username: 'bob', option: 1 }], ts: now },
  ] });
  const r = await page.evaluate(() => {
    const opts = [...document.querySelectorAll('.pollOption')];
    return {
      question: document.querySelector('.pollQuestion').textContent,
      labels: opts.map((o) => o.querySelector('.pollOptionLabel span').textContent),
      pcts: opts.map((o) => o.querySelector('.pollPct').textContent),
      mine: opts.map((o) => o.classList.contains('mine')),
      total: document.querySelector('.pollTotal').textContent,
    };
  });
  assert.equal(r.question, 'Когда?');
  assert.deepEqual(r.labels, ['Пн', 'Ср']);
  assert.deepEqual(r.pcts, ['50%', '50%']);
  assert.deepEqual(r.mine, [true, false], 'своя отметка (alice проголосовала за первый вариант) подсвечена');
  assert.match(r.total, /2 голоса/);
});

await scenario('опрос: тап по варианту голосует (PUT зашифрованным payload), повторный тап по своему — снимает (DELETE)', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [
    { id: 1, room_id: 1, username: 'bob', t: 'poll', question: 'Q', options: ['A', 'B'], votesDecoded: [], ts: now },
  ] });
  const r1 = await page.evaluate(async () => {
    let putUrl = null, putBody = null, payload = null;
    E2E.ensureKey = async () => 2;
    E2E.encryptPayload = async (id, ep, p) => { payload = p; return { epoch: ep, ct: 'CT', sig: 'SIG' }; };
    E2E.decryptMessage = async () => ({ option: 1 });
    window.api = async (u, opts) => { putUrl = u; putBody = opts?.body; return { type: 'vote_set', room_id: 1, message_id: 1, username: 'alice', epoch: 2, ct: 'CT', sig: 'SIG' }; };
    document.querySelectorAll('.pollOption')[1].click();
    await new Promise((res) => setTimeout(res, 20));
    return { putUrl, putBody, payload, mine: [...document.querySelectorAll('.pollOption')].map((o) => o.classList.contains('mine')) };
  });
  assert.equal(r1.putUrl, '/rooms/1/messages/1/vote');
  assert.equal(r1.payload.option, 1);
  assert.deepEqual(r1.mine, [false, true]);

  const r2 = await page.evaluate(async () => {
    let method = null;
    window.api = async (u, opts) => { method = opts?.method; return {}; };
    document.querySelectorAll('.pollOption')[1].click();
    await new Promise((res) => setTimeout(res, 20));
    return method;
  });
  assert.equal(r2, 'DELETE', 'повторный тап по своему же варианту снимает голос');
});

await scenario('создать опрос: заполнение и «Создать» уходит через wsSend с payload t:poll; валидация без запроса', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [] });
  const invalid = await page.evaluate(async () => {
    let called = false;
    window.wsSend = () => { called = true; return true; };
    openPollDialog();
    $('#pollQuestion').value = '';
    await createPoll();
    const emptyQErr = $('#pollErr').textContent;
    $('#pollQuestion').value = 'Вопрос';
    $('#pollOptions').children[0].value = 'Только один';
    await createPoll();
    return { called, emptyQErr, oneOptErr: $('#pollErr').textContent };
  });
  assert.equal(invalid.called, false);
  assert.match(invalid.emptyQErr, /вопрос/i);
  assert.match(invalid.oneOptErr, /два варианта/);

  const ok = await page.evaluate(async () => {
    E2E.ensureKey = async () => 4;
    E2E.encryptPayload = async (id, ep, p) => ({ epoch: ep, ct: 'x', sig: 'y', __payload: p });
    let sent = null;
    window.wsSend = (p) => { sent = p; return true; };
    $('#pollOptions').children[1].value = 'Второй';
    await createPoll();
    return { sent, dialogOpen: $('#polldlg').open };
  });
  assert.equal(ok.sent.room_id, 1);
  assert.equal(ok.dialogOpen, false);
});

await scenario('создать опрос: «+ добавить вариант» останавливается на 10', async (page) => {
  await seed(page, { rooms: [ROOM], cur: 1, messages: [] });
  const n = await page.evaluate(() => {
    openPollDialog();
    for (let i = 0; i < 15; i++) addPollOption();
    return $('#pollOptions').children.length;
  });
  assert.equal(n, 10);
});

await browser.close(); server.close();
console.log(failed ? `\nПРОВАЛЕНО: ${failed}` : '\nВсе проверки реакций/пересылки/закрепления/самоудаления прошли');
process.exit(failed ? 1 : 0);
