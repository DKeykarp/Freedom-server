// Страница входа в настоящем Chromium: блок скачивания, CSP из main.py, вёрстка. Без Chromium тест пропускается.
import fs from 'node:fs'; import http from 'node:http'; import path from 'node:path'; import crypto from 'node:crypto';
import assert from 'node:assert/strict'; import { createRequire } from 'node:module';

const ROOT = new URL('../../server/', import.meta.url).pathname;
const WEB = path.join(ROOT, 'web');
const req = createRequire(import.meta.url);
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

// та же политика безопасности, что ставит сервер
const py = fs.readFileSync(path.join(ROOT, 'main.py'), 'utf8');
const CSP = [...py.match(/"Content-Security-Policy",\s*((?:"[^"]*"\s*)+)\)/)[1].matchAll(/"([^"]*)"/g)].map((m) => m[1]).join('');
assert.match(CSP, /script-src 'self'/);

const FILES = { 'Freedom-Setup-1.0.0.exe': Buffer.concat([Buffer.from('MZ'), crypto.randomBytes(2 * 1048576)]),
  'Freedom-1.0.0-linux-amd64.deb': crypto.randomBytes(1500000), 'Freedom-1.0.0-linux-x86_64.rpm': crypto.randomBytes(1600000) };
const listing = () => Object.entries(FILES).map(([name, b]) => ({ name, url: '/downloads/' + name, kind: name.split('.').pop(),
  platform: name.endsWith('.exe') ? 'windows' : 'linux', size: b.length, sha256: crypto.createHash('sha256').update(b).digest('hex'), version: '1.0.0' }));
let mode = 'files';
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css' };
const server = http.createServer((rq, rs) => {
  const send = (code, body, type, extra = {}) => { rs.writeHead(code, { 'Content-Type': type, 'Content-Security-Policy': CSP, 'X-Content-Type-Options': 'nosniff', ...extra }); rs.end(body); };
  if (rq.url === '/api/downloads') {
    if (mode === 'error') return send(500, '{}', 'application/json');
    return send(200, JSON.stringify(mode === 'empty' ? [] : listing()), 'application/json');
  }
  if (rq.url.startsWith('/downloads/')) {
    const b = FILES[rq.url.slice(11)]; if (!b) return send(404, '{}', 'application/json');
    return send(200, b, 'application/octet-stream', { 'Content-Disposition': `attachment; filename="${rq.url.slice(11)}"` });
  }
  const f = path.join(WEB, rq.url === '/' ? 'index.html' : rq.url.split('?')[0]);
  if (!f.startsWith(WEB) || !fs.existsSync(f)) return send(404, 'nf', 'text/plain');
  send(200, fs.readFileSync(f), MIME[path.extname(f)] || 'application/octet-stream');
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const URL_ = `http://127.0.0.1:${server.address().port}/`;

const UAS = {
  linux: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
  windows: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
  android: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36',
  app: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Electron/31.7.7 Safari/537.36 FreedomDesktop/1.0.0',
};
const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ['--no-sandbox', '--disable-gpu'] });
let failed = 0;
async function scenario(name, { ua, m = 'files', vp = { width: 1100, height: 800 }, shot } = {}, check) {
  mode = m;
  const page = await browser.newPage(); const problems = [];
  page.on('console', (x) => { if (['error', 'warning'].includes(x.type())) problems.push(x.text()); });
  page.on('pageerror', (e) => problems.push('pageerror: ' + e.message));
  await page.setViewport(vp); await page.setUserAgent(UAS[ua] || UAS.linux);
  try {
    await page.goto(URL_, { waitUntil: 'networkidle0' });
    await check(page);
    assert.deepEqual(problems.filter((p) => !/favicon|Failed to load resource/.test(p)), [], 'ошибки в консоли (в т.ч. нарушения CSP)');
    if (shot) await page.screenshot({ path: shot });
    console.log('OK —', name);
  } catch (e) { failed++; console.log('FAIL —', name, '\n   ', e.message.split('\n').slice(0, 4).join('\n    ')); }
  await page.close();
}
const items = (p) => p.$$eval('#dlList .dlItem', (els) => els.map((e) => ({ rec: e.classList.contains('dlMine'), title: e.querySelector('b').textContent, href: e.querySelector('a').getAttribute('href'), dl: e.querySelector('a').hasAttribute('download') })));
const visible = (p) => p.$eval('#dl', (e) => !e.classList.contains('hidden') && e.getBoundingClientRect().height > 0);

await scenario('Linux: блок виден, deb и rpm первыми и подсвечены', { ua: 'linux', shot: '/tmp/auth_linux.png' }, async (p) => {
  assert.ok(await visible(p));
  // регрессия: классы блока не должны цепляться за стили приложения (.rec = мигающий красный индикатор записи)
  const st = await p.$$eval('#dl *', (els) => els.map((e) => { const c = getComputedStyle(e); return { tag: e.tagName, anim: c.animationName, op: c.opacity, color: c.color }; }));
  assert.ok(st.every((x) => x.anim === 'none' && x.op === '1'), 'в блоке скачивания нет анимаций и прозрачности: ' + JSON.stringify(st.find((x) => x.anim !== 'none' || x.op !== '1')));
  const danger = await p.evaluate(() => { const t = document.createElement('i'); t.style.color = getComputedStyle(document.documentElement).getPropertyValue('--danger'); document.body.append(t); const c = getComputedStyle(t).color; t.remove(); return c; });
  assert.ok(st.every((x) => x.color !== danger), 'ни один элемент блока не красный');
  const it = await items(p);
  assert.deepEqual(it.map((i) => i.href.split('.').pop()), ['deb', 'rpm', 'exe']);
  assert.deepEqual(it.map((i) => i.rec), [true, true, false]); assert.ok(it.every((i) => i.dl));
  const hint = await p.$$eval('#dlList code', (c) => c.map((x) => x.textContent));
  assert.ok(hint.includes('sudo apt install ./Freedom-1.0.0-linux-amd64.deb'));
  assert.ok(await p.$('#authForm #btnLogin') && await p.$('#aUser'), 'форма входа на месте');
  assert.equal(await p.evaluate(() => typeof E2E), 'object', 'e2ee.js загрузился под CSP');
});
await scenario('Windows: первым идёт установщик .exe', { ua: 'windows' }, async (p) => {
  const it = await items(p);
  assert.equal(it[0].title, 'Windows'); assert.deepEqual(it.map((i) => i.rec), [true, false, false]);
});
await scenario('Android: блок показан без подсветки', { ua: 'android', vp: { width: 390, height: 844 }, shot: '/tmp/auth_mobile.png' }, async (p) => {
  assert.ok(await visible(p)); assert.ok((await items(p)).every((i) => !i.rec));
  const m = await p.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: innerWidth, box: document.querySelector('#authBox').getBoundingClientRect().width }));
  assert.ok(m.sw <= m.iw, `нет горизонтальной прокрутки (${m.sw} > ${m.iw})`); assert.ok(m.box <= m.iw);
  const sc = await p.evaluate(() => { const a = document.getElementById('auth'); return { scrolls: a.scrollHeight > a.clientHeight }; });
  assert.ok(sc.scrolls, 'на узком экране страница входа прокручивается'); 
  await p.evaluate(() => document.getElementById('dl').scrollIntoView());
  assert.ok(await p.$eval('#dl .dlBtn', (b) => { const r = b.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }), 'кнопка достижима прокруткой');
});
await scenario('в самом приложении блок не показывается', { ua: 'app' }, async (p) => { assert.equal(await visible(p), false); });
await scenario('файлов ещё нет: понятное сообщение', { m: 'empty' }, async (p) => {
  assert.ok(await visible(p)); assert.match(await p.$eval('#dlList', (e) => e.textContent), /ещё не загружены/);
});
await scenario('старый сервер (ошибка API): блок скрыт, вход работает', { m: 'error' }, async (p) => {
  assert.equal(await visible(p), false); assert.ok(await p.$('#btnLogin'));
});
await scenario('скачивание: заголовки и содержимое совпадают с хэшем из списка', {}, async (p) => {
  for (const it of listing()) {
    const r = await p.evaluate(async (u) => { const x = await fetch(u); const b = new Uint8Array(await x.arrayBuffer()); const h = await crypto.subtle.digest('SHA-256', b);
      return { cd: x.headers.get('content-disposition'), ct: x.headers.get('content-type'), sha: [...new Uint8Array(h)].map((v) => v.toString(16).padStart(2, '0')).join('') }; }, it.url);
    assert.match(r.cd, /^attachment/); assert.equal(r.ct, 'application/octet-stream'); assert.equal(r.sha, it.sha256);
  }
});
await browser.close(); server.close();
console.log(failed ? `\nПРОВАЛЕНО: ${failed}` : '\nВсе проверки в браузере прошли');
process.exit(failed ? 1 : 0);
