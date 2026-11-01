// Каталог стикеров (веб-клиент): лоток установленных паков, отправка стикера как обычного фото, поиск,
// создание/удаление пака, права автора. Настоящий Chromium, реальный app.js — api() подменяется точечно.
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
  await page.setViewport({ width: 420, height: 700 });
  try {
    await page.goto(URL_, { waitUntil: 'networkidle0' });
    await page.evaluate(() => {
      S.me = 'alice'; S.online = true; S.token = 'tok';
      const now = Date.now() / 1000;
      S.rooms = [{ id: 1, name: 'bob', is_direct: true, unread: 0, sort_ts: now, peer: 'bob', epoch: 1 }];
      renderRooms();
      $('#auth').classList.add('hidden'); $('#app').classList.remove('hidden');
      S.cur = 1; S.loaded.add(1);
      document.body.classList.add('in-chat');
      $('#empty').classList.add('hidden'); $('#chatView').classList.remove('hidden');
      $('#title').textContent = 'bob';
      const av = avatarEl('avatar', 'bob', 'bob'); $('#chatAvatar').replaceWith(av); av.id = 'chatAvatar';
      S.msgs[1] = [];
      renderAll();
    });
    await check(page);
    assert.deepEqual(problems.filter((p) => !/favicon|Failed to load resource/.test(p)), [], 'ошибки в консоли');
    console.log('OK —', name);
  } catch (e) { failed++; console.log('FAIL —', name, '\n   ', e.message.split('\n').slice(0, 8).join('\n    ')); }
  await page.close();
}

await scenario('лоток: показывает установленные паки, тап по стикеру отправляет как фото и закрывает лоток', async (page) => {
  const r = await page.evaluate(async () => {
    window.api = async (u) => {
      if (u === '/stickers/installed') {
        return [{ id: 1, name: 'Коты', creator_username: 'alice', sticker_count: 1, stickers: [{ id: 1, url: '/stickers/files/x.jpg' }] }];
      }
      return [];
    };
    window.fetch = async (u) => ({ ok: true, blob: async () => new Blob(['x'], { type: 'image/jpeg' }) });
    let sentKind = null, sentBlob = null;
    window.sendMedia = async (kind, blob) => { sentKind = kind; sentBlob = blob; };
    await toggleStickerTray();
    await new Promise((res) => setTimeout(res, 20));
    const catalogBtnText = document.querySelector('.stickerTrayCatalog').textContent;
    document.querySelector('.stickerThumb').click();
    await new Promise((res) => setTimeout(res, 20));
    return { sentKind, hasBlob: sentBlob instanceof Blob, trayHidden: $('#stickerTray').classList.contains('hidden'), catalogBtnText };
  });
  assert.equal(r.sentKind, 'image');
  assert.equal(r.hasBlob, true);
  assert.equal(r.trayHidden, true, 'после отправки лоток должен закрыться');
  assert.match(r.catalogBtnText, /Каталог/);
});

await scenario('лоток: повторный тап по кнопке закрывает лоток, ничего не запрашивая заново', async (page) => {
  const r = await page.evaluate(async () => {
    let calls = 0;
    window.api = async () => { calls++; return []; };
    await toggleStickerTray();
    await new Promise((res) => setTimeout(res, 20));
    const afterOpen = { hidden: $('#stickerTray').classList.contains('hidden'), calls };
    await toggleStickerTray();
    return { afterOpen, afterClose: { hidden: $('#stickerTray').classList.contains('hidden'), calls } };
  });
  assert.equal(r.afterOpen.hidden, false);
  assert.equal(r.afterOpen.calls, 1);
  assert.equal(r.afterClose.hidden, true);
  assert.equal(r.afterClose.calls, 1, 'закрытие не должно повторно запрашивать сервер');
});

await scenario('каталог: поиск уходит с введённым текстом (с задержкой), результаты кликабельны', async (page) => {
  const r = await page.evaluate(async () => {
    const queries = [];
    window.api = async (u) => {
      if (u.startsWith('/stickers/packs?')) {
        queries.push(decodeURIComponent(u.split('q=')[1]));
        return [{ id: 5, name: 'Найденный пак', creator_username: 'bob', sticker_count: 3 }];
      }
      return {};
    };
    openStickerCatalog();
    await new Promise((res) => setTimeout(res, 20));
    $('#stickerSearch').value = 'кот';
    $('#stickerSearch').dispatchEvent(new Event('input'));
    await new Promise((res) => setTimeout(res, 350));
    return { queries, rowText: document.querySelector('.packRowName')?.textContent, meta: document.querySelector('.packRowMeta')?.textContent };
  });
  assert.deepEqual(r.queries, ['', 'кот'], 'сначала пустой запрос при открытии каталога, потом введённый текст');
  assert.equal(r.rowText, 'Найденный пак');
  assert.match(r.meta, /bob.*3 стикера/);
});

