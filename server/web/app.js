'use strict';
/* Freedom — веб-клиент со сквозным шифрованием. Криптография: e2ee.js (см. E2EE.md).
   Сервер видит только шифртекст; расшифровка и подписи проверяются здесь, в браузере. */

const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

const PAGE = 50;
const MAX_FILE_BYTES = 32 * 1048576;   // как на сервере (MAX_ENC_FILE минус запас на теги шифрования)
const S = {
  token: localStorage.getItem('token'), me: localStorage.getItem('me'),
  rooms: [], cur: null, msgs: {}, typing: {}, loaded: new Set(), done: new Set(),
  ws: null, online: false, everOnline: false, attempt: 0, lastTyping: 0, call: null, olderBusy: false,
  q: Promise.resolve(),                         // очередь событий: сообщения обрабатываются строго по порядку
  replyTo: null, editing: null,                 // активная «отвечаю на» / «редактирую» в открытой комнате
  readState: {},                                // roomId -> {username: last_read_id} — для галочек прочтения
  roomMembers: {},                              // roomId -> [username, …] — кэш для автодополнения @упоминаний
  showArchived: false,                          // развёрнут ли раздел «Архив» в списке чатов
};
const room = (id) => S.rooms.find((r) => r.id === id);

/* ---------- утилиты ---------- */

function fmtTime(ts) {
  const d = new Date(ts * 1000), now = new Date();
  return d.toDateString() === now.toDateString()
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString([], { day: '2-digit', month: '2-digit' });
}
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}

/* ---------- аватары ---------- */
// Тот же набор пар, что использует Telegram: цвет аватара зависит от имени, а не от того, кто на него смотрит.
const AVATAR_GRADIENTS = [
  ['#ff885e', '#ff516a'], ['#ffcd6a', '#ffa85c'], ['#82b1ff', '#665fff'],
  ['#a0de7e', '#54cb68'], ['#53edd6', '#28c9b7'], ['#72d5fd', '#2a9ef1'], ['#e0a2f3', '#d669ed'],
];
function hashCode(s) { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return Math.abs(h); }
function avatarGradient(name) { return AVATAR_GRADIENTS[hashCode(name || '?') % AVATAR_GRADIENTS.length]; }
function initials(name) {
  const parts = (name || '?').trim().split(/\s+/);
  return ((parts[0]?.[0] || '') + (parts.length > 1 ? parts[1][0] : '')).toUpperCase() || '?';
}
/** cls — например 'avatar sm'; hashName — по какому имени выбирать цвет (для ЛС — собеседник, для комнаты — сама комната). */
function avatarEl(cls, label, hashName) {
  const a = el('div', cls, label ? initials(label) : '#');
  const [c1, c2] = avatarGradient(hashName || label || '?');
  a.style.background = `linear-gradient(135deg, ${c1}, ${c2})`;
  return a;
}

/* ---------- псевдонимы (только на этом устройстве, не синхронизируются) ---------- */
const aliasKey = (u) => `frdm_alias_${u}`;
function getAlias(u) { return localStorage.getItem(aliasKey(u)) || ''; }
function setAlias(u, alias) {
  const v = (alias || '').trim();
  if (v) localStorage.setItem(aliasKey(u), v); else localStorage.removeItem(aliasKey(u));
}
/** Как показать имя пользователя: псевдоним, если задан, иначе — как есть. */
function displayName(u) { return getAlias(u) || u; }

/* ---------- заглушить / архивировать чат (только на этом устройстве, per-room, как псевдонимы) ---------- */
const isMuted = (roomId) => localStorage.getItem(`frdm_mute_${roomId}`) === '1';
function setMuted(roomId, muted) {
  if (muted) localStorage.setItem(`frdm_mute_${roomId}`, '1'); else localStorage.removeItem(`frdm_mute_${roomId}`);
}
const isArchived = (roomId) => localStorage.getItem(`frdm_archive_${roomId}`) === '1';
function setArchived(roomId, archived) {
  if (archived) localStorage.setItem(`frdm_archive_${roomId}`, '1'); else localStorage.removeItem(`frdm_archive_${roomId}`);
}

/* ---------- даты ---------- */
function dayKey(ts) { return new Date(ts * 1000).toDateString(); }
function fmtDay(ts) {
  const d = new Date(ts * 1000), now = new Date();
  if (d.toDateString() === now.toDateString()) return 'Сегодня';
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return 'Вчера';
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString([], sameYear ? { day: 'numeric', month: 'long' } : { day: 'numeric', month: 'long', year: 'numeric' });
}
/** «Продолжение той же реплики» — Telegram группирует так подряд идущие сообщения одного автора. */
function sameRun(a, b) {
  if (!a || !b || a.kind === 'call' || b.kind === 'call') return false;
  if (a.username !== b.username) return false;
  if (Math.abs(b.ts - a.ts) > 300) return false;
  return dayKey(a.ts) === dayKey(b.ts);
}
let toastTimer;
function toast(msg) {
  if (!msg) return;
  const t = $('#toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3500);
}
const KIND = { image: '📷 Фото', audio: '🎤 Голосовое сообщение', video: '🎥 Видеосообщение', file: '📎 Файл' };
const ERR_TEXT = {
  no_key: '🔒 Сообщение недоступно: отправлено до вашего вступления в комнату',
  bad_sig: '⚠️ Подпись сообщения не прошла проверку',
  key_changed: '⚠️ Ключ безопасности отправителя изменился',
  bad_data: '⚠️ Не удалось расшифровать сообщение',
};
function describe(m) {
  if (m.kind === 'call') return m.text;
  if (m.err) return '🔒 Сообщение';
  if (m.t === 'file') return `📎 ${m.name || 'Файл'}`;
  return KIND[m.t] || m.text;
}
/** Короткая подпись для цитаты «в ответ на…» — без эмодзи-префикса, тот идёт отдельной иконкой. */
function replyPreviewText(m) {
  if (m.t === 'file') return m.name || 'Файл';
  if (m.t === 'text') return m.text;
  return { image: 'Фото', audio: 'Голосовое сообщение', video: 'Видеосообщение' }[m.t] || '';
}
/** Лёгкая разметка в духе Telegram: **жирный**, __жирный__, *курсив*, _курсив_, `код`, ~~зачёркнутый~~.
    Без вложенности — этого достаточно для сообщений; строит текстовые узлы напрямую, инъекций не боится. */
const FORMAT_RE = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|__([^_\n]+)__|~~([^~\n]+)~~|\*([^*\n]+)\*|_([^_\n]+)_|(?<![\w@])@(\w{3,32})(?!\w)|\|\|([^|\n]+)\|\|/g;
function renderFormattedText(text) {
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const m of text.matchAll(FORMAT_RE)) {
    if (m.index > last) frag.append(document.createTextNode(text.slice(last, m.index)));
    if (m[1] !== undefined) frag.append(el('code', 'fmtCode', m[1]));
    else if (m[2] !== undefined || m[3] !== undefined) frag.append(el('b', '', m[2] ?? m[3]));
    else if (m[4] !== undefined) frag.append(el('s', '', m[4]));
    else if (m[5] !== undefined || m[6] !== undefined) frag.append(el('i', '', m[5] ?? m[6]));
    else if (m[7] !== undefined) frag.append(mentionEl(m[7]));
    else frag.append(spoilerEl(m[8]));
    last = m.index + m[0].length;
  }
  if (last < text.length) frag.append(document.createTextNode(text.slice(last)));
  return frag;
}
/** ||текст|| — скрыт до тапа, как в Telegram. Раскрытие — только визуальное состояние этого показа,
    нигде не сохраняется: свернётся заново при следующей отрисовке (смена чата, новое сообщение рядом и т.п.). */
function spoilerEl(text) {
  const s = el('span', 'spoiler', text);
  s.onclick = (e) => { e.stopPropagation(); s.classList.toggle('revealed'); };
  return s;
}
/** @username кликабелен — открывает профиль/псевдоним; существует ли такой пользователь, не проверяем
    (сервер и так вернёт понятную ошибку в диалоге профиля, если имя выдумано). */
function mentionEl(username) {
  const s = el('span', 'mention', '@' + username);
  s.onclick = (e) => { e.stopPropagation(); openContactProfile(username); };
  return s;
}
function formatBytes(n) {
  if (!n) return '';
  if (n < 1024) return n + ' Б';
  if (n < 1048576) return Math.round(n / 1024) + ' КБ';
  return (n / 1048576).toFixed(1) + ' МБ';
}
function preview(r) {
  if (!r.last) return 'Нет сообщений';
  return r.is_direct || r.last.kind === 'call' ? r.prev : `${r.last.username}: ${r.prev}`;
}
function pickMime(list) {
  return list.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || '';
}

/* ---------- API ---------- */

