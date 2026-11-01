// Фаза 1: JS создаёт аккаунты alice и bob и артефакты для Python
import fs from 'node:fs';
const store = {};
globalThis.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; }, removeItem: (k) => delete store[k] };
(0, eval)(fs.readFileSync(new URL('../../server/web/e2ee.js', import.meta.url), 'utf8'));
const E = globalThis.E2E, T = E._t;
const ITER = 2000;                       // для скорости теста (в клиентах 310000, значение не влияет на формат)
T.ITER = ITER;
const out = { iters: ITER };

async function makeUser(name, pw) {
  const s = await (async () => {                // deriveSecrets с уменьшенным числом итераций
    const master = await T.pbkdf2(pw, T.U('freedom-e2ee-v1:' + name), ITER);
    return { auth: T.b64(await T.hkdf(master, 'auth')), kek: await T.hkdf(master, 'kek') };
  })();
  const id = await E.createIdentity();
  const blob = await E.sealBlob(id, s.kek, name);
  return { name, pw, auth: s.auth, kek: T.b64(s.kek), blob, id, dh: T.b64(id.dh.spki), sig: T.b64(id.sig.spki) };
}
const alice = await makeUser('alice', 'пароль-alice-Ω'), bob = await makeUser('bob', 'bob-secret');
// ключи, уже открытые из блоба (как после логина на новом устройстве)
const aliceId = await E.openBlob(alice.blob, T.unb64(alice.kek), 'alice');

const stub = { '/users/bob/keys': { dh_pub: bob.dh, sig_pub: bob.sig }, '/users/alice/keys': { dh_pub: alice.dh, sig_pub: alice.sig } };
E.init({ api: async (p) => stub[p], me: 'alice' });
E.setIdentity(aliceId, 'alice');

// alice создаёт эпоху 3 в комнате 7 для себя и bob
const K = crypto.getRandomValues(new Uint8Array(32));
const subtle = crypto.subtle;
const imp = (spki) => subtle.importKey('spki', T.unb64(spki), { name: 'ECDH', namedCurve: 'P-256' }, false, []);
const members = [{ username: 'alice', dhPub: await imp(alice.dh) }, { username: 'bob', dhPub: await imp(bob.dh) }];
out.envelopes = [];
for (const m of members) out.envelopes.push(await T.wrap(7, 3, K, m));
out.K = T.b64(K);

// сообщение: подготавливаем ключ так же, как в ensureKey (без сети): кладём через unwrap собственного конверта
const selfEnv = { ...out.envelopes[0], room_id: 7, epoch: 3, sender: 'alice' };
await T.unwrap(selfEnv);
out.msg = await E.encryptPayload(7, 3, { v: 1, t: 'text', text: 'Привет, мир! 🔒' });

// файл 2.5 МиБ + 5 байт (несколько блоков, неполный последний)
const plain = new Uint8Array(2 * 1048576 + 524288 + 5).map((_, i) => (i * 31 + 7) & 255);
const f = await E.encryptFile(new Blob([plain]));
out.file = { key: f.key, nonce: f.nonce, size: f.size, enc: T.b64(new Uint8Array(await f.blob.arrayBuffer())), sha: T.b64(new Uint8Array(await subtle.digest('SHA-256', plain))) };
// пустой файл
const f0 = await E.encryptFile(new Blob([]));
out.empty = { key: f0.key, nonce: f0.nonce, enc: T.b64(new Uint8Array(await f0.blob.arrayBuffer())) };

out.signal = await E.sealSignal('bob', 'call-123', { sdp: { type: 'offer', sdp: 'v=0\r\n…' } });
out.safety = await E.safetyNumber('alice', 'bob');
out.users = { alice: { pw: alice.pw, auth: alice.auth, blob: alice.blob, dh: alice.dh, sig: alice.sig }, bob: { pw: bob.pw, auth: bob.auth, blob: bob.blob, dh: bob.dh, sig: bob.sig } };
fs.writeFileSync('/tmp/js_out.json', JSON.stringify(out));
console.log('JS: артефакты созданы, safety =', out.safety);
