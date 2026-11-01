import fs from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';

const src = fs.readFileSync(new URL('../../server/web/downloads.js', import.meta.url), 'utf8');
const D = new Function(src + '\nreturn Downloads;')();     // в Node нет document: автозапуск пропускается

const UA = {
  win: { userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36', platform: 'Win32' },
  win11ch: { userAgentData: { platform: 'Windows' }, userAgent: 'Mozilla/5.0 (Windows NT 10.0)', platform: 'Win32' },
  linux: { userAgent: 'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0', platform: 'Linux x86_64' },
  ubuntu: { userAgent: 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0', platform: 'Linux x86_64' },
  android: { userAgent: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36', platform: 'Linux armv81' },
  iphone: { userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148', platform: 'iPhone' },
  mac: { userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15', platform: 'MacIntel' },
  cros: { userAgent: 'Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36', platform: 'Linux x86_64' },
  bot: { userAgent: 'curl/8.0', platform: '' },
};

test('определение платформы', () => {
  assert.equal(D.detectPlatform(UA.win), 'windows');
  assert.equal(D.detectPlatform(UA.win11ch), 'windows');
  assert.equal(D.detectPlatform(UA.linux), 'linux');
  assert.equal(D.detectPlatform(UA.ubuntu), 'linux');
  assert.equal(D.detectPlatform(UA.android), 'mobile');     // в UA Android есть «Linux»
  assert.equal(D.detectPlatform(UA.iphone), 'mobile');      // в UA iPhone есть «like Mac OS X»
  assert.equal(D.detectPlatform(UA.mac), 'mac');            // «Darwin»/«Mac» не должны принимаcься за Windows
  assert.equal(D.detectPlatform(UA.bot), 'other');
});

test('приложение не показывает блок само себе', () => {
  assert.equal(D.isDesktopApp({ userAgent: 'Mozilla/5.0 (X11; Linux) Electron/31.7.7 Freedom/1.0.0 FreedomDesktop/1.0.0' }), true);
  assert.equal(D.isDesktopApp(UA.linux), false);
});

test('размер файла', () => {
  assert.equal(D.formatSize(0), '1\u00a0КБ');
  assert.equal(D.formatSize(2048), '2\u00a0КБ');
  assert.equal(D.formatSize(5.5 * 1048576), '5.5\u00a0МБ');
  assert.equal(D.formatSize(78.4 * 1048576), '78\u00a0МБ');
  assert.equal(D.formatSize(undefined), '');
});

const ITEMS = [
  { name: 'Freedom-Setup-1.0.0.exe', url: '/downloads/Freedom-Setup-1.0.0.exe', kind: 'exe', platform: 'windows', size: 80 * 1048576, sha256: 'aa'.repeat(32), version: '1.0.0' },
  { name: 'Freedom-1.0.0-linux-x64.deb', url: '/downloads/Freedom-1.0.0-linux-x64.deb', kind: 'deb', platform: 'linux', size: 70 * 1048576, sha256: 'bb'.repeat(32), version: '1.0.0' },
  { name: 'Freedom-1.0.0-linux-x64.rpm', url: '/downloads/Freedom-1.0.0-linux-x64.rpm', kind: 'rpm', platform: 'linux', size: 70 * 1048576, sha256: 'cc'.repeat(32), version: '1.0.0' },
];

test('порядок: своя платформа первой', () => {
  assert.deepEqual(D.orderItems(ITEMS, 'linux').map((i) => i.kind), ['deb', 'rpm', 'exe']);
  assert.deepEqual(D.orderItems(ITEMS, 'windows').map((i) => i.kind), ['exe', 'deb', 'rpm']);
  assert.deepEqual(D.orderItems(ITEMS, 'mac').map((i) => i.kind), ['exe', 'deb', 'rpm']);
});

// минимальная подмена DOM
class Node_ { constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.className = ''; this._text = ''; }
  append(...c) { this.children.push(...c); } setAttribute(k, v) { this.attrs[k] = v; }
  set textContent(v) { this._text = v; if (v === '') this.children = []; } get textContent() { return this._text; } }
const doc = { createElement: (t) => new Node_(t) };
const walk = (n, f) => { f(n); n.children.forEach((c) => walk(c, f)); };
const all = (n, pred) => { const out = []; walk(n, (x) => pred(x) && out.push(x)); return out; };

test('разметка: кнопки, инструкции, хэши, подсветка своей платформы', () => {
  const list = new Node_('div');
  D.render(doc, list, ITEMS, 'linux');
  const items = list.children;
  assert.equal(items.length, 3);
  assert.deepEqual(items.map((i) => i.className), ['dlItem dlMine', 'dlItem dlMine', 'dlItem']);
  const links = all(list, (n) => n.tag === 'a');
  assert.deepEqual(links.map((a) => a.attrs.href), ITEMS.slice(1).concat(ITEMS[0]).map((i) => i.url));
  assert.ok(links.every((a) => a.attrs.download && a.textContent === 'Скачать'));
  const codes = all(list, (n) => n.tag === 'code').map((c) => c.textContent);
  assert.ok(codes.includes('sudo apt install ./Freedom-1.0.0-linux-x64.deb'));
  assert.ok(codes.some((c) => c.startsWith('sudo dnf install ./Freedom-1.0.0-linux-x64.rpm') && c.includes('zypper')));
  assert.ok(codes.includes('bb'.repeat(32)) && codes.includes('aa'.repeat(32)));
  const subs = all(list, (n) => n.tag === 'small').map((s) => s.textContent);
  assert.ok(subs.some((s) => s.includes('Пакет .deb') && s.includes('70\u00a0МБ') && s.includes('v1.0.0')));
});

test('пустой список и неизвестный тип', () => {
  const list = new Node_('div');
  D.render(doc, list, [], 'linux');
  assert.match(list.children[0].textContent, /ещё не загружены/);
  D.render(doc, list, [{ name: 'Freedom-1.dmg', url: '/x', kind: 'dmg', platform: 'mac', size: 1, sha256: 'x' }], 'mac');
  assert.equal(list.children.length, 0);
});

test('текст из сервера вставляется как текст, а не как HTML', () => {
  const list = new Node_('div');
  D.render(doc, list, [{ ...ITEMS[1], name: 'Freedom-<img onerror=x>.deb' }], 'linux');
  const html = all(list, (n) => n.tag === 'code')[0].textContent;
  assert.match(html, /<img onerror=x>/);                                   // остался буквальным текстом
  assert.ok(!src.includes('innerHTML'), 'downloads.js не использует innerHTML');
});

// регрессия: Android .apk сервер отдаёт в /api/downloads (platform: 'android'), но раньше в INFO не было записи
// для 'apk', и render() молча пропускал такой элемент — кнопки «Скачать» для Android на странице не появлялось
const APK = { name: 'Freedom-5.1.apk', url: '/downloads/Freedom-5.1.apk', kind: 'apk', platform: 'android', size: 30 * 1048576, sha256: 'dd'.repeat(32), version: '5.1' };

test('Android .apk: кнопка скачивания реально рисуется (а не пропускается молча)', () => {
  const list = new Node_('div');
  D.render(doc, list, ITEMS.concat(APK), 'windows');
  const links = all(list, (n) => n.tag === 'a');
  assert.ok(links.some((a) => a.attrs.href === '/downloads/Freedom-5.1.apk'), 'ссылка на apk должна быть в разметке');
  const texts = all(list, (n) => n.tag === 'b').map((b) => b.textContent);
  assert.ok(texts.includes('Android'));
  const meta = all(list, (n) => n.tag === 'small').map((s) => s.textContent).join('|');
  assert.ok(meta.includes('v5.1'), 'номер версии из имени файла показывается');
});

test('Android .apk: подсказка по установке и приоритет для пользователя Android', () => {
  assert.match(D.installHint(APK), /разрешить установку/);
  const list = new Node_('div');
  D.render(doc, list, ITEMS.concat(APK), 'android');
  assert.equal(list.children[0].className, 'dlItem dlMine', 'своя платформа (android) идёт первой и подсвечена');
  assert.equal(all(list.children[0], (n) => n.tag === 'b')[0].textContent, 'Android');
});