async function api(path, opts = {}) {
  const headers = {};
  if (S.token) headers.Authorization = 'Bearer ' + S.token;
  let body = opts.body;
  if (body && !(body instanceof FormData)) {
    body = JSON.stringify(body);
    headers['Content-Type'] = 'application/json';
  }
  let res;
  try {
    res = await fetch(path, { method: opts.method || (body ? 'POST' : 'GET'), headers, body });
  } catch { throw new Error('Нет связи с сервером'); }
  if (!res.ok) {
    let msg = 'Ошибка ' + res.status;
    try {
      const j = await res.json();
      msg = typeof j.detail === 'string' ? j.detail : 'Проверьте введённые данные';
    } catch { /* пустое тело */ }
    if (res.status === 401 && S.token) logout('Сессия истекла, войдите заново');
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

/* Вложения: сервер хранит только шифртекст. Скачиваем, расшифровываем ключом из сообщения
   и показываем как blob. Расшифрованное лежит только в памяти вкладки. */
const plainCache = new Map();
function mediaPlain(m) {
  if (!m.file) return Promise.reject(new Error('нет ключа файла'));
  if (!plainCache.has(m.media)) {
    plainCache.set(m.media, (async () => {
      const r = await fetch(m.media, { headers: { Authorization: 'Bearer ' + S.token } });
      if (!r.ok) throw new Error('download');
      const blob = await E2E.decryptFile(await r.blob(), m.file.k, m.file.n, m.file.mime);
      return URL.createObjectURL(blob);
    })().catch((e) => { plainCache.delete(m.media); throw e; }));
  }
  return plainCache.get(m.media);
}

/* ---------- вход / выход ---------- */

function authBusy(on, text) {
  for (const id of ['#btnLogin', '#btnRegister']) $(id).disabled = on;
  if (on) $('#authErr').textContent = text || '';
}

async function authenticate(register) {
  const username = $('#aUser').value.trim(), password = $('#aPass').value;
  $('#authErr').textContent = '';
  if (!E2E.supported) { $('#authErr').textContent = 'Для шифрования нужен HTTPS (или localhost)'; return; }
  authBusy(true, 'Генерация ключей шифрования…');
  try {
    // из пароля выводятся два независимых секрета: auth (уходит на сервер) и kek (остаётся здесь)
    const { auth, kek } = await E2E.deriveSecrets(username, password);
    let r, ident;
    if (register) {
      const fresh = await E2E.createIdentity();
      const blob = await E2E.sealBlob(fresh, kek, username);   // приватные ключи, зашифрованные kek
      r = await api('/register', { body: {
        username, password: auth, dh_pub: E2E.b64(fresh.dh.spki), sig_pub: E2E.b64(fresh.sig.spki), key_blob: blob } });
      ident = await E2E.openBlob(blob, kek, username);          // рабочая копия: ключи неизвлекаемые
    } else {
      r = await api('/login', { body: { username, password: auth } });
      ident = await E2E.openBlob(r.key_blob, kek, username);
    }
    S.token = r.token; S.me = r.username;
    localStorage.setItem('token', S.token); localStorage.setItem('me', S.me);
    E2E.setIdentity(ident, S.me);
    await E2E.saveIdentity(S.me, ident);
    $('#aPass').value = '';
    await enter();
  } catch (e) { $('#authErr').textContent = e.message; }
  finally { authBusy(false); }
}

function showAuth(msg) {
  $('#app').classList.add('hidden'); $('#auth').classList.remove('hidden');
  $('#authErr').textContent = msg || '';
}

async function enter() {
  $('#auth').classList.add('hidden'); $('#app').classList.remove('hidden');
  $('#me').textContent = S.me;
  if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();
  try { await loadRooms(); } catch (e) { toast(e.message); }
  connectWs();
}

function logout(msg) {
  if (S.call) endLocal();
  S.token = null; S.me = null;
  localStorage.removeItem('token'); localStorage.removeItem('me');
  E2E.reset(); E2E.clearLocal();
  if (S.ws) { S.ws.onclose = null; S.ws.close(); S.ws = null; }
  Object.assign(S, { rooms: [], cur: null, msgs: {}, typing: {}, online: false, everOnline: false, q: Promise.resolve() });
  S.loaded.clear(); S.done.clear();
  plainCache.forEach((p) => p.then(URL.revokeObjectURL).catch(() => {})); plainCache.clear();
  document.body.classList.remove('in-chat');
  $('#chatView').classList.add('hidden'); $('#empty').classList.remove('hidden');
  showAuth(msg);
}

/* ---------- WebSocket ---------- */

function connectWs() {
  if (!S.token) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = S.ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(S.token)}`);
  ws.onopen = async () => {
    const reconnected = S.everOnline;
    S.everOnline = true; S.online = true; S.attempt = 0; updateSubtitle();
    if (reconnected) {                                 // после обрыва подтягиваем пропущенное
      try { await loadRooms(); if (S.cur) await fetchLatest(S.cur); } catch { /* ignore */ }
    }
  };
  ws.onmessage = (e) => { try { onEvent(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  ws.onclose = () => {
    if (S.ws !== ws) return;
    S.online = false; updateSubtitle();
    if (!S.token) return;
    setTimeout(async () => {
      try { await api('/rooms'); } catch { /* 401 обработает api() */ }   // заодно проверяет токен
      connectWs();
    }, Math.min(30000, 1000 * 2 ** Math.min(S.attempt++, 5)));
  };
}
function wsSend(obj) {
  if (S.ws && S.ws.readyState === WebSocket.OPEN) { S.ws.send(JSON.stringify(obj)); return true; }
  return false;
}

function onEvent(m) {
  switch (m.type) {
    case 'message': case 'keys_updated': case 'rooms_changed':
    case 'message_edited': case 'message_deleted':
    case 'reaction_set': case 'reaction_removed':
    case 'message_pinned': case 'message_unpinned': case 'autodelete_changed':
    case 'read_state':
    case 'vote_set': case 'vote_removed':
      S.q = S.q.then(() => handleQueued(m)).catch((e) => console.error(e)); break;
    case 'typing': onTyping(m); break;
    case 'error':
      if (m.code === 'stale_epoch') E2E.fetchKeys(m.room_id).catch(() => {});
      toast(m.message); break;
    case 'call_invite': onInvite(m); break;
    case 'call_joined': onJoined(m); break;
    case 'call_peer_joined': onPeerJoined(m); break;
    case 'call_peer_left': onPeerLeft(m); break;
    case 'call_signal': onSignal(m); break;
    case 'call_end': onCallEnd(m); break;
  }
}
async function handleQueued(m) {
  if (m.type === 'message') return onMessage(m);
  if (m.type === 'message_edited') return applyEdit(m);
  if (m.type === 'message_deleted') return applyDelete(m.room_id, m.id);
  if (m.type === 'reaction_set') return applyReaction(m);
  if (m.type === 'reaction_removed') return applyReactionRemoved(m);
  if (m.type === 'message_pinned') return applyPinned(m.room_id, m.message_id, true);
  if (m.type === 'message_unpinned') return applyPinned(m.room_id, m.message_id, false);
  if (m.type === 'autodelete_changed') { const r = room(m.room_id); if (r) r.autodelete_seconds = m.seconds; return; }
  if (m.type === 'read_state') return applyReadState(m);
  if (m.type === 'vote_set') return applyVote(m);
  if (m.type === 'vote_removed') return applyVoteRemoved(m);
  if (m.type === 'rooms_changed') return loadRooms();
  if (m.type === 'keys_updated') {                     // новая эпоха ключа: забираем её до следующих сообщений
    E2E.noteEpoch(m.room_id, m.epoch);
    const r = room(m.room_id); if (r) r.epoch = m.epoch;
    await E2E.fetchKeys(m.room_id).catch((e) => console.warn(e));
  }
}

/* ---------- расшифровка ---------- */

/** Превращает серверную запись в готовое к показу сообщение (m.t, m.text, m.dur, m.file, m.name, m.replyTo или m.err). */
async function open(m) {
  if (m.kind === 'call') return m;
  try {
    const p = await E2E.decryptMessage(m);
    m.t = p.t; m.text = p.text || ''; m.dur = p.dur; m.file = p.file; m.name = p.name || null;
    m.replyTo = p.reply_to || null; m.forwardedFrom = p.forwarded_from || null; m.err = null;
    m.question = p.question || null; m.options = p.options || null;
  } catch (e) {
    m.t = 'text'; m.text = ''; m.err = e.code || 'bad_data';
  }
  m.reactionsDecoded = await decryptReactions(m.room_id, m.reactions);
  m.votesDecoded = await decryptVotes(m.room_id, m.votes);
  return m;
}

/** Каждая реакция — отдельное маленькое зашифрованное сообщение (тот же формат, что и обычные). */
async function decryptReactions(roomId, reactions) {
  if (!reactions || !reactions.length) return [];
  const out = [];
  for (const r of reactions) {
    try {
      const p = await E2E.decryptMessage({ room_id: roomId, epoch: r.epoch, username: r.username, ct: r.ct, sig: r.sig });
      out.push({ username: r.username, emoji: p.emoji });
    } catch { /* не смогли — просто не покажем эту реакцию */ }
  }
  return out;
}

/** Голоса опроса — той же формы, что реакции: каждый отдельно зашифрован ключом комнаты. */
async function decryptVotes(roomId, votes) {
  if (!votes || !votes.length) return [];
  const out = [];
  for (const v of votes) {
    try {
      const p = await E2E.decryptMessage({ room_id: roomId, epoch: v.epoch, username: v.username, ct: v.ct, sig: v.sig });
      out.push({ username: v.username, option: p.option });
    } catch { /* не смогли — просто не покажем этот голос */ }
  }
  return out;
}

/* ---------- комнаты ---------- */

async function loadRooms() {
  const rooms = await api('/rooms');
  await E2E.fetchKeys().catch((e) => console.warn(e));         // все конверты с ключами одним запросом
  await Promise.all(rooms.map(async (r) => {
    E2E.noteEpoch(r.id, r.epoch);
    r.prev = r.last ? describe(await open(r.last)) : '';
  }));
  S.rooms = rooms;
  if (S.cur) { const r = room(S.cur); if (r) r.unread = 0; }
  renderRooms();
}

function renderRooms() {
  const box = $('#rooms'); box.textContent = '';
  const archivedRooms = S.rooms.filter((r) => isArchived(r.id));
  const visibleRooms = S.showArchived ? S.rooms : S.rooms.filter((r) => !isArchived(r.id));
  for (const r of visibleRooms) {
    const item = el('div', 'room' + (r.id === S.cur ? ' active' : ''));
    const shown = r.is_direct ? displayName(r.name) : r.name;
    item.dataset.q = (shown + ' ' + r.name).toLowerCase();
    item.append(r.is_direct ? avatarEl('avatar', shown, r.name) : avatarEl('avatar', null, r.name));
    const meta = el('div', 'meta'), top = el('div', 'top'), bottom = el('div', 'bottom');
    const nameRow = el('div', 'name');
    nameRow.append(document.createTextNode(shown));
    if (isMuted(r.id)) nameRow.append(Icon('muted', 14));
    top.append(nameRow);
    if (r.last) top.append(el('div', 'time', fmtTime(r.sort_ts)));
    const draft = r.id !== S.cur ? loadDraft(r.id) : '';   // черновик открытой комнаты и так виден в поле ввода
    if (draft) {
      const p = el('div', 'prev');
      p.append(el('span', 'draftLabel', 'Черновик: '), document.createTextNode(draft));
      bottom.append(p);
    } else {
      bottom.append(el('div', 'prev', preview(r)));
    }
    if (r.unread > 0) bottom.append(el('div', 'badge', r.unread > 99 ? '99+' : String(r.unread)));
    meta.append(top, bottom); item.append(meta);
    const more = el('button', 'roomMore'); more.append(Icon('more', 14));
    more.onclick = (e) => { e.stopPropagation(); openRoomMenu(r, more); };
    item.append(more);
    item.onclick = () => openRoom(r.id);
    box.append(item);
  }
  if (!S.showArchived && archivedRooms.length) {
    const bar = el('div', 'archiveBar');
    bar.append(Icon('archive', 16), el('span', '', `Архив (${archivedRooms.length})`));
    bar.onclick = () => { S.showArchived = true; renderRooms(); };
    box.append(bar);
  } else if (S.showArchived) {
    const bar = el('div', 'archiveBar');
    bar.append(Icon('archive', 16), el('span', '', 'Скрыть архив'));
    bar.onclick = () => { S.showArchived = false; renderRooms(); };
    box.append(bar);
  }
  filterRooms();
  const total = S.rooms.reduce((n, r) => n + r.unread, 0);
  document.title = total ? `(${total}) Freedom` : 'Freedom';
}

function openRoomMenu(r, anchorEl) {
  closeMsgMenu();
  const menu = el('div', 'msgMenu'); menu.id = 'msgMenu';
  const item = (icon, label, fn) => {
    const b = el('button', '');
    b.append(Icon(icon, 16), el('span', '', label));
    b.onclick = (e) => { e.stopPropagation(); closeMsgMenu(); fn(); };
    menu.append(b);
  };
  item(isMuted(r.id) ? 'unmuted' : 'muted', isMuted(r.id) ? 'Включить уведомления' : 'Заглушить', () => { setMuted(r.id, !isMuted(r.id)); renderRooms(); });
  item('archive', isArchived(r.id) ? 'Вернуть из архива' : 'Архивировать', () => { setArchived(r.id, !isArchived(r.id)); renderRooms(); });
  document.body.append(menu);
  const rect = anchorEl.getBoundingClientRect();
  const top = Math.min(rect.bottom + 4, window.innerHeight - menu.offsetHeight - 8);
  const left = Math.min(rect.left, window.innerWidth - menu.offsetWidth - 8);
  menu.style.top = Math.max(8, top) + 'px'; menu.style.left = Math.max(8, left) + 'px';
  setTimeout(() => document.addEventListener('click', closeMsgMenuOnClick, true), 0);
}

function filterRooms() {
  const q = ($('#roomSearch')?.value || '').trim().toLowerCase();
  for (const item of $('#rooms').children) item.classList.toggle('hide', q.length > 0 && !item.dataset.q.includes(q));
}

async function openRoom(id) {
  const r = room(id); if (!r) return;
  saveDraftNow(S.cur);
  S.cur = id; r.unread = 0; renderRooms();
  resetComposerState();
  $('#searchPanel').classList.add('hidden');
  hideMentionList();
  document.body.classList.add('in-chat');
  $('#empty').classList.add('hidden'); $('#chatView').classList.remove('hidden');
  $('#title').textContent = r.is_direct ? displayName(r.name) : r.name;
  const av = r.is_direct ? avatarEl('avatar', displayName(r.name), r.name) : avatarEl('avatar', null, r.name);
  $('#chatAvatar').replaceWith(av); av.id = 'chatAvatar'; av.style.cursor = 'pointer';
  av.onclick = openCurrentContactProfile;
  $('#callAudio').hidden = $('#callVideo').hidden = false;
  updateSubtitle();
  input().value = loadDraft(id); autosize(); toggleSend();
  S.loaded.add(id);
  renderAll();
  renderPinnedBar();
  refreshScheduledBar(id);
  try { await E2E.ensureKey(id); } catch (e) { toast(e.message); }   // при необходимости создаёт ключ комнаты
  try {
    const h = await api(`/rooms/${id}/messages?limit=${PAGE}`);
    await Promise.all(h.map(open));
    if (h.length < PAGE) S.done.add(id);
    merge(id, h);
    const rs = await api(`/rooms/${id}/read_state`);
    S.readState[id] = Object.fromEntries(rs.map((x) => [x.username, x.last_read_id]));
    if (S.cur === id) { renderAll(); sendRead(id); }
  } catch (e) { toast(e.message); }
  $('#input').focus();
}

async function fetchLatest(id) {
  const h = await api(`/rooms/${id}/messages?limit=${PAGE}`);
  await Promise.all(h.map(open));
  merge(id, h);
  if (S.cur === id) { renderAll(); sendRead(id); }
}

function closeRoom() {
  saveDraftNow(S.cur);
  S.cur = null; document.body.classList.remove('in-chat');
  resetComposerState();
  $('#searchPanel').classList.add('hidden');
  hideMentionList();
  $('#chatView').classList.add('hidden'); $('#empty').classList.remove('hidden');
  renderRooms();
}

function sendRead(id) {
  const list = S.msgs[id];
  if (list && list.length) wsSend({ type: 'read', room_id: id, message_id: list[list.length - 1].id });
}

/* ---------- сообщения ---------- */

function merge(id, list) {
  const cur = S.msgs[id] || (S.msgs[id] = []);
  const seen = new Set(cur.map((m) => m.id));
  const added = list.filter((m) => !seen.has(m.id));
  if (added.length) { cur.push(...added); cur.sort((a, b) => a.id - b.id); }
  return added;
}

const box = () => $('#messages');
const nearBottom = () => box().scrollHeight - box().scrollTop - box().clientHeight < 140;
const toBottom = () => { box().scrollTop = box().scrollHeight; };

function renderAll() {
  const b = box(); b.textContent = '';
  const list = S.msgs[S.cur] || [];
  let lastDay = null;
  list.forEach((m, i) => {
    const day = dayKey(m.ts);
    if (day !== lastDay) { b.append(el('div', 'datesep', fmtDay(m.ts))); lastDay = day; }
    b.append(msgEl(m, { startsRun: !sameRun(list[i - 1], m), endsRun: !sameRun(m, list[i + 1]) }));
  });
  toBottom();
}

async function onMessage(m) {
  await open(m);
  const r = room(m.room_id);
  if (!r) { await loadRooms(); return; }
  const mine = m.username === S.me, isOpen = S.cur === m.room_id;
  r.last = { id: m.id, username: m.username, kind: m.kind, ts: m.ts };
  r.prev = describe(m); r.sort_ts = m.ts;
  if (!isOpen && !mine && m.kind !== 'call') r.unread++;
  S.rooms.sort((a, b) => b.sort_ts - a.sort_ts);
  renderRooms();

  const t = S.typing[m.room_id];
  if (t && t.has(m.username)) { clearTimeout(t.get(m.username)); t.delete(m.username); updateSubtitle(); }

  if (S.loaded.has(m.room_id)) {
    const stick = isOpen && (nearBottom() || mine);
    const prevLast = (S.msgs[m.room_id] || []).at(-1);
    if (merge(m.room_id, [m]).length && isOpen) {
      // это сообщение продолжает предыдущую реплику того же автора — у неё был «хвостик», теперь не должен быть
      const lastRow = box().lastElementChild;
      if (lastRow?.classList.contains('row') && sameRun(prevLast, m)) lastRow.classList.remove('tail');
      if (!prevLast || dayKey(prevLast.ts) !== dayKey(m.ts)) box().append(el('div', 'datesep', fmtDay(m.ts)));
      box().append(msgEl(m, { startsRun: !sameRun(prevLast, m), endsRun: true }));
      if (stick) toBottom();
    }
  }
  if (isOpen) sendRead(m.room_id);
  if (!mine && m.kind !== 'call' && document.hidden && !isMuted(m.room_id) && 'Notification' in window && Notification.permission === 'granted') {
    new Notification(r.is_direct ? m.username : r.name, { body: '🔒 Новое сообщение' });
  }
}

async function loadOlder() {
  const id = S.cur, list = S.msgs[id];
  if (!id || S.olderBusy || S.done.has(id) || !list || !list.length) return;
  S.olderBusy = true;
  try {
    const older = await api(`/rooms/${id}/messages?limit=${PAGE}&before_id=${list[0].id}`);
    await Promise.all(older.map(open));
    if (older.length < PAGE) S.done.add(id);
    const addedIds = new Set(merge(id, older).map((m) => m.id));
    if (S.cur === id && addedIds.size) {
      const full = S.msgs[id], b = box(), prev = b.scrollHeight, first = b.firstChild;
      const frag = document.createDocumentFragment();
      let lastDay = null;
      // именно эти сообщения новые (более ранние по времени, чем всё, что уже отрисовано) — они идут первыми
      // в full после сортировки по id, поэтому цикл просто идёт с начала, пока встречаются добавленные id
      for (let i = 0; i < full.length && addedIds.has(full[i].id); i++) {
        const m = full[i], day = dayKey(m.ts);
        if (day !== lastDay) { frag.append(el('div', 'datesep', fmtDay(m.ts))); lastDay = day; }
        frag.append(msgEl(m, { startsRun: !sameRun(full[i - 1], m), endsRun: !sameRun(m, full[i + 1]) }));
      }
      b.insertBefore(frag, first);
      b.scrollTop += b.scrollHeight - prev;             // не «прыгаем» при подгрузке
    }
  } catch (e) { toast(e.message); } finally { S.olderBusy = false; }
}

/** Сообщение отредактировано (свой PUT-ответ или чужой message_edited по WS) — перерасшифровываем и обновляем бы. */
async function applyEdit(ev) {
  const list = S.msgs[ev.room_id]; if (!list) return;
  const m = list.find((x) => x.id === ev.id); if (!m) return;
  Object.assign(m, { epoch: ev.epoch, ct: ev.ct, sig: ev.sig, edited_ts: ev.edited_ts });
  await open(m);
  const r = room(ev.room_id);
  if (r && r.last && r.last.id === ev.id) { r.prev = describe(m); renderRooms(); }
  if (S.cur === ev.room_id) {
    const rowEl = box().querySelector(`[data-id="${ev.id}"]`);
    if (rowEl) rowEl.replaceWith(msgEl(m, { startsRun: !rowEl.classList.contains('grouped'), endsRun: rowEl.classList.contains('tail') }));
  }
}

/** Сообщение удалено («для себя» — только локально у инициатора, «для всех» — всем по WS message_deleted). */
function applyDelete(roomId, id) {
  const list = S.msgs[roomId];
  if (list) {
    const i = list.findIndex((x) => x.id === id);
    if (i >= 0) list.splice(i, 1);
  }
  const r = room(roomId);
  if (r && r.last && r.last.id === id) {
    const rest = list || [];
    const last = rest[rest.length - 1];
    r.last = last ? { id: last.id, username: last.username, kind: last.kind, ts: last.ts } : null;
    r.prev = last ? describe(last) : '';
    renderRooms();
  }
  if (S.cur === roomId) renderAll();          // проще и надёжнее точечного патча: пересчитывает группировку/хвостики
}

/** Реакция (своя или чужая) добавлена/изменена — приходит и в ответ на свой PUT, и по WS от других. */
async function applyReaction(ev) {
  const list = S.msgs[ev.room_id]; if (!list) return;
  const m = list.find((x) => x.id === ev.message_id); if (!m) return;
  const raw = { username: ev.username, epoch: ev.epoch, ct: ev.ct, sig: ev.sig };
  m.reactions = (m.reactions || []).filter((r) => r.username !== ev.username).concat(raw);
  m.reactionsDecoded = await decryptReactions(ev.room_id, m.reactions);
  if (S.cur === ev.room_id) rerenderMessageRow(m);
}

function applyReactionRemoved(ev) {
  const list = S.msgs[ev.room_id]; if (!list) return;
  const m = list.find((x) => x.id === ev.message_id); if (!m) return;
  m.reactions = (m.reactions || []).filter((r) => r.username !== ev.username);
  m.reactionsDecoded = (m.reactionsDecoded || []).filter((r) => r.username !== ev.username);
  if (S.cur === ev.room_id) rerenderMessageRow(m);
}

/** Голос (свой или чужой) добавлен/изменён — и в ответ на свой PUT, и по WS от других. */
async function applyVote(ev) {
  const list = S.msgs[ev.room_id]; if (!list) return;
  const m = list.find((x) => x.id === ev.message_id); if (!m) return;
  const raw = { username: ev.username, epoch: ev.epoch, ct: ev.ct, sig: ev.sig };
  m.votes = (m.votes || []).filter((v) => v.username !== ev.username).concat(raw);
  m.votesDecoded = await decryptVotes(ev.room_id, m.votes);
  if (S.cur === ev.room_id) rerenderMessageRow(m);
}

function applyVoteRemoved(ev) {
  const list = S.msgs[ev.room_id]; if (!list) return;
  const m = list.find((x) => x.id === ev.message_id); if (!m) return;
  m.votes = (m.votes || []).filter((v) => v.username !== ev.username);
  m.votesDecoded = (m.votesDecoded || []).filter((v) => v.username !== ev.username);
  if (S.cur === ev.room_id) rerenderMessageRow(m);
}

function applyPinned(roomId, messageId, pinned) {
  const list = S.msgs[roomId];
  const m = list?.find((x) => x.id === messageId);
  if (m) { m.pinned = pinned; if (S.cur === roomId) rerenderMessageRow(m); }
  if (S.cur === roomId) renderPinnedBar();
}

/* ---------- галочки «доставлено/прочитано» ---------- */
// «Прочитано» = last_read_id у ВСЕХ остальных участников комнаты не меньше id сообщения (в личном чате — у
// единственного собеседника). Кто ничего не читал (не прислал ни одной read-отметки) остаётся на 0 — значит
// «прочитано всеми» для него не наступит, пока он и правда не откроет чат.

function applyReadState(ev) {
  const st = S.readState[ev.room_id] || (S.readState[ev.room_id] = {});
  st[ev.username] = Math.max(st[ev.username] || 0, ev.message_id);
  if (S.cur === ev.room_id) renderAll();
}

function tickStatus(m) {
  const st = S.readState[m.room_id];
  const others = st ? Object.keys(st) : [];
  if (!others.length) return 'sent';
  return others.every((u) => (st[u] || 0) >= m.id) ? 'read' : 'sent';
}

function tickEl(m) {
  const read = tickStatus(m) === 'read';
  const t = el('span', 'tick' + (read ? ' read' : ''));
  t.append(Icon('check', 13));
  if (read) t.append(Icon('check', 13));
  return t;
}

/** Перерисовывает одну строку сообщения на месте — используется для реакций/закрепления, где перестраивать
    весь список (как при удалении) незачем: группировка и хвостики не меняются. */
function rerenderMessageRow(m) {
  const rowEl = box().querySelector(`[data-id="${m.id}"]`);
  if (!rowEl) return;
  rowEl.replaceWith(msgEl(m, { startsRun: !rowEl.classList.contains('grouped'), endsRun: rowEl.classList.contains('tail') }));
}

async function deleteMessage(m, forEveryone) {
  try {
    await api(`/rooms/${m.room_id}/messages/${m.id}?for_everyone=${forEveryone}`, { method: 'DELETE' });
    applyDelete(m.room_id, m.id);
  } catch (e) { toast(e.message); }
}

const REACTION_EMOJI = ['👍', '❤️', '😂', '😮', '😢', '🙏'];

async function setReaction(m, emoji) {
  try {
    const epoch = await E2E.ensureKey(m.room_id);
    const enc = await E2E.encryptPayload(m.room_id, epoch, { v: 1, emoji });
    const r = await api(`/rooms/${m.room_id}/messages/${m.id}/reaction`, { method: 'PUT', body: enc });
    await applyReaction(r);
  } catch (e) { toast(e.message); }
}
async function removeReaction(m) {
  try {
    await api(`/rooms/${m.room_id}/messages/${m.id}/reaction`, { method: 'DELETE' });
    applyReactionRemoved({ room_id: m.room_id, message_id: m.id, username: S.me });
  } catch (e) { toast(e.message); }
}
/** Тап по палитре: если это уже мой эмодзи — снимаю реакцию, иначе ставлю/меняю. */
function toggleReaction(m, emoji) {
  const mine = (m.reactionsDecoded || []).find((r) => r.username === S.me);
  if (mine && mine.emoji === emoji) removeReaction(m); else setReaction(m, emoji);
}

/* ---------- опросы: голосование и создание ---------- */

async function setVote(m, option) {
  try {
    const epoch = await E2E.ensureKey(m.room_id);
    const enc = await E2E.encryptPayload(m.room_id, epoch, { v: 1, option });
    const r = await api(`/rooms/${m.room_id}/messages/${m.id}/vote`, { method: 'PUT', body: enc });
    await applyVote(r);
  } catch (e) { toast(e.message); }
}
async function removeVote(m) {
  try {
    await api(`/rooms/${m.room_id}/messages/${m.id}/vote`, { method: 'DELETE' });
    applyVoteRemoved({ room_id: m.room_id, message_id: m.id, username: S.me });
  } catch (e) { toast(e.message); }
}
/** Тап по своему же варианту снимает голос (переголосовать нельзя «никак» иначе — опрос одиночного выбора). */
function toggleVote(m, option) {
  const mine = (m.votesDecoded || []).find((v) => v.username === S.me);
  if (mine && mine.option === option) removeVote(m); else setVote(m, option);
}

function openPollDialog() {
  $('#pollQuestion').value = ''; $('#pollErr').textContent = '';
  const box_ = $('#pollOptions'); box_.textContent = '';
  for (let i = 0; i < 2; i++) box_.append(pollOptionInput());
  $('#polldlg').showModal();
}
function pollOptionInput() {
  const inp = document.createElement('input');
  inp.placeholder = 'Вариант'; inp.maxLength = 100; inp.autocomplete = 'off';
  return inp;
}
function addPollOption() {
  const box_ = $('#pollOptions');
  if (box_.children.length >= 10) return;
  box_.append(pollOptionInput());
}

async function createPoll() {
  const question = $('#pollQuestion').value.trim();
  const options = [...$('#pollOptions').children].map((i) => i.value.trim()).filter(Boolean);
  if (!question) { $('#pollErr').textContent = 'Введите вопрос'; return; }
  if (options.length < 2) { $('#pollErr').textContent = 'Нужно хотя бы два варианта'; return; }
  const id = S.cur;
  try {
    const epoch = await E2E.ensureKey(id);
    const enc = await E2E.encryptPayload(id, epoch, { v: 1, t: 'poll', question, options });
    if (!wsSend({ type: 'message', room_id: id, ...enc })) throw new Error('Нет соединения с сервером');
    $('#polldlg').close();
  } catch (e) { $('#pollErr').textContent = e.message; }
}

/* ---------- стикеры: каталог (открытый, не E2EE) и лоток установленных ---------- */
// Сам стикер отправляется в чат как обычное зашифрованное фото (sendMedia) — шифрование сообщения этот
// каталог никак не касается, он только раздаёт картинки, которые сами пользователи туда положили.

async function fetchStickerBlob(url) {
  const r = await fetch(url, { headers: { Authorization: 'Bearer ' + S.token } });
  if (!r.ok) throw new Error('Не удалось скачать стикер');
  return r.blob();
}

async function sendSticker(url) {
  try {
    const blob = await fetchStickerBlob(url);
    await sendMedia('image', blob);
    closeStickerTray();
  } catch (e) { toast(e.message); }
}

async function toggleStickerTray() {
  const tray = $('#stickerTray');
  if (!tray.classList.contains('hidden')) return closeStickerTray();
  tray.classList.remove('hidden');
  tray.textContent = 'Загрузка…';
  try {
    const packs = await api('/stickers/installed');
    tray.textContent = '';
    const top = el('div', 'stickerTrayTop');
    const catalogBtn = el('button', 'stickerTrayCatalog');
    catalogBtn.append(Icon('sticker', 15), document.createTextNode(' Каталог'));
    catalogBtn.onclick = () => { closeStickerTray(); openStickerCatalog(); };
    top.append(catalogBtn);
    tray.append(top);
    if (!packs.length) {
      tray.append(el('div', 'searchEmpty', 'Нет установленных паков — загляните в каталог'));
      return;
    }
    for (const p of packs) {
      const grid = el('div', 'stickerGrid');
      for (const s of p.stickers) grid.append(stickerThumb(s.url, () => sendSticker(s.url)));
      tray.append(grid);
    }
  } catch (e) { tray.textContent = e.message; }
}
function closeStickerTray() { $('#stickerTray').classList.add('hidden'); }

function stickerThumb(url, onClick) {
  const img = document.createElement('img');
  img.src = url; img.loading = 'lazy'; img.className = 'stickerThumb';
  img.onclick = onClick;
  return img;
}

let stickerSearchTimer = null;
function openStickerCatalog() {
  $('#stickerSearch').value = '';
  $('#stickerCatalogDlg').showModal();
  runStickerSearch('');
}

async function runStickerSearch(q) {
  const box_ = $('#stickerPackList'); box_.textContent = 'Загрузка…';
  try {
    const packs = await api(`/stickers/packs?q=${encodeURIComponent(q)}`);
    box_.textContent = '';
    if (!packs.length) { box_.append(el('div', 'searchEmpty', q ? 'Ничего не нашлось' : 'Пока нет ни одного пака — станьте первым')); return; }
    for (const p of packs) box_.append(packRow(p));
  } catch (e) { box_.textContent = e.message; }
}

function packRow(p) {
  const row = el('div', 'packRow');
  row.onclick = () => openPackDetail(p.id);
  const info = el('div', 'packRowInfo');
  info.append(el('div', 'packRowName', p.name));
  info.append(el('div', 'packRowMeta', `${displayName(p.creator_username)} · ${p.sticker_count} ${stickerWord(p.sticker_count)}`));
  row.append(info);
  return row;
}
function stickerWord(n) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return 'стикер';
  if ([2, 3, 4].includes(mod10) && ![12, 13, 14].includes(mod100)) return 'стикера';
  return 'стикеров';
}

function openNewPackDialog() {
  $('#newPackName').value = ''; $('#newPackErr').textContent = '';
  $('#stickerNewPackDlg').showModal();
}

async function createStickerPack() {
  const name = $('#newPackName').value.trim();
  if (!name) { $('#newPackErr').textContent = 'Введите название'; return; }
  try {
    const pack = await api('/stickers/packs', { method: 'POST', body: { name } });
    $('#stickerNewPackDlg').close();
    runStickerSearch($('#stickerSearch').value);
    openPackDetail(pack.id);
  } catch (e) { $('#newPackErr').textContent = e.message; }
}

async function openPackDetail(packId) {
  $('#packDlgErr').textContent = '';
  $('#packDlgName').textContent = 'Загрузка…';
  $('#packDlgStickers').textContent = '';
  $('#packDlgMeta').textContent = '';
  $('#stickerPackDlg').showModal();
  try {
    const pack = await api(`/stickers/packs/${packId}`);
    $('#packDlgName').textContent = pack.name;
    $('#packDlgMeta').textContent = `${displayName(pack.creator_username)} · ${pack.sticker_count} ${stickerWord(pack.sticker_count)}`;
    const grid = el('div', 'stickerGrid');
    const mine = pack.creator_username === S.me;
    for (const s of pack.stickers) {
      const thumb = stickerThumb(s.url, mine ? null : () => sendSticker(s.url));
      if (mine) {
        const wrap = el('div', 'stickerEditWrap');
        wrap.append(thumb);
        const del = el('button', 'stickerDelBtn', '✕');
        del.onclick = async (e) => {
          e.stopPropagation();
          try { await api(`/stickers/packs/${packId}/stickers/${s.id}`, { method: 'DELETE' }); openPackDetail(packId); }
          catch (err) { toast(err.message); }
        };
        wrap.append(del);
        grid.append(wrap);
      } else {
        grid.append(thumb);
      }
    }
    $('#packDlgStickers').append(grid);
    $('#packDlgAddLabel').classList.toggle('hidden', !mine);
    $('#packDlgDelete').classList.toggle('hidden', !mine);
    $('#packDlgDelete').onclick = async () => {
      try { await api(`/stickers/packs/${packId}`, { method: 'DELETE' }); $('#stickerPackDlg').close(); runStickerSearch($('#stickerSearch').value); }
      catch (e) { $('#packDlgErr').textContent = e.message; }
    };
    $('#packDlgInstall').textContent = pack.installed ? 'Установлено ✓' : 'Установить';
    $('#packDlgInstall').onclick = async () => {
      try {
        await api(`/stickers/packs/${packId}/install`, { method: pack.installed ? 'DELETE' : 'POST' });
        openPackDetail(packId);
      } catch (e) { $('#packDlgErr').textContent = e.message; }
    };
    $('#packDlgAddFile').onchange = async (e) => {
      const file = e.target.files[0]; e.target.value = '';
      if (!file) return;
      const fd = new FormData(); fd.append('file', file);
      try { await api(`/stickers/packs/${packId}/stickers`, { method: 'POST', body: fd }); openPackDetail(packId); }
      catch (err) { $('#packDlgErr').textContent = err.message; }
    };
  } catch (e) {
    $('#packDlgName').textContent = 'Ошибка';
    $('#packDlgErr').textContent = e.message;
  }
}

async function pinMessage(m) {
  try { await api(`/rooms/${m.room_id}/pin/${m.id}`, { method: 'POST' }); applyPinned(m.room_id, m.id, true); }
  catch (e) { toast(e.message); }
}
async function unpinMessage(m) {
  try { await api(`/rooms/${m.room_id}/pin/${m.id}`, { method: 'DELETE' }); applyPinned(m.room_id, m.id, false); }
  catch (e) { toast(e.message); }
}

/** Пересылка: для текста — новое зашифрованное сообщение в комнате-адресате; для вложений — тот же файл
    расшифровывается (уже есть в кэше показа/воспроизведения) и заново шифруется под ключ другой комнаты —
    отдельного «скопировать файл на сервере» эндпоинта нет и не нужно, шифрование всё равно на клиенте. */
async function forwardMessage(m, targetRoomId) {
  try {
    const from = { username: m.username };
    if (m.t === 'text') {
      const epoch = await E2E.ensureKey(targetRoomId);
      const payload = { v: 1, t: 'text', text: m.text, forwarded_from: from };
      const enc = await E2E.encryptPayload(targetRoomId, epoch, payload);
      if (!wsSend({ type: 'message', room_id: targetRoomId, ...enc })) throw new Error('Нет соединения с сервером');
    } else {
      const url = await mediaPlain(m);
      const blob = await (await fetch(url)).blob();
      const f = await E2E.encryptFile(blob);
      const epoch = await E2E.ensureKey(targetRoomId);
      const payload = { v: 1, t: m.t, dur: m.dur ?? null, forwarded_from: from,
                        file: { k: f.key, n: f.nonce, size: f.size, mime: blob.type || '' } };
      if (m.name) payload.name = m.name;
      const enc = await E2E.encryptPayload(targetRoomId, epoch, payload);
      const fd = new FormData();
      fd.append('epoch', enc.epoch); fd.append('ct', enc.ct); fd.append('sig', enc.sig);
      fd.append('file', f.blob, 'e.bin');
      const sent = await api(`/rooms/${targetRoomId}/media`, { method: 'POST', body: fd });
      S.q = S.q.then(() => onMessage(sent)).catch((e) => console.error(e));
    }
    toast('Переслано');
  } catch (e) { toast(e.message); }
}

/* ---------- отложенная отправка (только текст — см. README, §7) ---------- */

function openScheduleDialog() {
  if (!input().value.trim()) return;
  $('#schedErr').textContent = '';
  const d = new Date(Date.now() + 5 * 60000);   // по умолчанию — через 5 минут, удобная отправная точка
  d.setSeconds(0, 0);
  $('#schedAt').value = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  $('#scheddlg').showModal();
}

async function confirmSchedule() {
  const local = $('#schedAt').value;
  if (!local) { $('#schedErr').textContent = 'Укажите время'; return; }
  const sendAt = new Date(local).getTime() / 1000;
  if (sendAt <= Date.now() / 1000) { $('#schedErr').textContent = 'Время должно быть в будущем'; return; }
  const text = input().value.trim();
  const id = S.cur;
  try {
    const epoch = await E2E.ensureKey(id);
    const enc = await E2E.encryptPayload(id, epoch, { v: 1, t: 'text', text });
    await api(`/rooms/${id}/schedule`, { method: 'POST', body: { ...enc, send_at: sendAt } });
    $('#scheddlg').close();
    input().value = ''; autosize(); toggleSend(); saveDraft(id, '');
    toast('Запланировано');
    refreshScheduledBar(id);
  } catch (e) { $('#schedErr').textContent = e.message; }
}

async function refreshScheduledBar(roomId) {
  const bar = $('#scheduledBar');
  if (roomId !== S.cur) return;
  try {
    const list = await api(`/rooms/${roomId}/scheduled`);
    if (!list.length) { bar.classList.add('hidden'); return; }
    bar.textContent = '';
    bar.append(Icon('clock', 16), el('span', 'schedText', `Запланировано: ${list.length}`));
    bar.onclick = () => openScheduledList(roomId);
    bar.classList.remove('hidden');
  } catch { bar.classList.add('hidden'); }
}

async function openScheduledList(roomId) {
  const box_ = $('#schedList'); box_.textContent = 'Загрузка…';
  $('#schedlistdlg').showModal();
  try {
    const list = await api(`/rooms/${roomId}/scheduled`);
    box_.textContent = '';
    if (!list.length) { box_.append(el('div', 'searchEmpty', 'Нет запланированных сообщений')); return; }
    for (const s of list) {
      const row = el('div', 'schedRow');
      row.append(el('span', '', new Date(s.send_at * 1000).toLocaleString('ru-RU')));
      const cancel = el('button', '', 'Отменить');
      cancel.onclick = async () => {
        try {
          await api(`/rooms/${roomId}/scheduled/${s.id}`, { method: 'DELETE' });
          openScheduledList(roomId);
          refreshScheduledBar(roomId);
        } catch (e) { toast(e.message); }
      };
      row.append(cancel);
      box_.append(row);
    }
  } catch (e) { box_.textContent = e.message; }
}

/* ---------- ответ / редактирование ---------- */

function showComposerBar(icon, title, preview) {
  $('#cbIconWrap').textContent = ''; $('#cbIconWrap').append(Icon(icon, 16));
  $('#cbTitle').textContent = title; $('#cbPreview').textContent = preview;
  $('#composerBar').classList.remove('hidden');
}
function hideComposerBar() { $('#composerBar').classList.add('hidden'); }

/* ---------- @упоминания: автодополнение в composer'е (только в групповых чатах) ---------- */

async function getRoomMembers(roomId) {
  if (S.roomMembers[roomId]) return S.roomMembers[roomId];
  try {
    const list = (await api(`/rooms/${roomId}/members`)).map((m) => m.username).filter((u) => u !== S.me);
    S.roomMembers[roomId] = list;
    return list;
  } catch { return []; }
}

/** Смотрит на текст перед курсором: если он кончается на «@частичное_имя» — показывает подходящих участников. */
async function checkMentionTrigger() {
  const r = room(S.cur);
  if (!r || r.is_direct) return hideMentionList();
  const el_ = input();
  const before = el_.value.slice(0, el_.selectionStart);
  const m = before.match(/(?:^|\s)@(\w{0,32})$/);
  if (!m) return hideMentionList();
  const query = m[1].toLowerCase();
  const members = await getRoomMembers(S.cur);
  const matches = members.filter((u) => u.toLowerCase().startsWith(query));
  if (!matches.length) return hideMentionList();
  showMentionList(matches, before.length - m[1].length);
}

function showMentionList(matches, atPos) {
  const box_ = $('#mentionList'); box_.textContent = '';
  for (const u of matches.slice(0, 8)) {
    const b = el('button', '');
    b.append(avatarEl('avatar xs', displayName(u), u), el('span', '', displayName(u)));
    b.onclick = () => insertMention(u, atPos);
    box_.append(b);
  }
  box_.classList.remove('hidden');
}
function hideMentionList() { $('#mentionList').classList.add('hidden'); $('#mentionList').textContent = ''; }
function mentionListOpen() { return !$('#mentionList').classList.contains('hidden'); }

function insertMention(username, atPos) {
  const el_ = input();
  const end = el_.selectionStart;
  el_.value = el_.value.slice(0, atPos) + username + ' ' + el_.value.slice(end);
  const caret = atPos + username.length + 1;
  el_.setSelectionRange(caret, caret);
  hideMentionList();
  autosize(); toggleSend(); el_.focus();
}

function startReply(m) {
  S.editing = null;
  S.replyTo = { id: m.id, username: m.username, t: m.t, text: m.text, name: m.name };
  showComposerBar('reply', m.username === S.me ? 'Вы' : displayName(m.username), replyPreviewText(m));
  input().focus();
}
function cancelReply() { S.replyTo = null; if (!S.editing) hideComposerBar(); }
function resetComposerState() { S.replyTo = null; S.editing = null; hideComposerBar(); }

function startEdit(m) {
  S.replyTo = null;
  S.editing = m.id;
  showComposerBar('edit', 'Редактирование', m.text);
  input().value = m.text; autosize(); toggleSend();
  input().focus();
}
function cancelEditing() {
  S.editing = null; hideComposerBar();
  input().value = ''; autosize(); toggleSend();
}

/* ---------- черновики (только на этом устройстве, localStorage) ---------- */

const draftKey = (id) => `frdm_draft_${id}`;
const loadDraft = (id) => (id ? localStorage.getItem(draftKey(id)) || '' : '');
function saveDraftNow(id) {
  if (!id) return;
  const text = input().value;
  if (text) localStorage.setItem(draftKey(id), text); else localStorage.removeItem(draftKey(id));
}
function clearDraft(id) { localStorage.removeItem(draftKey(id)); }

/* ---------- меню сообщения (ответить / редактировать / удалить) ---------- */

function closeMsgMenu() {
  $('#msgMenu')?.remove();
  document.removeEventListener('click', closeMsgMenuOnClick, true);
}
function closeMsgMenuOnClick(e) { if (!e.target.closest('#msgMenu')) closeMsgMenu(); }

function openMsgMenu(m, anchorEl) {
  closeMsgMenu();
  const menu = el('div', 'msgMenu'); menu.id = 'msgMenu';
  const mine = m.username === S.me;
  const myEmoji = (m.reactionsDecoded || []).find((r) => r.username === S.me)?.emoji;

  const picker = el('div', 'reactionPicker');
  for (const emoji of REACTION_EMOJI) {
    const b = el('button', 'reactionPick' + (emoji === myEmoji ? ' mine' : ''), emoji);
    b.onclick = () => { closeMsgMenu(); toggleReaction(m, emoji); };
    picker.append(b);
  }
  menu.append(picker);

  const item = (icon, label, fn) => {
    const b = el('button', '');
    b.append(Icon(icon, 16), el('span', '', label));
    b.onclick = () => { closeMsgMenu(); fn(); };
    menu.append(b);
  };
  item('reply', 'Ответить', () => startReply(m));
  if (!m.err) item('forward', 'Переслать', () => openForwardDialog(m));
  if (mine && m.t === 'text' && !m.err) item('edit', 'Редактировать', () => startEdit(m));
  item('pin', m.pinned ? 'Открепить' : 'Закрепить', () => (m.pinned ? unpinMessage(m) : pinMessage(m)));
  item('trash', 'Удалить у себя', () => deleteMessage(m, false));
  if (mine) item('trash', 'Удалить у всех', () => deleteMessage(m, true));
  document.body.append(menu);
  const rect = anchorEl.getBoundingClientRect();
  const top = Math.min(rect.bottom + 4, window.innerHeight - menu.offsetHeight - 8);
  const left = Math.min(rect.left, window.innerWidth - menu.offsetWidth - 8);
  menu.style.top = Math.max(8, top) + 'px'; menu.style.left = Math.max(8, left) + 'px';
  setTimeout(() => document.addEventListener('click', closeMsgMenuOnClick, true), 0);
}

/** startsRun/endsRun — начинает ли/завершает ли сообщение подряд идущую пачку реплик одного автора
    (Telegram так группирует: аватар и имя только у первого сообщения пачки, «хвостик» — у последнего). */
function msgEl(m, { startsRun = true, endsRun = true } = {}) {
  if (m.kind === 'call') return el('div', 'sys', `${m.text} · ${fmtTime(m.ts)}`);
  if (m.err) return el('div', 'sys locked', ERR_TEXT[m.err] || ERR_TEXT.bad_data);
  const r = room(m.room_id), mine = m.username === S.me, group = !!r && !r.is_direct;
  const cls = ['row', mine ? 'mine' : 'theirs'];
  if (!startsRun) cls.push('grouped');
  if (endsRun) cls.push('tail');
  const row = el('div', cls.join(' '));
  row.dataset.id = m.id;
  const b = el('div', 'bubble');
  if (!mine && group) {
    const slot = el('div', 'avatarSlot');
    if (startsRun) slot.append(avatarEl('avatar', displayName(m.username), m.username));
    row.append(slot);
  }
  if (!mine && group && startsRun) b.append(el('div', 'name', displayName(m.username)));
  if (m.pinned) b.append(pinBadgeEl());
  if (m.forwardedFrom) b.append(forwardedLabelEl(m.forwardedFrom));
  if (m.replyTo) b.append(replyQuoteEl(m.replyTo));
  if (m.t === 'image') b.append(imageEl(m));
  else if (m.t === 'audio') b.append(audioEl(m));
  else if (m.t === 'video') b.append(videoEl(m));
  else if (m.t === 'file') b.append(fileEl(m));
  else if (m.t === 'poll') b.append(pollEl(m));
  if (m.text && m.t !== 'file') {
    const textEl = el('div', 'text');
    textEl.append(renderFormattedText(m.text));
    b.append(textEl);
  }
  if (m.reactionsDecoded && m.reactionsDecoded.length) b.append(reactionsRowEl(m));
  const meta = el('div', 'time', (m.edited_ts ? 'изм. ' : '') + fmtTime(m.ts));
  if (mine && m.kind !== 'call') meta.append(tickEl(m));
  b.append(meta);
  const more = el('button', 'msgMore'); more.append(Icon('more', 14));
  more.onclick = (e) => { e.stopPropagation(); openMsgMenu(m, more); };
  b.append(more);
  row.append(b);
  return row;
}

function pinBadgeEl() {
  const p = el('div', 'pinBadge');
  p.append(Icon('pin', 12), el('span', '', 'Закреплено'));
  return p;
}

function forwardedLabelEl(from) {
  return el('div', 'forwardedLabel', `Переслано от ${from.username === S.me ? 'вас' : displayName(from.username)}`);
}

/** Пилюли «эмодзи × количество»; своя реакция подсвечена и снимается повторным тапом. */
function reactionsRowEl(m) {
  const row = el('div', 'reactions');
  const byEmoji = new Map();
  for (const r of m.reactionsDecoded) {
    if (!byEmoji.has(r.emoji)) byEmoji.set(r.emoji, []);
    byEmoji.get(r.emoji).push(r.username);
  }
  for (const [emoji, users] of byEmoji) {
    const mine = users.includes(S.me);
    const pill = el('button', 'reactionPill' + (mine ? ' mine' : ''));
    pill.append(document.createTextNode(emoji + ' '), el('span', '', String(users.length)));
    pill.title = users.map((u) => displayName(u)).join(', ');
    pill.onclick = (e) => { e.stopPropagation(); toggleReaction(m, emoji); };
    row.append(pill);
  }
  return row;
}

/** Карточка опроса: вопрос + варианты-полоски (% и число голосов); своя отметка подсвечена, повторный
    тап по своему же варианту снимает голос. Одиночный выбор — без множественных ответов и «викторин». */
function pollEl(m) {
  const box_ = el('div', 'poll');
  box_.append(el('div', 'pollQuestion', m.question || ''));
  const votes = m.votesDecoded || [];
  const total = votes.length;
  const mine = votes.find((v) => v.username === S.me);
  (m.options || []).forEach((label, i) => {
    const count = votes.filter((v) => v.option === i).length;
    const pct = total ? Math.round((count / total) * 100) : 0;
    const row = el('button', 'pollOption' + (mine?.option === i ? ' mine' : ''));
    const bar = el('div', 'pollBar'); bar.style.width = pct + '%';
    const labelRow = el('div', 'pollOptionLabel');
    labelRow.append(el('span', '', label), el('span', 'pollPct', total ? `${pct}%` : ''));
    row.append(bar, labelRow);
    if (total) row.append(el('div', 'pollCount', `${count} ${voteWord(count)}`));
    row.onclick = (e) => { e.stopPropagation(); toggleVote(m, i); };
    box_.append(row);
  });
  box_.append(el('div', 'pollTotal', total ? `${total} ${voteWord(total)}` : 'Нет голосов'));
  return box_;
}
function voteWord(n) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return 'голос';
  if ([2, 3, 4].includes(mod10) && ![12, 13, 14].includes(mod100)) return 'голоса';
  return 'голосов';
}

function replyQuoteEl(rt) {
  const q = el('div', 'quote');
  q.append(el('div', 'quoteName', rt.username === S.me ? 'Вы' : displayName(rt.username)));
  q.append(el('div', 'quoteText', rt.text || ''));
  return q;
}

function fileEl(m) {
  const w = el('div', 'fileCard');
  w.append(Icon('file', 28));
  const info = el('div', 'fileInfo');
  info.append(el('div', 'fileName', m.name || 'Файл'), el('div', 'fileSize muted', formatBytes(m.file?.size)));
  w.append(info);
  const dl = el('button', 'ibtn'); dl.append(Icon('download', 18));
  dl.onclick = async (e) => {
    e.stopPropagation();
    try {
      const url = await mediaPlain(m);
      const a = document.createElement('a'); a.href = url; a.download = m.name || 'file'; a.click();
    } catch { toast('Не удалось скачать файл'); }
  };
  w.append(dl);
  return w;
}

function imageEl(m) {
  const img = el('img', 'pic'); img.alt = 'Фото';
  mediaPlain(m).then((u) => { img.src = u; img.onload = () => { if (nearBottom()) toBottom(); }; })
    .catch(() => { img.alt = 'Не удалось загрузить'; });
  img.onclick = () => { $('#iview').src = img.src; $('#idlg').showModal(); };
  return img;
}

let playing = null;   // единственный проигрываемый аудиофайл
function audioEl(m) {
  const w = el('div', 'audio'), btn = el('button');
  btn.append(Icon('play', 16));
  const bar = el('div', 'bar'), fill = el('div', 'fill'), dur = el('span', 'dur', fmtDur(m.dur));
  bar.append(fill); w.append(btn, bar, dur);
  const setIcon = (name) => { btn.textContent = ''; btn.append(Icon(name, 16)); };
  const a = new Audio();
  a.onplay = () => setIcon('pause');
  a.onpause = () => setIcon('play');
  a.ontimeupdate = () => {
    fill.style.width = Math.min(100, (a.currentTime / (m.dur || a.duration || 1)) * 100) + '%';
    dur.textContent = fmtDur(a.currentTime);
  };
  a.onended = () => { fill.style.width = '0'; dur.textContent = fmtDur(m.dur); };
  btn.onclick = async () => {
    try {
      if (!a.src) a.src = await mediaPlain(m);
      if (a.paused) { if (playing && playing !== a) playing.pause(); playing = a; await a.play(); } else a.pause();
    } catch { toast('Не удалось воспроизвести'); }
  };
  return w;
}

function videoEl(m) {
  const ph = el('div', 'vph'); ph.append(Icon('play', 40), el('small', '', fmtDur(m.dur)));
  ph.onclick = async () => {
    try {
      const v = el('video', 'vmsg'); v.controls = true; v.playsInline = true;
      v.src = await mediaPlain(m);
      ph.replaceWith(v); v.play().catch(() => {});
    } catch { toast('Не удалось загрузить видео'); }
  };
  return ph;
}

/* ---------- отправка ---------- */

const input = () => $('#input');
function toggleSend() {
  const has = input().value.trim().length > 0;
  $('#sendBtn').classList.toggle('hidden', !has);
  $('#scheduleBtn').classList.toggle('hidden', !has);
  $('#micBtn').classList.toggle('hidden', has);
  $('#vidBtn').classList.toggle('hidden', has);
}
function autosize() { const i = input(); i.style.height = 'auto'; i.style.height = Math.min(i.scrollHeight, 140) + 'px'; }

async function sendText() {
  const text = input().value.trim(), id = S.cur;
  if (!text || !id) return;
  hideMentionList();
  if (!S.online) return toast('Нет соединения с сервером');
  try {
    const epoch = await E2E.ensureKey(id);
    if (S.editing) {
      const msgId = S.editing;
      const enc = await E2E.encryptPayload(id, epoch, { v: 1, t: 'text', text });
      const r = await api(`/rooms/${id}/messages/${msgId}`, { method: 'PUT', body: enc });
      await applyEdit(r);
      cancelEditing();
      return;
    }
    const payload = { v: 1, t: 'text', text };
    if (S.replyTo) payload.reply_to = { id: S.replyTo.id, username: S.replyTo.username, t: S.replyTo.t, text: replyPreviewText(S.replyTo) };
    const enc = await E2E.encryptPayload(id, epoch, payload);
    if (!wsSend({ type: 'message', room_id: id, ...enc })) throw new Error('Нет соединения с сервером');
    cancelReply();
    input().value = ''; autosize(); toggleSend(); clearDraft(id);
  } catch (e) { toast(e.message); }
}

/** Шифрует файл на устройстве, ключ файла кладёт внутрь зашифрованного сообщения. name — для t:'file'. */
async function sendMedia(kind, blob, duration, name) {
  const id = S.cur; if (!id) return;
  $('#busy').classList.remove('hidden');
  try {
    const f = await E2E.encryptFile(blob);
    for (let attempt = 0; ; attempt++) {
      const epoch = await E2E.ensureKey(id);
      const payload = { v: 1, t: kind, dur: duration ?? null, file: { k: f.key, n: f.nonce, size: f.size, mime: blob.type || '' } };
      if (name) payload.name = name;
      if (S.replyTo) payload.reply_to = { id: S.replyTo.id, username: S.replyTo.username, t: S.replyTo.t, text: replyPreviewText(S.replyTo) };
      const enc = await E2E.encryptPayload(id, epoch, payload);
      const fd = new FormData();
      fd.append('epoch', enc.epoch); fd.append('ct', enc.ct); fd.append('sig', enc.sig);
      fd.append('file', f.blob, 'e.bin');
      try {
        const m = await api(`/rooms/${id}/media`, { method: 'POST', body: fd });
        S.q = S.q.then(() => onMessage(m)).catch((e) => console.error(e));
        cancelReply();
        break;
      } catch (e) {
        if (e.status === 409 && attempt < 2) { await E2E.fetchKeys(id); continue; }   // ключи комнаты обновились
        throw e;
      }
    }
  } catch (e) { toast(e.message); }
  finally { $('#busy').classList.add('hidden'); }
}

function onTyping({ room_id, username }) {
  const t = S.typing[room_id] || (S.typing[room_id] = new Map());
  clearTimeout(t.get(username));
  t.set(username, setTimeout(() => { t.delete(username); updateSubtitle(); }, 4000));
  updateSubtitle();
}
function updateSubtitle() {
  const r = room(S.cur); if (!r) return;
  const names = S.typing[S.cur] ? [...S.typing[S.cur].keys()] : [];
  $('#subtitle').textContent = '🔒 ' + (names.length
    ? (r.is_direct ? 'печатает…' : names.join(', ') + (names.length === 1 ? ' печатает…' : ' печатают…'))
    : (S.online ? 'в сети' : 'подключение…'));
}

async function sendImage(file) {
  if (!file) return;
  if (file.type === 'image/gif') return sendMedia('image', file);
  try {                                                     // уменьшаем до 1600 px и сжимаем в JPEG
    const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
    const k = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
    const c = document.createElement('canvas');
    c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
    const blob = await new Promise((res) => c.toBlob(res, 'image/jpeg', 0.85));
    await sendMedia('image', blob);
  } catch { toast('Не удалось обработать изображение'); }
}

/** Файл любого типа (документ) — шифруется и хранится так же, как фото/аудио/видео. */
async function sendFile(file) {
  if (!file) return;
  if (file.size > MAX_FILE_BYTES) return toast('Файл больше 32 МБ');
  await sendMedia('file', file, null, file.name);
}

/* ---------- голосовые сообщения ---------- */

const A = { rec: null, stream: null, chunks: [], t0: 0, timer: null };

async function startAudio() {
  if (!navigator.mediaDevices?.getUserMedia) return toast('Для записи нужен HTTPS (или localhost)');
  try { A.stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch { return toast('Нет доступа к микрофону'); }
  const mime = pickMime(['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus']);
  A.rec = new MediaRecorder(A.stream, mime ? { mimeType: mime } : undefined);
  A.chunks = [];
  A.rec.ondataavailable = (e) => e.data.size && A.chunks.push(e.data);
  A.rec.start();
  A.t0 = Date.now();
  $('#normalBar').classList.add('hidden'); $('#recBar').classList.remove('hidden');
  A.timer = setInterval(() => {
    const s = (Date.now() - A.t0) / 1000;
    $('#recTime').textContent = fmtDur(s);
    if (s >= 300) stopAudio(true);                          // не длиннее 5 минут
  }, 250);
}

function stopAudio(send) {
  if (!A.rec) return;
  clearInterval(A.timer);
  const dur = (Date.now() - A.t0) / 1000, rec = A.rec, stream = A.stream;
  A.rec = null;
  rec.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    if (!send) return;
    if (dur < 0.6) return toast('Слишком короткое сообщение');
    sendMedia('audio', new Blob(A.chunks, { type: rec.mimeType || 'audio/webm' }), dur);
  };
  rec.stop();
  $('#recBar').classList.add('hidden'); $('#normalBar').classList.remove('hidden');
}

/* ---------- видеосообщения ---------- */

const V = { stream: null, rec: null, chunks: [], blob: null, t0: 0, dur: 0, timer: null };

function vState(s) {                                        // idle | recording | review
  $('#vRec').classList.toggle('hidden', s !== 'idle');
  $('#vStop').classList.toggle('hidden', s !== 'recording');
  $('#vRetry').classList.toggle('hidden', s !== 'review');
  $('#vSend').classList.toggle('hidden', s !== 'review');
  $('#vprev').classList.toggle('review', s === 'review');
}

async function openVideoRec() {
  if (!navigator.mediaDevices?.getUserMedia) return toast('Для записи нужен HTTPS (или localhost)');
  try {
    V.stream = await navigator.mediaDevices.getUserMedia(
      { video: { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 480 } }, audio: true });
  } catch { return toast('Нет доступа к камере или микрофону'); }
  startPreview();
  $('#vdlg').showModal();
}
function startPreview() {
  const v = $('#vprev');
  v.srcObject = V.stream; v.muted = true; v.controls = false; v.play().catch(() => {});
  $('#vtime').textContent = '0:00'; V.blob = null; vState('idle');
}
function startVideoRec() {
  const mime = pickMime(['video/webm;codecs=vp8,opus', 'video/webm;codecs=vp9,opus', 'video/webm', 'video/mp4']);
  V.rec = new MediaRecorder(V.stream, mime ? { mimeType: mime, videoBitsPerSecond: 900000 } : undefined);
  V.chunks = [];
  V.rec.ondataavailable = (e) => e.data.size && V.chunks.push(e.data);
  V.rec.onstop = () => {
    V.dur = (Date.now() - V.t0) / 1000;
    V.blob = new Blob(V.chunks, { type: V.rec.mimeType || 'video/webm' });
    const v = $('#vprev');
    v.srcObject = null; v.src = URL.createObjectURL(V.blob); v.muted = false; v.controls = true;
    vState('review');
  };
  V.rec.start(); V.t0 = Date.now(); vState('recording');
  V.timer = setInterval(() => {
    const s = (Date.now() - V.t0) / 1000;
    $('#vtime').textContent = fmtDur(s);
    if (s >= 60) stopVideoRec();                            // лимит 60 секунд
  }, 250);
}
function stopVideoRec() { clearInterval(V.timer); if (V.rec && V.rec.state !== 'inactive') V.rec.stop(); }
function closeVideoRec() {
  clearInterval(V.timer);
  if (V.rec && V.rec.state !== 'inactive') { V.rec.onstop = null; V.rec.stop(); }
  V.stream?.getTracks().forEach((t) => t.stop()); V.stream = null;
  $('#vprev').removeAttribute('src'); $('#vprev').srcObject = null;
  $('#vdlg').close();
}
async function sendVideoRec() {
  const blob = V.blob, dur = V.dur;
  closeVideoRec();
  if (blob.size > 30 * 2 ** 20) return toast('Видео больше 30 МБ');
  await sendMedia('video', blob, dur);
}

/* ---------- диалоги ---------- */

function ask(title, label) {
  return new Promise((res) => {
    const d = $('#pdlg'), i = $('#pInput');
    $('#pTitle').textContent = title; i.value = ''; i.placeholder = label;
    i.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); d.close('ok'); } };
    d.onclose = () => res(d.returnValue === 'ok' && i.value.trim() ? i.value.trim() : null);
    d.returnValue = ''; d.showModal(); i.focus();
  });
}

async function afterRoomAction(promise) {
  try { const r = await promise; await loadRooms(); openRoom(r.id); } catch (e) { toast(e.message); }
}

async function discover() {
  const list = $('#dList'); list.textContent = 'Загрузка…'; $('#ddlg').showModal();
  try {
    const rooms = await api('/rooms/discover');
    list.textContent = rooms.length ? '' : 'Нет доступных комнат';
    for (const r of rooms) {
      const row = el('div'), info = el('div');
      info.append(el('div', '', r.name), el('small', 'muted', `${r.members} участн.`));
      const btn = el('button', 'primary', 'Войти');
      btn.onclick = () => { $('#ddlg').close(); afterRoomAction(api(`/rooms/${r.id}/join`, { method: 'POST' })); };
      row.append(info, btn); list.append(row);
    }
  } catch (e) { list.textContent = e.message; }
}

/** Экран безопасности: номер безопасности для личных чатов, сведения о ключе комнаты. */
async function showSecurity() {
  const r = room(S.cur); if (!r) return;
  const body = $('#sBody'); body.textContent = 'Загрузка…'; $('#sdlg').showModal();
  try {
    body.textContent = '';
    if (r.is_direct) {
      const peer = r.peer || r.name;
      body.append(el('p', '', `Сообщения, фото, голосовые, видео и звонки с ${peer} зашифрованы: их не может прочитать даже сервер.`));
      body.append(el('p', '', 'Номер безопасности этого чата:'));
      body.append(el('div', 'safety', await E2E.safetyNumber(S.me, peer)));
      body.append(el('small', 'muted', 'Сравните его с номером у собеседника лично или по другому каналу связи. Если номера совпадают, никто не подменил ключи.'));
    } else {
      body.append(el('p', '', 'Сообщения комнаты зашифрованы общим ключом, который получают только её участники.'));
      body.append(el('p', '', `Текущая эпоха ключа: ${r.epoch}. Новый участник получает новый ключ и не может читать сообщения, отправленные до его вступления.`));
      body.append(el('small', 'muted', 'Публичная комната: любой, кто в неё вступил, читает её сообщения. Для проверки конкретного человека откройте личный чат с ним.'));
    }
  } catch (e) { body.textContent = e.message; }
  renderAutodeleteOptions(r);
}

const AUTODELETE_OPTIONS = [
  { label: 'Выключено', seconds: null },
  { label: '1 час', seconds: 3600 },
  { label: '1 день', seconds: 86400 },
  { label: '1 неделя', seconds: 604800 },
  { label: '1 месяц', seconds: 2592000 },
];

function renderAutodeleteOptions(r) {
  const list = $('#adList'); list.textContent = '';
  for (const opt of AUTODELETE_OPTIONS) {
    const active = (r.autodelete_seconds || null) === opt.seconds;
    const b = el('button', active ? 'active' : '', opt.label);
    b.onclick = async () => {
      try {
        await api(`/rooms/${r.id}/autodelete`, { method: 'PUT', body: { seconds: opt.seconds } });
        r.autodelete_seconds = opt.seconds;
        renderAutodeleteOptions(r);
      } catch (e) { toast(e.message); }
    };
    list.append(b);
  }
}

/* ---------- пересылка сообщений ---------- */

function openForwardDialog(m) {
  const list = $('#fwList'); list.textContent = '';
  for (const r of S.rooms) {
    const b = el('button', '', r.is_direct ? displayName(r.name) : r.name);
    b.onclick = () => { $('#fwdlg').close(); forwardMessage(m, r.id); };
    list.append(b);
  }
  $('#fwdlg').showModal();
}

/* ---------- закреплённые сообщения: панель под шапкой ---------- */

async function renderPinnedBar() {
  const bar = $('#pinnedBar');
  const id = S.cur;
  if (!id) { bar.classList.add('hidden'); return; }
  try {
    const pins = await api(`/rooms/${id}/pins`);
    if (!pins.length) { bar.classList.add('hidden'); bar.textContent = ''; return; }
    await Promise.all(pins.map(open));
    const last = pins[pins.length - 1];
    bar.textContent = '';
    bar.append(Icon('pin', 16));
    bar.append(el('div', 'pinnedText', describe(last)));
    bar.onclick = () => scrollToMessage(last.id);
    bar.classList.remove('hidden');
  } catch { bar.classList.add('hidden'); }
}

function scrollToMessage(id) {
  const rowEl = box().querySelector(`[data-id="${id}"]`);
  if (rowEl) { rowEl.scrollIntoView({ behavior: 'smooth', block: 'center' }); rowEl.classList.add('flash'); setTimeout(() => rowEl.classList.remove('flash'), 1200); }
}

/* ---------- поиск по уже загруженной истории (сервер текст не видит — искать может только клиент) ---------- */

function toggleChatSearch() {
  const panel = $('#searchPanel');
  const opening = panel.classList.contains('hidden');
  panel.classList.toggle('hidden');
  if (opening) {
    $('#chatSearchInput').value = ''; $('#searchResults').textContent = '';
    $('#chatSearchInput').focus();
  }
}

function runChatSearch(query) {
  const results = $('#searchResults'); results.textContent = '';
  const q = query.trim().toLowerCase();
  if (!q) return;
  const list = S.msgs[S.cur] || [];
  const hits = list.filter((m) => m.t === 'text' && !m.err && m.text.toLowerCase().includes(q)).slice(-50).reverse();
  if (!hits.length) { results.append(el('div', 'searchEmpty', 'Ничего не найдено в загруженной истории')); return; }
  const r = room(S.cur);
  for (const m of hits) {
    const b = el('button', 'searchHit');
    if (r && !r.is_direct) b.append(el('div', 'shWho', displayName(m.username)));
    b.append(highlightMatch(m.text, q));
    b.onclick = () => { toggleChatSearch(); scrollToMessage(m.id); };
    results.append(b);
  }
}

function highlightMatch(text, q) {
  const div = el('div', 'shText');
  const i = text.toLowerCase().indexOf(q);
  if (i < 0) { div.textContent = text; return div; }
  div.append(document.createTextNode(text.slice(0, i)));
  div.append(el('mark', '', text.slice(i, i + q.length)));
  div.append(document.createTextNode(text.slice(i + q.length)));
  return div;
}

/* ---------- профиль: свой (био + QR) и чужой (био + псевдоним) ---------- */

/** Рисует QR-код (матрица из qr.js) как инлайн-SVG — чётко в любом масштабе, без растровых артефактов. */
function renderQrSvg(text) {
  const { matrix, size } = QR.encode(text);
  const cell = 6, margin = 4 * cell;
  const dim = size * cell + margin * 2;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${dim} ${dim}`);
  svg.setAttribute('width', '200'); svg.setAttribute('height', '200');
  svg.style.background = '#fff'; svg.style.borderRadius = 'var(--r-md)';
  const rect = (x, y, w, h, fill) => {
    const r = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    r.setAttribute('x', x); r.setAttribute('y', y); r.setAttribute('width', w); r.setAttribute('height', h);
    r.setAttribute('fill', fill);
    return r;
  };
  svg.append(rect(0, 0, dim, dim, '#fff'));
  for (let r = 0; r < size; r++) for (let c = 0; c < size; c++) {
    if (matrix[r][c]) svg.append(rect(margin + c * cell, margin + r * cell, cell, cell, '#000'));
  }
  return svg;
}

