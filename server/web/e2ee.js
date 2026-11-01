'use strict';
/*
 * Freedom E2EE — протокол FRDM-E2EE v1 (описание в E2EE.md).
 * Только WebCrypto: P-256 (ECDH + ECDSA), AES-256-GCM, HKDF-SHA256, PBKDF2-SHA256.
 * Сервер видит только шифртекст, публичные ключи и метаданные.
 */
const E2E = (() => {
  const subtle = globalThis.crypto && globalThis.crypto.subtle;
  const enc = new TextEncoder(), dec = new TextDecoder();
  const ITER = 310000;              // итерации PBKDF2
  const CH = 1048576;               // размер блока шифрования файла (1 МиБ)
  const ECDH = { name: 'ECDH', namedCurve: 'P-256' };
  const ECDSA = { name: 'ECDSA', namedCurve: 'P-256' };
  const SIGN = { name: 'ECDSA', hash: 'SHA-256' };

  let api = null, me = null, ident = null;         // ident: { dh:{priv,spki}, sig:{priv,spki} }
  const peers = new Map();                          // username -> { dhPub, sigPub, dhSpki, sigSpki }
  const roomKeys = new Map();                       // "room:epoch" -> CryptoKey (ключ сообщений)
  const latest = new Map();                         // room -> актуальная эпоха на сервере
  const missTried = new Set(), badEnv = new Set();

  class E2EError extends Error { constructor(code, msg) { super(msg || code); this.code = code; } }

  /* ---------- байты ---------- */
  const U = (s) => enc.encode(s);
  const b64 = (buf) => {
    const a = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    let s = '';
    for (let i = 0; i < a.length; i += 0x8000) s += String.fromCharCode.apply(null, a.subarray(i, i + 0x8000));
    return btoa(s);
  };
  const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  const cat = (...parts) => {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let o = 0;
    for (const p of parts) { out.set(p, o); o += p.length; }
    return out;
  };
  const rnd = (n) => crypto.getRandomValues(new Uint8Array(n));
  const u32 = (n) => new Uint8Array([(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255]);
  const ZERO = new Uint8Array([0]);

  /* ---------- примитивы ---------- */
  async function hkdf(ikm, info, len = 32) {
    const k = await subtle.importKey('raw', ikm, 'HKDF', false, ['deriveBits']);
    const bits = await subtle.deriveBits(
      { name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(0), info: typeof info === 'string' ? U(info) : info }, k, len * 8);
    return new Uint8Array(bits);
  }
  async function pbkdf2(password, salt, iterations) {
    const k = await subtle.importKey('raw', U(password), 'PBKDF2', false, ['deriveBits']);
    return new Uint8Array(await subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations }, k, 256));
  }
  const aesKey = (raw) => subtle.importKey('raw', raw, 'AES-GCM', false, ['encrypt', 'decrypt']);
  const gcmEnc = async (key, iv, data, aad) =>
    new Uint8Array(await subtle.encrypt({ name: 'AES-GCM', iv, additionalData: aad, tagLength: 128 }, key, data));
  const gcmDec = async (key, iv, data, aad) =>
    new Uint8Array(await subtle.decrypt({ name: 'AES-GCM', iv, additionalData: aad, tagLength: 128 }, key, data));
  const sha256 = async (...parts) => new Uint8Array(await subtle.digest('SHA-256', cat(...parts)));
  const verify = (pub, sig, data) => subtle.verify(SIGN, pub, sig, data);
  const sign = async (priv, data) => new Uint8Array(await subtle.sign(SIGN, priv, data));   // r||s, 64 байта
  const ecdh = async (priv, pub) => new Uint8Array(await subtle.deriveBits({ name: 'ECDH', public: pub }, priv, 256));

  /* ---------- пароль -> секреты ---------- */
  async function deriveSecrets(username, password) {
    if (!subtle) throw new E2EError('insecure', 'Для шифрования нужен HTTPS (или localhost)');
    const master = await pbkdf2(password, U('freedom-e2ee-v1:' + username), ITER);
    return { auth: b64(await hkdf(master, 'auth')), kek: await hkdf(master, 'kek') };
  }

  /* ---------- личность ---------- */
  async function createIdentity() {
    const dh = await subtle.generateKey(ECDH, true, ['deriveBits']);
    const sg = await subtle.generateKey(ECDSA, true, ['sign', 'verify']);
    return {
      dh: { priv: dh.privateKey, spki: new Uint8Array(await subtle.exportKey('spki', dh.publicKey)) },
      sig: { priv: sg.privateKey, spki: new Uint8Array(await subtle.exportKey('spki', sg.publicKey)) },
    };
  }

  /** Приватные ключи, зашифрованные kek (ключ из пароля). Именно это хранит сервер. */
  async function sealBlob(id, kek, username) {
    const plain = U(JSON.stringify({
      dh: b64(await subtle.exportKey('pkcs8', id.dh.priv)), dhPub: b64(id.dh.spki),
      sig: b64(await subtle.exportKey('pkcs8', id.sig.priv)), sigPub: b64(id.sig.spki),
    }));
    const n = rnd(12);
    const c = await gcmEnc(await aesKey(kek), n, plain, U('FRDM-KEYBLOB-v1|' + username));
    return JSON.stringify({ v: 1, n: b64(n), c: b64(c) });
  }

  async function openBlob(blob, kek, username) {
    let o;
    try { o = JSON.parse(blob); } catch { throw new E2EError('bad_blob'); }
    let p;
    try { p = JSON.parse(dec.decode(await gcmDec(await aesKey(kek), unb64(o.n), unb64(o.c), U('FRDM-KEYBLOB-v1|' + username)))); }
    catch { throw new E2EError('bad_blob', 'Не удалось расшифровать ключи'); }
    return {
      dh: { priv: await subtle.importKey('pkcs8', unb64(p.dh), ECDH, false, ['deriveBits']), spki: unb64(p.dhPub) },
      sig: { priv: await subtle.importKey('pkcs8', unb64(p.sig), ECDSA, false, ['sign']), spki: unb64(p.sigPub) },
    };
  }

  /* ---------- локальное хранилище (IndexedDB, ключи неизвлекаемые) ---------- */
  const idb = () => new Promise((res, rej) => {
    const r = indexedDB.open('freedom-e2ee', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('kv');
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
  async function idbOp(mode, fn) {
    const db = await idb();
    return new Promise((res, rej) => {
      const tx = db.transaction('kv', mode), req = fn(tx.objectStore('kv'));
      tx.oncomplete = () => { db.close(); res(req && req.result); };
      tx.onerror = () => { db.close(); rej(tx.error); };
    });
  }
  const saveIdentity = (username, id) => idbOp('readwrite', (s) => s.put({ username, ...id }, 'identity'));
  async function loadIdentity(username) {
    const v = await idbOp('readonly', (s) => s.get('identity'));
    return v && v.username === username ? { dh: v.dh, sig: v.sig } : null;
  }
  const clearLocal = async () => { try { await idbOp('readwrite', (s) => s.delete('identity')); } catch { /* нет IDB */ } };

  /* ---------- сессия ---------- */
  function init(opts) { api = opts.api; me = opts.me; }
  function setIdentity(id, username) {
    ident = id; me = username;
    peers.clear(); roomKeys.clear(); latest.clear(); missTried.clear(); badEnv.clear();
    pin(username, b64(id.dh.spki), b64(id.sig.spki));       // свои ключи тоже закрепляем
  }
  const ready = () => !!ident;
  function reset() { ident = null; me = null; peers.clear(); roomKeys.clear(); latest.clear(); missTried.clear(); badEnv.clear(); }

  /* ---------- закрепление ключей собеседников (TOFU) ---------- */
  const readPins = () => { try { return JSON.parse(localStorage.getItem('frdm_pins') || '{}'); } catch { return {}; } };
  function pin(username, dhB64, sigB64) {
    const pins = readPins(), v = dhB64 + '|' + sigB64;
    if (pins[username] === undefined) { pins[username] = v; localStorage.setItem('frdm_pins', JSON.stringify(pins)); }
    else if (pins[username] !== v) throw new E2EError('key_changed', `Ключ безопасности пользователя ${username} изменился`);
  }
  async function loadPeer(username, dhB64, sigB64) {
    pin(username, dhB64, sigB64);
    const dhSpki = unb64(dhB64), sigSpki = unb64(sigB64);
    const p = {
      dhSpki, sigSpki,
      dhPub: await subtle.importKey('spki', dhSpki, ECDH, false, []),
      sigPub: await subtle.importKey('spki', sigSpki, ECDSA, false, ['verify']),
    };
    peers.set(username, p);
    return p;
  }
  async function peer(username) {
    if (peers.has(username)) return peers.get(username);
    const pinned = readPins()[username];
    if (pinned) { const [d, s] = pinned.split('|'); return loadPeer(username, d, s); }
    const r = await api('/users/' + encodeURIComponent(username) + '/keys');
    return loadPeer(username, r.dh_pub, r.sig_pub);
  }
  async function memberKeys(roomId) {
    const list = await api(`/rooms/${roomId}/members`);
    const out = [];
    for (const m of list) out.push({ username: m.username, ...(await loadPeer(m.username, m.dh_pub, m.sig_pub)) });
    return out;
  }

  /* ---------- ключи комнат ---------- */
  const rk = (room, epoch) => `${room}:${epoch}`;
  const msgKey = async (K) => aesKey(await hkdf(K, 'FRDM-MSG-KEY-v1'));

  async function wrap(roomId, epoch, K, member) {
    const eph = await subtle.generateKey(ECDH, true, ['deriveBits']);
    const ephSpki = new Uint8Array(await subtle.exportKey('spki', eph.publicKey));
    const info = U(`FRDM-WRAP-v1|${roomId}|${epoch}|${me}|${member.username}`);
    const wk = await aesKey(await hkdf(await ecdh(eph.privateKey, member.dhPub), info));
    const n = rnd(12), c = await gcmEnc(wk, n, K, info);
    const sig = await sign(ident.sig.priv,
      cat(U(`FRDM-ENV-v1|${roomId}|${epoch}|${me}|${member.username}`), ZERO, ephSpki, n, c));
    return { recipient: member.username, eph: b64(ephSpki), n: b64(n), c: b64(c), sig: b64(sig) };
  }

  async function unwrap(e) {
    const s = await peer(e.sender);
    const eph = unb64(e.eph), n = unb64(e.n), c = unb64(e.c);
    const okSig = await verify(s.sigPub, unb64(e.sig),
      cat(U(`FRDM-ENV-v1|${e.room_id}|${e.epoch}|${e.sender}|${me}`), ZERO, eph, n, c));
    if (!okSig) throw new E2EError('bad_sig', 'Подпись ключа комнаты неверна');
    const ephPub = await subtle.importKey('spki', eph, ECDH, false, []);
    const info = U(`FRDM-WRAP-v1|${e.room_id}|${e.epoch}|${e.sender}|${me}`);
    const wk = await aesKey(await hkdf(await ecdh(ident.dh.priv, ephPub), info));
    roomKeys.set(rk(e.room_id, e.epoch), await msgKey(await gcmDec(wk, n, c, info)));
  }

  async function fetchKeys(roomId) {
    const r = await api('/keys' + (roomId != null ? `?room_id=${roomId}` : ''));
    for (const [rid, ep] of Object.entries(r.latest)) latest.set(+rid, ep);
    for (const e of r.envelopes) {
      const k = rk(e.room_id, e.epoch);
      if (roomKeys.has(k) || badEnv.has(k)) continue;
      try { await unwrap(e); } catch (err) { badEnv.add(k); console.warn('E2EE: ключ', k, err.message); }
    }
  }

  function noteEpoch(roomId, epoch) { if (epoch > (latest.get(roomId) || 0)) latest.set(roomId, epoch); }
  const haveKey = (roomId, epoch) => roomKeys.has(rk(roomId, epoch));

  /** Гарантирует, что у нас есть ключ актуальной эпохи; если нас в ней нет — создаёт новую эпоху. */
  async function ensureKey(roomId) {
    for (let attempt = 0; attempt < 4; attempt++) {
      let ep = latest.get(roomId) || 0;
      if (ep && haveKey(roomId, ep)) return ep;
      await fetchKeys(roomId);
      ep = latest.get(roomId) || 0;
      if (ep && haveKey(roomId, ep)) return ep;
      const members = await memberKeys(roomId);          // проверяет закреплённые ключи
      const K = rnd(32), next = ep + 1, envelopes = [];
      for (const m of members) envelopes.push(await wrap(roomId, next, K, m));
      try {
        await api(`/rooms/${roomId}/keys`, { body: { epoch: next, envelopes } });
      } catch (err) {
        if (err.status === 409) continue;                // кто-то опередил — берём его эпоху
        throw err;
      }
      roomKeys.set(rk(roomId, next), await msgKey(K));
      latest.set(roomId, next);
      return next;
    }
    throw new E2EError('no_key', 'Не удалось согласовать ключ комнаты');
  }

  /* ---------- сообщения ---------- */
  async function encryptPayload(roomId, epoch, payload) {
    const key = roomKeys.get(rk(roomId, epoch));
    if (!key) throw new E2EError('no_key');
    const aad = U(`FRDM-MSG-v1|${roomId}|${epoch}|${me}`);
    const n = rnd(12), ct = cat(n, await gcmEnc(key, n, U(JSON.stringify(payload)), aad));
    return { epoch, ct: b64(ct), sig: b64(await sign(ident.sig.priv, cat(aad, ZERO, ct))) };
  }

  /** msg: {room_id, username, epoch, ct, sig}. Бросает E2EError(no_key | bad_sig | bad_data). */
  async function decryptMessage(msg) {
    const k = rk(msg.room_id, msg.epoch);
    if (!roomKeys.has(k) && !missTried.has(k)) { missTried.add(k); await fetchKeys(msg.room_id).catch(() => {}); }
    const key = roomKeys.get(k);
    if (!key) throw new E2EError('no_key');
    const s = await peer(msg.username);
    const aad = U(`FRDM-MSG-v1|${msg.room_id}|${msg.epoch}|${msg.username}`), ct = unb64(msg.ct);
    if (!(await verify(s.sigPub, unb64(msg.sig), cat(aad, ZERO, ct)))) throw new E2EError('bad_sig');
    try { return JSON.parse(dec.decode(await gcmDec(key, ct.subarray(0, 12), ct.subarray(12), aad))); }
    catch { throw new E2EError('bad_data'); }
  }

  /* ---------- файлы (блоки по 1 МиБ, у каждого файла свой ключ) ---------- */
  const chunkNonce = (base, i) => { const n = base.slice(), c = u32(i); for (let j = 0; j < 4; j++) n[8 + j] ^= c[j]; return n; };
  const chunkAad = (i, last) => cat(U('FRDM-CHUNK-v1'), u32(i), new Uint8Array([last ? 1 : 0]));

  async function encryptFile(blob) {
    const k = rnd(32), base = rnd(12), key = await aesKey(k);
    const n = Math.max(1, Math.ceil(blob.size / CH)), parts = [];
    for (let i = 0; i < n; i++) {
      const p = new Uint8Array(await blob.slice(i * CH, Math.min(blob.size, (i + 1) * CH)).arrayBuffer());
      parts.push(await gcmEnc(key, chunkNonce(base, i), p, chunkAad(i, i === n - 1)));
    }
    return { blob: new Blob(parts), key: b64(k), nonce: b64(base), size: blob.size };
  }
  async function decryptFile(encBlob, keyB64, nonceB64, mime) {
    const key = await aesKey(unb64(keyB64)), base = unb64(nonceB64), step = CH + 16;
    const n = Math.max(1, Math.ceil(encBlob.size / step)), parts = [];
    for (let i = 0; i < n; i++) {
      const c = new Uint8Array(await encBlob.slice(i * step, Math.min(encBlob.size, (i + 1) * step)).arrayBuffer());
      parts.push(await gcmDec(key, chunkNonce(base, i), c, chunkAad(i, i === n - 1)));
    }
    return new Blob(parts, { type: mime || 'application/octet-stream' });
  }

  /* ---------- сигналинг звонков ---------- */
  async function callKey(peerName, callId) {
    const p = await peer(peerName);
    return aesKey(await hkdf(await ecdh(ident.dh.priv, p.dhPub), 'FRDM-CALL-v1|' + callId));
  }
  async function sealSignal(peerName, callId, obj) {
    const n = rnd(12);
    const c = await gcmEnc(await callKey(peerName, callId), n, U(JSON.stringify(obj)), U(`FRDM-SIG-v1|${callId}|${me}|${peerName}`));
    return { n: b64(n), c: b64(c) };
  }
  async function openSignal(peerName, callId, data) {
    const p = await gcmDec(await callKey(peerName, callId), unb64(data.n), unb64(data.c), U(`FRDM-SIG-v1|${callId}|${peerName}|${me}`));
    return JSON.parse(dec.decode(p));
  }

  /* ---------- номер безопасности ---------- */
  async function fingerprint(username, dhSpki, sigSpki) { return sha256(U(`FRDM-FP-v1|${username}|`), dhSpki, sigSpki); }
  async function safetyNumber(a, b) {
    const info = async (u) => {
      if (u === me) return { u, fp: await fingerprint(u, ident.dh.spki, ident.sig.spki) };
      const p = await peer(u);
      return { u, fp: await fingerprint(u, p.dhSpki, p.sigSpki) };
    };
    const pair = [await info(a), await info(b)].sort((x, y) => (x.u < y.u ? -1 : x.u > y.u ? 1 : 0));
    const h = await sha256(pair[0].fp, pair[1].fp), groups = [];
    for (let i = 0; i < 8; i++) {
      const v = ((h[4 * i] << 24) | (h[4 * i + 1] << 16) | (h[4 * i + 2] << 8) | h[4 * i + 3]) >>> 0;
      groups.push(String(v % 100000).padStart(5, '0'));
    }
    return groups.join(' ');
  }

  return {
    supported: !!subtle, E2EError, b64, unb64, init, ready, reset, deriveSecrets, createIdentity, sealBlob, openBlob,
    setIdentity, saveIdentity, loadIdentity, clearLocal, ensureKey, fetchKeys, noteEpoch, haveKey,
    encryptPayload, decryptMessage, encryptFile, decryptFile, sealSignal, openSignal, safetyNumber,
    _t: { hkdf, pbkdf2, b64, unb64, U, cat, wrap, unwrap, chunkNonce, chunkAad, aesKey, gcmEnc, gcmDec, ITER },
  };
})();
if (typeof globalThis !== 'undefined') globalThis.E2E = E2E;
