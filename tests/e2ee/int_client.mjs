import fs from 'node:fs';
const src = fs.readFileSync(new URL('../../server/web/e2ee.js', import.meta.url), 'utf8');
const BASE = 'http://127.0.0.1:8765';
function makeClient(name) {
  const ls = {}, localStorage = { getItem: (k) => ls[k] ?? null, setItem: (k, v) => { ls[k] = v; }, removeItem: (k) => delete ls[k] };
  const E = new Function('localStorage', src + '\nreturn E2E;')(localStorage);
  const c = { name, E, token: null };
  c.api = async (path, opts = {}) => {
    const headers = { 'Content-Type': 'application/json' }; if (c.token) headers.Authorization = 'Bearer ' + c.token;
    const r = await fetch(BASE + path, { method: opts.body ? 'POST' : 'GET', headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
    const j = await r.json();
    if (!r.ok) { const e = new Error(typeof j.detail === 'string' ? j.detail : 'err'); e.status = r.status; throw e; }
    return j;
  };
  E.init({ api: c.api, me: name });
  c.auth = async (register) => {   // ровно как authenticate() в app.js
    const { auth, kek } = await E.deriveSecrets(name, 'пароль-' + name);
    let r, ident;
    if (register) {
      const fresh = await E.createIdentity(), blob = await E.sealBlob(fresh, kek, name);
      r = await c.api('/register', { body: { username: name, password: auth, dh_pub: E.b64(fresh.dh.spki), sig_pub: E.b64(fresh.sig.spki), key_blob: blob } });
      ident = await E.openBlob(blob, kek, name);
    } else { r = await c.api('/login', { body: { username: name, password: auth } }); ident = await E.openBlob(r.key_blob, kek, name); }
    c.token = r.token; E.setIdentity(ident, name);
  };
  c.say = async (room, text) => {
    const epoch = await E.ensureKey(room), enc = await E.encryptPayload(room, epoch, { v: 1, t: 'text', text });
    return c.api('/_send', { body: { room_id: room, ...enc } });
  };
  c.read = async (room) => {
    const list = await c.api(`/rooms/${room}/messages`), out = [];
    for (const m of list) { try { out.push(m.username + ': ' + (await E.decryptMessage(m)).text); } catch (e) { out.push(m.username + ': <' + e.code + '>'); } }
    return out;
  };
  return c;
}
const eq = (a, b, msg) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${msg}\n  ожидали ${JSON.stringify(b)}\n  получили ${JSON.stringify(a)}`); console.log('OK —', msg); };

const A = makeClient('alice'), B = makeClient('bob');
await A.auth(true);
const grp = await A.api('/rooms', { body: { name: 'группа', kind: 'group' } });
await A.say(grp.id, 'привет из группы');
console.log('alice создала группу, эпоху 1 в ней и написала');
await B.auth(true);
await B.api(`/rooms/${grp.id}/join`, { body: {} });
eq(await B.read(grp.id), ['alice: <no_key>'], 'bob (вошёл позже) не может прочитать сообщение до вступления');
await B.say(grp.id, 'я bob');
eq(await B.read(grp.id), ['alice: <no_key>', 'bob: я bob'], 'bob после создания эпохи 2 читает свои сообщения');

// alice входит заново «на новом устройстве» из блоба (пароль -> kek -> ключи с сервера)
const A2 = makeClient('alice'); await A2.auth(false);
eq(await A2.read(grp.id), ['alice: привет из группы', 'bob: я bob'], 'alice на новом устройстве читает обе эпохи (ключи из конвертов)');

// личный чат и гонка: оба одновременно создают эпоху
const dm = await A.api('/dm', { body: { username: 'bob' } });
await Promise.all([A.E.ensureKey(dm.id), B.E.ensureKey(dm.id)]);
const ea = await A.api('/keys?room_id=' + dm.id), eb = await B.api('/keys?room_id=' + dm.id);
eq([ea.latest[dm.id], eb.latest[dm.id]], [1, 1], 'гонка за эпоху: победила одна, номер эпохи 1');
await A.say(dm.id, 'секрет alice'); await B.say(dm.id, 'секрет bob');
eq(await B.read(dm.id), ['alice: секрет alice', 'bob: секрет bob'], 'DM: bob читает обе реплики, независимо от победителя гонки');
eq(await A.read(dm.id), ['alice: секрет alice', 'bob: секрет bob'], 'DM: alice читает обе реплики');
eq(await A.E.safetyNumber('alice', 'bob'), await B.E.safetyNumber('bob', 'alice'), 'номер безопасности у обоих одинаковый');

// новый участник группы получает новую эпоху; старые участники подхватывают её
const C = makeClient('carol'); await C.auth(true);
await C.api(`/rooms/${grp.id}/join`, { body: {} });
await C.say(grp.id, 'я carol');
await B.E.fetchKeys(grp.id);
const rB = await B.read(grp.id);
eq(rB[rB.length - 1], 'carol: я carol', 'bob читает сообщение carol из эпохи 3 после fetchKeys');
eq((await C.read(grp.id)).filter((x) => x.includes('<')).length, 2, 'carol не читает то, что было до неё (2 сообщения недоступны)');

// подмена ключа сервером: alice уже закрепила ключ bob (TOFU), сервер подсовывает другой ключ при выдаче ключа комнаты
const room = await A.api('/rooms', { body: { name: 'тестовая' } });
await B.api(`/rooms/${room.id}/join`, { body: {} });
const rogue = await A.E.createIdentity(), orig = A.api;
A.E.init({ api: async (p, o) => { const r = await orig(p, o); if (/\/members$/.test(p)) r.forEach((m) => { if (m.username === 'bob') m.dh_pub = A.E.b64(rogue.dh.spki); }); return r; }, me: 'alice' });
let code = null;
try { await A.E.ensureKey(room.id); } catch (e) { code = e.code; }
eq(code, 'key_changed', 'подмена ключа bob сервером обнаружена: ключ комнаты на чужой ключ не выдаётся');
const srv = await A.api('/keys?room_id=' + room.id);
eq(srv.latest[room.id], 0, 'сервер так и не получил эпоху с подменённым ключом');
A.E.init({ api: orig, me: 'alice' });
await A.E.ensureKey(room.id);
eq((await A.api('/keys?room_id=' + room.id)).latest[room.id], 1, 'без подмены эпоха создаётся нормально');
console.log('всё проверено');