async function openMyProfile() {
  $('#pfUsername').textContent = S.me;
  const av = avatarEl('avatar big', S.me, S.me); $('#pfAvatar').replaceWith(av); av.id = 'pfAvatar';
  $('#pfBio').value = '…'; $('#pfErr').textContent = '';
  $('#pfQr').textContent = ''; $('#pfQr').append(renderQrSvg(`frdm:u:${S.me}`));
  $('#pfdlg').showModal();
  try {
    const p = await api(`/users/${encodeURIComponent(S.me)}/profile`);
    $('#pfBio').value = p.bio || '';
  } catch (e) { $('#pfBio').value = ''; toast(e.message); }
}

async function saveMyProfile() {
  try {
    await api('/profile', { method: 'PUT', body: { bio: $('#pfBio').value } });
    $('#pfdlg').close();
  } catch (e) { $('#pfErr').textContent = e.message; }
}

async function openContactProfile(username) {
  $('#cName').textContent = displayName(username);
  const av = avatarEl('avatar big', displayName(username), username); $('#cAvatar').replaceWith(av); av.id = 'cAvatar';
  $('#cBio').textContent = '…'; $('#cAlias').value = getAlias(username);
  $('#cdlg').showModal();
  try {
    const p = await api(`/users/${encodeURIComponent(username)}/profile`);
    $('#cBio').textContent = p.bio || '';
  } catch (e) { $('#cBio').textContent = ''; }
  $('#cSaveAlias').onclick = () => {
    setAlias(username, $('#cAlias').value);
    $('#cdlg').close();
    renderRooms();
    // обновляем заголовок открытого чата точечно — не через openRoom(), чтобы не дёргать сеть заново
    if (S.cur) {
      const r = room(S.cur);
      if (r && r.is_direct && r.name === username) {
        $('#title').textContent = displayName(username);
        const av = avatarEl('avatar', displayName(username), username);
        av.style.cursor = 'pointer'; av.onclick = openCurrentContactProfile;
        $('#chatAvatar').replaceWith(av); av.id = 'chatAvatar';
      }
    }
  };
}