await scenario('создание пака: пустое имя — ошибка без запроса; успешное — открывает пак и обновляет список', async (page) => {
  const r = await page.evaluate(async () => {
    let created = null, detailOpened = null;
    window.api = async (u, opts) => {
      if (opts?.method === 'POST' && u === '/stickers/packs') { created = opts.body; return { id: 9, name: opts.body.name }; }
      if (u === '/stickers/packs/9') { detailOpened = true; return { id: 9, name: 'Мой пак', creator_username: 'alice', sticker_count: 0, installed: false, stickers: [] }; }
      if (u.startsWith('/stickers/packs?')) return [];
      return {};
    };
    openNewPackDialog();
    await createStickerPack();
    const emptyErr = $('#newPackErr').textContent;
    const createdAfterEmpty = created;
    $('#newPackName').value = 'Мой пак';
    await createStickerPack();
    await new Promise((res) => setTimeout(res, 20));
    return { emptyErr, createdAfterEmpty, created, detailOpened, dialogClosed: !$('#stickerNewPackDlg').open, packDlgOpen: $('#stickerPackDlg').open };
  });
  assert.match(r.emptyErr, /название/i);
  assert.equal(r.createdAfterEmpty, null, 'пустое имя не должно уходить на сервер');
  assert.deepEqual(r.created, { name: 'Мой пак' });
  assert.equal(r.detailOpened, true, 'после создания должен открыться просмотр нового пака');
  assert.equal(r.dialogClosed, true);
  assert.equal(r.packDlgOpen, true);
});

await scenario('просмотр пака: свой пак — можно удалить стикер и сам пак; чужой — только отправка, без кнопок', async (page) => {
  const own = await page.evaluate(async () => {
    window.api = async (u) => {
      if (u === '/stickers/packs/1') {
        return { id: 1, name: 'Свой пак', creator_username: 'alice', sticker_count: 1, installed: false,
          stickers: [{ id: 11, url: '/x.jpg' }] };
      }
      return {};
    };
    await openPackDetail(1);
    await new Promise((res) => setTimeout(res, 20));
    return {
      hasDelSticker: !!document.querySelector('.stickerDelBtn'),
      addVisible: !$('#packDlgAddLabel').classList.contains('hidden'),
      deleteVisible: !$('#packDlgDelete').classList.contains('hidden'),
      installText: $('#packDlgInstall').textContent,
    };
  });
  assert.equal(own.hasDelSticker, true);
  assert.equal(own.addVisible, true);
  assert.equal(own.deleteVisible, true);
  assert.match(own.installText, /Установить$/);

  const foreign = await page.evaluate(async () => {
    window.api = async (u) => {
      if (u === '/stickers/packs/2') {
        return { id: 2, name: 'Чужой пак', creator_username: 'bob', sticker_count: 1, installed: true,
          stickers: [{ id: 22, url: '/y.jpg' }] };
      }
      return {};
    };
    await openPackDetail(2);
    await new Promise((res) => setTimeout(res, 20));
    return {
      hasDelSticker: !!document.querySelector('.stickerDelBtn'),
      addVisible: !$('#packDlgAddLabel').classList.contains('hidden'),
      deleteVisible: !$('#packDlgDelete').classList.contains('hidden'),
      installText: $('#packDlgInstall').textContent,
    };
  });
  assert.equal(foreign.hasDelSticker, false, 'в чужом паке кнопок удаления стикеров быть не должно');
  assert.equal(foreign.addVisible, false);
  assert.equal(foreign.deleteVisible, false, 'удалить чужой пак нельзя — кнопки не должно быть вовсе');
  assert.match(foreign.installText, /Установлено/);
});

await browser.close(); server.close();
console.log(failed ? `\nПРОВАЛЕНО: ${failed}` : '\nВсе проверки каталога стикеров (веб) прошли');
process.exit(failed ? 1 : 0);
