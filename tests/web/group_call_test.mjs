// Групповые звонки (веб-клиент): адресация сигналов конкретному участнику, подключение новых, отключение
// вышедших, рендер плиток. RTCPeerConnection подменён фейком — настоящий WebRTC-стек здесь не нужен,
// сигналинг и его адресация проверяются отдельно от него (серверная часть — tests/server/group_call_test.py).
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
  await page.setViewport({ width: 900, height: 700 });
  try {
    await page.goto(URL_, { waitUntil: 'networkidle0' });
    await page.evaluate(() => {
      class FakePC {
        constructor() { this.connectionState = 'new'; this.onicecandidate = null; this.ontrack = null; this.onconnectionstatechange = null; this.localDescription = null; this.remoteDescription = null; this.closed = false; }
        addTrack() {}
        async createOffer() { return { type: 'offer', sdp: 'offer-sdp' }; }
        async createAnswer() { return { type: 'answer', sdp: 'answer-sdp' }; }
        async setLocalDescription(d) { this.localDescription = { type: d.type, sdp: d.sdp, toJSON: () => ({ type: d.type, sdp: d.sdp }) }; }
        async setRemoteDescription(d) { this.remoteDescription = d; }
        async addIceCandidate() {}
        close() { this.closed = true; this.connectionState = 'closed'; }
      }
      window.RTCPeerConnection = FakePC;
      window.api = async (u) => (u.includes('/webrtc/config') ? { iceServers: [] } : {});
      E2E.sealSignal = async (username, callId, data) => ({ n: 'N', c: JSON.stringify({ username, data }) });
      E2E.openSignal = async (username, callId, sealed) => JSON.parse(sealed.c).data;
      // определяем здесь же (не в Node) — иначе функция stream.getTracks потерялась бы при сериализации
      // аргумента page.evaluate(fn, arg): puppeteer гоняет его через JSON, а JSON функции не хранит
      window.seedCall = (overrides = {}) => ({
        id: 'c1', roomId: 1, roomName: 'Команда', video: false, outgoing: true, group: true,
        phase: 'active', stream: { getTracks: () => [] }, peers: {}, t0: null, ...overrides,
      });
    });
    await check(page);
    assert.deepEqual(problems.filter((p) => !/favicon/.test(p)), [], 'ошибки в консоли');
    console.log('OK —', name);
  } catch (e) { failed++; console.log('FAIL —', name, '\n   ', e.message.split('\n').slice(0, 8).join('\n    ')); }
  await page.close();
}

await scenario('onJoined: новичок сам предлагает оффер каждому уже подключённому, адресуя call_signal по to', async (page) => {
  const r = await page.evaluate(async () => {
    S.call = window.seedCall({ phase: 'connecting' });
    const sent = [];
    window.wsSend = (p) => { sent.push(p); return true; };
    await onJoined({ call_id: 'c1', participants: [{ id: 2, username: 'bob' }, { id: 3, username: 'carol' }] });
    return { sent, peers: Object.keys(S.call.peers) };
  });
  assert.deepEqual(r.peers.sort(), ['2', '3']);
  const signals = r.sent.filter((p) => p.type === 'call_signal');
  assert.equal(signals.length, 2);
  assert.deepEqual(signals.map((s) => s.to).sort(), [2, 3]);
});

await scenario('onPeerJoined: готовит приёмное соединение, но оффер НЕ отправляет (ждёт оффер от новичка)', async (page) => {
  const r = await page.evaluate(async () => {
    S.call = window.seedCall();
    const sent = [];
    window.wsSend = (p) => { sent.push(p); return true; };
    await onPeerJoined({ call_id: 'c1', user_id: 5, username: 'dave' });
    return { sent, peers: Object.keys(S.call.peers) };
  });
  assert.deepEqual(r.peers, ['5']);
  assert.equal(r.sent.filter((p) => p.type === 'call_signal').length, 0, 'существующий участник не должен сам звонить новичку');
});