/** Открывает профиль контакта прямо из шапки чата (для ЛС) — по аватару/имени. */
function openCurrentContactProfile() {
  const r = room(S.cur);
  if (r && r.is_direct) openContactProfile(r.name);
}

/** Разбирает фото с QR-кодом контакта (frdm:u:<имя>) через нативный BarcodeDetector браузера. */
async function scanContactQr(file) {
  if (!file) return;
  if (typeof BarcodeDetector === 'undefined') {
    return toast('Сканирование QR не поддерживается этим браузером — введите имя вручную');
  }
  try {
    const det = new BarcodeDetector({ formats: ['qr_code'] });
    const bitmap = await createImageBitmap(file);
    const codes = await det.detect(bitmap);
    const raw = codes[0]?.rawValue || '';
    const m = raw.match(/^frdm:u:(\w{3,32})$/);
    if (!m) return toast('На фото не нашёлся QR-код контакта Freedom');
    $('#pfdlg').close();
    afterRoomAction(api('/dm', { body: { username: m[1] } }));
  } catch (e) { toast('Не удалось распознать QR-код'); }
}

/* ---------- звонки (WebRTC + зашифрованный сигналинг) ---------- */

const Ring = {
  ctx: null, timer: null,
  start() {
    try {
      this.ctx = this.ctx || new AudioContext();
      const beep = () => {
        const o = this.ctx.createOscillator(), g = this.ctx.createGain();
        o.frequency.value = 440; g.gain.value = 0.07;
        o.connect(g); g.connect(this.ctx.destination);
        o.start(); o.stop(this.ctx.currentTime + 0.4);
      };
      beep(); this.timer = setInterval(beep, 1500);
    } catch { /* звук недоступен */ }
  },
  stop() { clearInterval(this.timer); this.timer = null; },
};

