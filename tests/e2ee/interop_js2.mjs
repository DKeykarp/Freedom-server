import fs from 'node:fs';
const store = {};
globalThis.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; }, removeItem: (k) => delete store[k] };
(0, eval)(fs.readFileSync(new URL('../../server/web/e2ee.js', import.meta.url), 'utf8'));
const E = globalThis.E2E, T = E._t;
const js = JSON.parse(fs.readFileSync('/tmp/js_out.json')), py = JSON.parse(fs.readFileSync('/tmp/py_out.json'));
const A = js.users.alice, B = js.users.bob;
const master = await T.pbkdf2(A.pw, T.U('freedom-e2ee-v1:alice'), js.iters);
const id = await E.openBlob(A.blob, await T.hkdf(master, 'kek'), 'alice');
const stub = { '/users/bob/keys': { dh_pub: B.dh, sig_pub: B.sig } };
E.init({ api: async (p) => stub[p], me: 'alice' });
E.setIdentity(id, 'alice');

await T._t?.x;
await T.unwrap({ ...py.env, room_id: 9, epoch: 1, sender: 'bob' });
const pl = await E.decryptMessage({ room_id: 9, epoch: 1, username: 'bob', ct: py.msg.ct, sig: py.msg.sig });
if (pl.t !== 'audio' || pl.dur !== 3.5) throw new Error('payload mismatch');
console.log('JS: конверт и сообщение от Python — OK');

const plain = await E.decryptFile(new Blob([T.unb64(py.file.enc)]), py.file.key, py.file.nonce, 'audio/mp4');
const sha = T.b64(new Uint8Array(await crypto.subtle.digest('SHA-256', await plain.arrayBuffer())));
if (sha !== py.file.sha) throw new Error('file mismatch');
console.log('JS: файл от Python — OK');

const s = await E.openSignal('bob', 'call-999', py.signal);
if (!s.candidate.candidate.startsWith('candidate:1')) throw new Error('signal mismatch');
console.log('JS: сигнал от Python — OK');

// негативные проверки
const bad = { room_id: 9, epoch: 1, username: 'bob', ct: py.msg.ct, sig: py.msg.sig };
async function mustFail(name, fn, code) { try { await fn(); } catch (e) { if (!code || e.code === code) { console.log('JS: отказ как и должно быть —', name); return; } throw e; } throw new Error('НЕ отклонено: ' + name); }
await mustFail('подмена отправителя', () => E.decryptMessage({ ...bad, username: 'alice' }), 'bad_sig');
const ctBad = T.unb64(py.msg.ct); ctBad[20] ^= 1;
await mustFail('изменённый шифртекст', () => E.decryptMessage({ ...bad, ct: T.b64(ctBad) }), 'bad_sig');
await mustFail('другая комната (AAD)', () => E.decryptMessage({ ...bad, room_id: 10 }), 'no_key');
await mustFail('чужой звонок (call_id)', () => E.openSignal('bob', 'call-000', py.signal));
await mustFail('отражённый сигнал (from=me)', () => E.openSignal('alice', 'call-999', py.signal));
await mustFail('неверный пароль для блоба', async () => E.openBlob(A.blob, await T.hkdf(await T.pbkdf2('wrong', T.U('freedom-e2ee-v1:alice'), js.iters), 'kek'), 'alice'), 'bad_blob');