await scenario('onSignal: чужой оффер порождает ответ, адресованный обратно тому же from_id', async (page) => {
  const r = await page.evaluate(async () => {
    S.call = window.seedCall();
    const sent = [];
    window.wsSend = (p) => { sent.push(p); return true; };
    onSignal({ call_id: 'c1', from: 'bob', from_id: 2, data: { n: 'N', c: JSON.stringify({ username: 'bob', data: { sdp: { type: 'offer', sdp: 'x' } } }) } });
    await new Promise((res) => setTimeout(res, 30));
    return { sent, peerExists: !!S.call.peers[2] };
  });
  assert.equal(r.peerExists, true);
  const answer = r.sent.find((p) => p.type === 'call_signal');
  assert.equal(answer.to, 2);
});

await scenario('onPeerLeft: закрывает именно то соединение и убирает плитку, других не трогает', async (page) => {
  const r = await page.evaluate(async () => {
    S.call = window.seedCall();
    S.call.peers[2] = { username: 'bob', pc: new RTCPeerConnection(), pending: [], remoteSet: true, chain: Promise.resolve(), stream: null };
    S.call.peers[3] = { username: 'carol', pc: new RTCPeerConnection(), pending: [], remoteSet: true, chain: Promise.resolve(), stream: null };
    onPeerLeft({ call_id: 'c1', user_id: 2 });
    return { bobClosed: S.call.peers[2]?.pc?.closed, has2: 2 in S.call.peers, has3: 3 in S.call.peers, carolClosed: S.call.peers[3].pc.closed };
  });
  assert.equal(r.has2, false);
  assert.equal(r.has3, true);
  assert.equal(r.carolClosed, false, 'соединение с оставшимся участником не должно закрываться');
});

await scenario('callUi: плитка на каждого участника с подписью имени, аудиозвонок без видео', async (page) => {
  const r = await page.evaluate(() => {
    S.call = window.seedCall({ video: false });
    S.call.peers = {
      2: { username: 'bob', pc: {}, pending: [], remoteSet: true, chain: Promise.resolve(), stream: null },
      3: { username: 'carol', pc: {}, pending: [], remoteSet: true, chain: Promise.resolve(), stream: null },
    };
    callUi();
    return {
      tiles: [...document.querySelectorAll('.callTile .tileLabel')].map((e) => e.textContent),
      hasVideoEls: document.querySelectorAll('.callTile video').length,
      audioOnly: $('#callUi').classList.contains('audio-only'),
      name: $('#callName').textContent,
    };
  });
  assert.deepEqual(r.tiles.sort(), ['bob', 'carol']);
  assert.equal(r.hasVideoEls, 0, 'аудиозвонок — видео-тегов в плитках быть не должно');
  assert.equal(r.audioOnly, true);
  assert.equal(r.name, 'Команда');
});

await scenario('hangup: пока не принял входящий — call_reject; в любой другой фазе — call_end', async (page) => {
  const r1 = await page.evaluate(() => {
    S.call = window.seedCall({ phase: 'incoming', outgoing: false });
    let sent = null;
    window.wsSend = (p) => { sent = p; return true; };
    hangup();
    return sent.type;
  });
  assert.equal(r1, 'call_reject');

  const r2 = await page.evaluate(() => {
    S.call = window.seedCall({ phase: 'ringing', outgoing: true });
    let sent = null;
    window.wsSend = (p) => { sent = p; return true; };
    hangup();
    return sent.type;
  });
  assert.equal(r2, 'call_end');
});

await scenario('кнопки звонка больше не скрыты для групповых комнат (раньше — только личные чаты)', async (page) => {
  const hidden = await page.evaluate(() => {
    S.me = 'alice'; S.online = true;
    S.rooms = [{ id: 1, name: 'Команда', is_direct: false, unread: 0, sort_ts: 1 }];
    S.cur = 1; S.loaded.add(1);
    document.body.classList.add('in-chat');
    $('#empty').classList.add('hidden'); $('#chatView').classList.remove('hidden');
    const r = S.rooms[0];
    $('#title').textContent = r.name;
    const av = avatarEl('avatar', null, r.name); $('#chatAvatar').replaceWith(av); av.id = 'chatAvatar';
    $('#callAudio').hidden = $('#callVideo').hidden = false;
    return { audio: $('#callAudio').hidden, video: $('#callVideo').hidden };
  });
  assert.deepEqual(hidden, { audio: false, video: false });
});

await browser.close(); server.close();
console.log(failed ? `\nПРОВАЛЕНО: ${failed}` : '\nВсе проверки группового звонка (веб) прошли');
process.exit(failed ? 1 : 0);