const CALL_END_TEXT = {
  busy: 'Абонент занят', declined: 'Звонок отклонён', no_answer: 'Нет ответа',
  connection_lost: 'Соединение потеряно',
};

function getMedia(video) {
  if (!navigator.mediaDevices?.getUserMedia) throw new Error('Для звонков нужен HTTPS (или localhost)');
  return navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true },
    video: video ? { width: 640, height: 480, facingMode: 'user' } : false,
  });
}

/** Готовит (если ещё не готово) соединение с одним конкретным участником звонка. Идемпотентно —
    можно звать и когда просто узнали об участнике, и когда от него уже пришёл сигнал. */
async function ensurePeer(c, peerId, username) {
  const existing = c.peers[peerId];
  if (existing?.pc) return existing;
  const { iceServers } = await api('/webrtc/config');
  if (S.call !== c) throw new Error('call ended');
  const p = c.peers[peerId] = existing || { username, pending: [], remoteSet: false, chain: Promise.resolve(), stream: null, pc: null };
  p.username = username || p.username;
  const pc = p.pc = new RTCPeerConnection({ iceServers });
  c.stream.getTracks().forEach((t) => pc.addTrack(t, c.stream));
  pc.onicecandidate = (e) => { if (e.candidate) signal(c, peerId, { candidate: e.candidate.toJSON() }); };
  pc.ontrack = (e) => { p.stream = e.streams[0]; callUi(); };
  pc.onconnectionstatechange = () => {
    if (S.call !== c) return;
    if (pc.connectionState === 'connected') { c.phase = 'active'; c.t0 = c.t0 || Date.now(); callUi(); }
    else if (pc.connectionState === 'failed') removePeer(c, peerId);
  };
  return p;
}

function removePeer(c, peerId) {
  if (!c.peers[peerId]) return;
  try { c.peers[peerId].pc?.close(); } catch { /* уже закрыт */ }
  delete c.peers[peerId];
  if (S.call === c) callUi();
}

/** SDP (в нём отпечаток DTLS-сертификата) и ICE-кандидаты шифруются ключом конкретного участника:
    сервер не может ни прочитать их, ни подменить, а значит и вклиниться в звонок. */
function signal(c, peerId, data) {
  const username = c.peers[peerId]?.username;
  if (!username) return;
  E2E.sealSignal(username, c.id, data)
    .then((sealed) => wsSend({ type: 'call_signal', call_id: c.id, to: peerId, data: sealed }))
    .catch(() => { if (S.call === c) hangup('Ошибка шифрования звонка'); });
}

async function startCall(video) {
  const r = room(S.cur);
  if (S.call || !r) return;
  if (!S.online) return toast('Нет соединения с сервером');
  let stream;
  try { stream = await getMedia(video); }
  catch (e) { return toast(e.message.includes('HTTPS') ? e.message : 'Нет доступа к микрофону или камере'); }
  const c = S.call = {
    id: crypto.randomUUID(), roomId: r.id, roomName: r.is_direct ? (r.peer || r.name) : r.name,
    video, outgoing: true, group: !r.is_direct, phase: 'ringing', stream, peers: {}, t0: null,
  };
  wsSend({ type: 'call_invite', room_id: r.id, call_id: c.id, video });
  callUi();
}

function onInvite(m) {
  if (S.call) return;
  S.call = {
    id: m.call_id, roomId: m.room_id, roomName: m.from, video: m.video, outgoing: false,
    group: !!m.group, phase: 'incoming', stream: null, peers: {}, t0: null,
  };
  Ring.start(); callUi();
  if (document.hidden && 'Notification' in window && Notification.permission === 'granted') {
    new Notification(m.from, { body: m.video ? 'Входящий видеозвонок' : 'Входящий звонок' });
  }
}

async function acceptCall() {
  const c = S.call;
  if (!c || c.phase !== 'incoming') return;
  Ring.stop(); c.phase = 'connecting'; callUi();
  try { c.stream = await getMedia(c.video); }
  catch (e) { hangup(e.message.includes('HTTPS') ? e.message : 'Нет доступа к микрофону или камере'); return; }
  if (S.call !== c) return;
  wsSend({ type: 'call_accept', call_id: c.id });
}

/** Мне подтвердили участие — соединяюсь с каждым, кто уже в звонке. Новичок (то есть я) всегда сам
    предлагает оффер уже подключённым — так у обеих сторон однозначно, кому чья очередь звонить. */
async function onJoined(m) {
  const c = S.call;
  if (!c || c.id !== m.call_id) return;
  c.phase = 'connecting'; callUi();
  for (const p of m.participants) {
    let peer;
    try { peer = await ensurePeer(c, p.id, p.username); } catch { return; }
    if (S.call !== c) return;
    const pc = peer.pc;
    await pc.setLocalDescription(await pc.createOffer());
    if (S.call !== c) return;
    signal(c, p.id, { sdp: pc.localDescription.toJSON() });
  }
}

/** Кто-то ещё присоединился, пока я уже в звонке — готовлю приёмное соединение и жду от него оффер. */
async function onPeerJoined(m) {
  const c = S.call;
  if (!c || c.id !== m.call_id) return;
  try { await ensurePeer(c, m.user_id, m.username); } catch { return; }
  if (S.call === c) callUi();
}

function onPeerLeft(m) {
  const c = S.call;
  if (!c || c.id !== m.call_id) return;
  removePeer(c, m.user_id);
}

function onSignal(m) {
  const c = S.call;
  if (!c || c.id !== m.call_id) return;
  const peerId = m.from_id;
  if (!c.peers[peerId]) c.peers[peerId] = { username: m.from, pending: [], remoteSet: false, chain: Promise.resolve(), stream: null, pc: null };
  const p = c.peers[peerId];
  p.chain = p.chain.then(async () => {                      // сигналы от одного и того же человека — строго по очереди
    await ensurePeer(c, peerId, m.from);
    if (S.call !== c) return;
    const { sdp, candidate } = await E2E.openSignal(m.from, c.id, m.data);
    if (sdp) {
      await p.pc.setRemoteDescription(sdp);
      p.remoteSet = true;
      for (const cd of p.pending) await p.pc.addIceCandidate(cd).catch(() => {});
      p.pending = [];
      if (sdp.type === 'offer') {
        await p.pc.setLocalDescription(await p.pc.createAnswer());
        if (S.call !== c) return;
        signal(c, peerId, { sdp: p.pc.localDescription.toJSON() });
      }
    } else if (candidate) {
      if (p.remoteSet) await p.pc.addIceCandidate(candidate).catch(() => {});
      else p.pending.push(candidate);
    }
  }).catch((e) => { console.error('signal', e); });
}

function onCallEnd(m) {
  if (S.call && S.call.id === m.call_id) endLocal(CALL_END_TEXT[m.reason]);
}

function hangup(msg) {
  const c = S.call; if (!c) return;
  wsSend({ type: c.phase === 'incoming' ? 'call_reject' : 'call_end', call_id: c.id });
  endLocal(msg);
}

function endLocal(msg) {
  const c = S.call; S.call = null;
  Ring.stop();
  if (c) {
    clearInterval(c.tick);
    c.stream?.getTracks().forEach((t) => t.stop());
    for (const peerId in c.peers) { try { c.peers[peerId].pc?.close(); } catch { /* уже закрыт */ } }
  }
  $('#remoteVideos').textContent = ''; $('#localVideo').srcObject = null;
  $('#cMute').classList.remove('off'); $('#cCam').classList.remove('off');
  $('#callUi').classList.add('hidden');
  toast(msg);
}

/** Одна плитка на участника: видео, если оно уже идёт, иначе — аватар с именем (аудиозвонок или ещё не
    подключились). Перерисовывается целиком — участников мало (см. MAX_CALL_PARTICIPANTS на сервере),
    и это гораздо проще, чем аккуратно патчить DOM по одному элементу. */
function renderCallTiles(c) {
  const box = $('#remoteVideos'); box.textContent = '';
  for (const peerId in c.peers) {
    const p = c.peers[peerId];
    const tile = el('div', 'callTile');
    if (p.stream && c.video) {
      const v = document.createElement('video');
      v.autoplay = true; v.playsInline = true; v.srcObject = p.stream;
      tile.append(v);
    } else {
      tile.append(avatarEl('avatar', p.username, p.username));
    }
    tile.append(el('div', 'tileLabel', displayName(p.username)));
    box.append(tile);
  }
}

function callUi() {
  const c = S.call, ui = $('#callUi');
  if (!c) return;
  ui.classList.remove('hidden');
  renderCallTiles(c);
  const anyVideo = Object.values(c.peers).some((p) => p.stream);
  const live = c.video && anyVideo && c.phase !== 'ringing' && c.phase !== 'incoming';
  ui.classList.toggle('audio-only', !c.video);
  ui.classList.toggle('no-remote', Object.keys(c.peers).length === 0);
  ui.classList.toggle('video-live', !!live);
  $('#callName').textContent = c.group ? c.roomName : displayName(c.roomName);
  if ($('#callAvatar').dataset.peer !== c.roomName) {
    const cav = avatarEl('avatar big', c.group ? c.roomName : displayName(c.roomName), c.roomName);
    cav.dataset.peer = c.roomName;
    $('#callAvatar').replaceWith(cav); cav.id = 'callAvatar';
  }
  if (c.stream && c.video) $('#localVideo').srcObject = c.stream;
  $('#cAccept').classList.toggle('hidden', c.phase !== 'incoming');
  const inCall = c.phase !== 'incoming';
  $('#cMute').classList.toggle('hidden', !inCall);
  $('#cCam').classList.toggle('hidden', !inCall || !c.video);
  const status = () => {
    $('#callStatus').textContent = { ringing: 'Вызов…', incoming: c.video ? 'Входящий видеозвонок' : 'Входящий звонок',
      connecting: 'Соединение…' }[c.phase] || fmtDur((Date.now() - c.t0) / 1000);
  };
  status();
  clearInterval(c.tick);
  if (c.phase === 'active') c.tick = setInterval(status, 1000);
}

function toggleTrack(kind, btn) {
  const c = S.call; if (!c || !c.stream) return;
  const tracks = kind === 'audio' ? c.stream.getAudioTracks() : c.stream.getVideoTracks();
  const on = !tracks.every((t) => t.enabled);
  tracks.forEach((t) => { t.enabled = on; });
  btn.classList.toggle('off', !on);
}

/* ---------- события интерфейса ---------- */

$('#authForm').onsubmit = (e) => { e.preventDefault(); authenticate(false); };
$('#btnRegister').onclick = () => { if ($('#authForm').reportValidity()) authenticate(true); };
$('#btnLogout').onclick = () => logout();
$('#btnDm').onclick = async () => {
  const name = await ask('Личное сообщение', 'Имя пользователя');
  if (name) afterRoomAction(api('/dm', { body: { username: name } }));
};
$('#btnNewRoom').onclick = async () => {
  const name = await ask('Новая комната', 'Название');
  if (name) afterRoomAction(api('/rooms', { body: { name } }));
};
$('#btnDiscover').onclick = discover;
$('#dClose').onclick = () => $('#ddlg').close();
$('#btnSec').onclick = showSecurity;
$('#sClose').onclick = () => $('#sdlg').close();
$('#brandProfile').onclick = openMyProfile;
$('#pfCancel').onclick = $('#pfClose').onclick = () => $('#pfdlg').close();
$('#pfSave').onclick = saveMyProfile;
$('#pfScan').onclick = () => $('#scanFile').click();
$('#scanFile').onchange = (e) => { scanContactQr(e.target.files[0]); e.target.value = ''; };
$('#cClose').onclick = () => $('#cdlg').close();
$('#fwCancel').onclick = () => $('#fwdlg').close();
$('#btnChatSearch').onclick = toggleChatSearch;
$('#chatSearchClose').onclick = toggleChatSearch;
$('#chatSearchInput').oninput = (e) => runChatSearch(e.target.value);
$('#scheduleBtn').onclick = openScheduleDialog;
$('#schedCancel').onclick = () => $('#scheddlg').close();
$('#schedConfirm').onclick = confirmSchedule;
$('#schedListClose').onclick = () => $('#schedlistdlg').close();
$('#pollBtn').onclick = openPollDialog;
$('#pollAddOption').onclick = addPollOption;
$('#pollCancel').onclick = () => $('#polldlg').close();
$('#pollCreate').onclick = createPoll;
$('#stickerBtn').onclick = toggleStickerTray;
$('#stickerCatalogClose').onclick = () => $('#stickerCatalogDlg').close();
$('#stickerNewPack').onclick = openNewPackDialog;
$('#newPackCancel').onclick = () => $('#stickerNewPackDlg').close();
$('#newPackCreate').onclick = createStickerPack;
$('#packDlgClose').onclick = () => $('#stickerPackDlg').close();
$('#stickerSearch').oninput = (e) => {
  clearTimeout(stickerSearchTimer);
  const q = e.target.value;
  stickerSearchTimer = setTimeout(() => runStickerSearch(q), 250);
};
$('#titleClick').onclick = openCurrentContactProfile;
$('#back').onclick = closeRoom;
$('#roomSearch').addEventListener('input', filterRooms);

input().addEventListener('input', () => {
  autosize(); toggleSend();
  checkMentionTrigger();
  const now = Date.now();
  if (S.cur && input().value && now - S.lastTyping > 2500) {
    S.lastTyping = now; wsSend({ type: 'typing', room_id: S.cur });
  }
});
input().addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && mentionListOpen()) { hideMentionList(); return; }
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendText(); }
});
$('#sendBtn').onclick = sendText;
$('#file').onchange = (e) => { sendImage(e.target.files[0]); e.target.value = ''; };
$('#fileDoc').onchange = (e) => { sendFile(e.target.files[0]); e.target.value = ''; };
$('#cbClose').onclick = () => { if (S.editing) cancelEditing(); else cancelReply(); };
$('#micBtn').onclick = startAudio;
$('#recCancel').onclick = () => stopAudio(false);
$('#recSend').onclick = () => stopAudio(true);
$('#vidBtn').onclick = openVideoRec;
$('#vRec').onclick = startVideoRec;
$('#vStop').onclick = stopVideoRec;
$('#vRetry').onclick = () => { $('#vprev').removeAttribute('src'); startPreview(); };
$('#vSend').onclick = sendVideoRec;
$('#vClose').onclick = closeVideoRec;
$('#vdlg').addEventListener('cancel', closeVideoRec);
$('#idlg').onclick = () => $('#idlg').close();

$('#callAudio').onclick = () => startCall(false);
$('#callVideo').onclick = () => startCall(true);
$('#cAccept').onclick = acceptCall;
$('#cEnd').onclick = () => hangup();
$('#cMute').onclick = (e) => toggleTrack('audio', e.currentTarget);
$('#cCam').onclick = (e) => toggleTrack('video', e.currentTarget);

box().addEventListener('scroll', () => { if (box().scrollTop < 60) loadOlder(); });
window.addEventListener('pagehide', () => {
  if (S.call) wsSend({ type: !S.call.outgoing && !S.call.accepted ? 'call_reject' : 'call_end', call_id: S.call.id });
});

/* ---------- запуск ---------- */

(async function boot() {
  E2E.init({ api, me: S.me });
  if (!S.token) return showAuth();
  try {
    const id = E2E.supported && await E2E.loadIdentity(S.me);
    if (!id) throw new Error('нет ключей');
    E2E.setIdentity(id, S.me);
  } catch { return logout('Войдите заново: на этом устройстве нет ключей шифрования'); }
  enter();
})();
