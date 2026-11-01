"""Freedom — сервер чата со сквозным шифрованием (E2EE).

Сервер хранит и пересылает только шифртекст: тексты, фото, голосовые и видео шифруются
на устройствах ключом комнаты (AES-256-GCM) и подписываются отправителем. Сервер знает
публичные ключи, зашифрованные конверты с ключами комнат и метаданные (кто, кому, когда),
но не может прочитать содержимое. Протокол: E2EE.md.

Запуск (только HTTPS, см. serve.py и certs/README.txt):  python serve.py
Для разработки без TLS:  uvicorn main:app --port 8000
"""
import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException,
                     Query, Request, UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import push
from db import db

log = logging.getLogger("freedom")

TOKEN_TTL = 30 * 24 * 3600      # 30 дней
MAX_TEXT = 2000
RING_TIMEOUT = 45               # секунд звонка без ответа
RECONNECT_GRACE = 15            # сколько ждём вернувшегося абонента в звонке
MAX_ENC_FILE = 33 * 2**20       # зашифрованный файл: до 32 МБ данных + теги
MAX_CT = 24_000                 # base64-длина шифртекста сообщения
MAX_BIO = 200                    # длина открытого «о себе» в профиле
MAX_SIGNAL = 32_768             # размер зашифрованного сигнала звонка
MAX_STICKER_BYTES = 2 * 2**20   # один стикер: до 2 МБ (не зашифрован, хранится как есть — незачем больше)
MAX_AVATAR_BYTES = 2 * 2**20    # аватар — тот же порядок, что и стикер: открытая картинка, не зашифрована
MAX_TRANSCRIBE_BYTES = 25 * 2**20  # расшифрованное аудио на распознавание — с запасом под длинные голосовые
MAX_STICKERS_PER_PACK = 120     # столько же, сколько исторически ограничивает Telegram
MAX_PACKS_PER_USER = 30         # разумный потолок против спама паками
MAX_PACK_NAME = 60
APP_VERSION = "4.1"
# Минимальная версия каждого клиента, которую сервер ещё считает совместимой (по платформам:
# windows/deb-, rpm-установщики Linux/desktop, android). Не задано (пусто) — минимум не проверяется,
# клиентам лишь советуют поставить более новую версию, если она есть в downloads/. Поднимайте при
# выпуске несовместимых изменений протокола или клиента.
MIN_CLIENT_VERSION = {
    "windows": os.getenv("MIN_CLIENT_VERSION_WINDOWS") or None,
    "linux": os.getenv("MIN_CLIENT_VERSION_LINUX") or None,
    "android": os.getenv("MIN_CLIENT_VERSION_ANDROID") or None,
}
UPLOAD_DIR = Path(os.getenv("CHAT_UPLOADS", "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
STICKER_DIR = Path(os.getenv("CHAT_STICKERS", "stickers"))
STICKER_DIR.mkdir(parents=True, exist_ok=True)
AVATAR_DIR = Path(os.getenv("CHAT_AVATARS", "avatars"))
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
# Установщики клиентов для компьютера (Freedom-*.exe | .deb | .rpm), см. desktop/README.md
DOWNLOADS_DIR = Path(os.getenv("CHAT_DOWNLOADS", str(Path(__file__).parent / "downloads")))
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="Freedom")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
        "connect-src 'self' wss:; style-src 'self'; script-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    return resp


# ---------- ограничение частоты ----------

class RateLimiter:
    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


limiter = RateLimiter()


def b64len(s) -> Optional[int]:
    """Длина декодированных данных или None, если это не корректный base64."""
    if not isinstance(s, str):
        return None
    try:
        return len(base64.b64decode(s, validate=True))
    except (binascii.Error, ValueError):
        return None


# ---------- регистрация и вход ----------
# «Пароль», который приходит на сервер, — это не пароль пользователя, а auth-секрет,
# выведенный на устройстве (PBKDF2 + HKDF). Ключ шифрования приватных ключей (kek) из того же
# мастер-ключа выводится независимо и на сервер не попадает.

class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^\w+$")
    password: str = Field(min_length=6, max_length=128)


class RegisterIn(Credentials):
    dh_pub: str = Field(max_length=200)
    sig_pub: str = Field(max_length=200)
    key_blob: str = Field(min_length=20, max_length=4000)


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)


def _issue_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO tokens(token, user_id, created) VALUES(?,?,?)",
               (token, user_id, time.time()))
    db.commit()
    return token


def user_by_token(token: str) -> Optional[sqlite3.Row]:
    row = db.execute(
        "SELECT u.id, u.username, t.created FROM tokens t "
        "JOIN users u ON u.id = t.user_id WHERE t.token = ?", (token,)
    ).fetchone()
    if row is None or time.time() - row["created"] > TOKEN_TTL:
        return None
    return row


async def current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Требуется авторизация")
    user = user_by_token(authorization[7:])
    if user is None:
        raise HTTPException(401, "Сессия истекла, войдите заново")
    return user


def add_member(room_id: int, user_id: int, role: str = "member") -> None:
    """Добавляет в комнату; старая история считается прочитанной."""
    db.execute(
        "INSERT OR IGNORE INTO room_members(room_id, user_id, last_read_id, role) "
        "VALUES(?, ?, (SELECT COALESCE(MAX(id), 0) FROM messages WHERE room_id = ?), ?)",
        (room_id, user_id, room_id, role))


@app.post("/register")
async def register(c: RegisterIn):
    if b64len(c.dh_pub) != 91 or b64len(c.sig_pub) != 91:
        raise HTTPException(422, "Неверные публичные ключи")
    salt = secrets.token_bytes(16)
    pw_hash = await asyncio.to_thread(_hash, c.password, salt)
    try:
        cur = db.execute(
            "INSERT INTO users(username, salt, pw_hash, dh_pub, sig_pub, key_blob) VALUES(?,?,?,?,?,?)",
            (c.username, salt, pw_hash, c.dh_pub, c.sig_pub, c.key_blob))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Это имя уже занято")
    db.commit()
    return {"token": _issue_token(cur.lastrowid), "username": c.username,
            "dh_pub": c.dh_pub, "sig_pub": c.sig_pub, "key_blob": c.key_blob}


@app.post("/login")
async def login(c: Credentials, request: Request):
    ip = request.client.host if request.client else "?"
    if not limiter.allow(f"login:{ip}", 10, 60):
        raise HTTPException(429, "Слишком много попыток, подождите минуту")
    row = db.execute("SELECT id, salt, pw_hash, dh_pub, sig_pub, key_blob FROM users WHERE username = ?",
                     (c.username,)).fetchone()
    if row is None:
        await asyncio.to_thread(_hash, c.password, b"\0" * 16)  # выравниваем время ответа
        raise HTTPException(401, "Неверное имя или пароль")
    pw_hash = await asyncio.to_thread(_hash, c.password, row["salt"])
    if not hmac.compare_digest(pw_hash, row["pw_hash"]):
        raise HTTPException(401, "Неверное имя или пароль")
    return {"token": _issue_token(row["id"]), "username": c.username,
            "dh_pub": row["dh_pub"], "sig_pub": row["sig_pub"], "key_blob": row["key_blob"]}


# ---------- WebSocket-менеджер ----------

class ConnectionManager:
    def __init__(self):
        self.by_user: dict[int, set[WebSocket]] = {}

    async def connect(self, user_id: int, ws: WebSocket):
        await ws.accept()
        self.by_user.setdefault(user_id, set()).add(ws)

    def disconnect(self, user_id: int, ws: WebSocket):
        conns = self.by_user.get(user_id)
        if conns is not None:
            conns.discard(ws)
            if not conns:
                del self.by_user[user_id]

    def is_online(self, user_id: int) -> bool:
        return bool(self.by_user.get(user_id))

    async def send_to_users(self, user_ids, payload: dict):
        for uid in set(user_ids):
            for ws in list(self.by_user.get(uid, ())):
                try:
                    await ws.send_json(payload)
                except Exception:
                    self.disconnect(uid, ws)


manager = ConnectionManager()
_background: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


# ---------- комнаты ----------

def member_ids(room_id: int) -> list[int]:
    return [r["user_id"] for r in
            db.execute("SELECT user_id FROM room_members WHERE room_id = ?", (room_id,))]


def is_member(room_id: int, user_id: int) -> bool:
    return db.execute("SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
                      (room_id, user_id)).fetchone() is not None


def require_member(room_id: int, user_id: int) -> None:
    if not is_member(room_id, user_id):
        raise HTTPException(403, "Вы не участник этой комнаты")


def role_of(room_id: int, user_id: int) -> Optional[str]:
    row = db.execute("SELECT role FROM room_members WHERE room_id = ? AND user_id = ?",
                     (room_id, user_id)).fetchone()
    return row["role"] if row else None


def can_post(room_id: int, user_id: int) -> bool:
    """В канале писать могут только владелец и администраторы; в личных чатах и группах — все участники."""
    kind = db.execute("SELECT kind FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if kind is None or kind["kind"] != "channel":
        return True
    return role_of(room_id, user_id) in ("owner", "admin")


_ROOMS_SQL = """
SELECT r.id, r.name, r.is_direct, r.kind, r.key_epoch, r.autodelete_seconds, rm.role AS my_role,
  COALESCE(lm.ts, r.created) AS sort_ts,
  (SELECT u.username FROM room_members m2 JOIN users u ON u.id = m2.user_id
     WHERE m2.room_id = r.id AND m2.user_id != ? LIMIT 1) AS peer,
  (SELECT u.avatar FROM room_members m3 JOIN users u ON u.id = m3.user_id
     WHERE m3.room_id = r.id AND m3.user_id != ? LIMIT 1) AS peer_avatar,
  (SELECT COUNT(*) FROM messages m
     WHERE m.room_id = r.id AND m.id > rm.last_read_id AND m.user_id != ?
       AND m.kind != 'call') AS unread,
  lm.id AS last_id, lm.username AS last_user, lm.kind AS last_kind, lm.text AS last_text,
  lm.epoch AS last_epoch, lm.ct AS last_ct, lm.sig AS last_sig, lm.ts AS last_ts
FROM room_members rm
JOIN rooms r ON r.id = rm.room_id
LEFT JOIN messages lm ON lm.id = (SELECT MAX(id) FROM messages WHERE room_id = r.id)
WHERE rm.user_id = ? AND (? IS NULL OR r.id = ?)
ORDER BY sort_ts DESC
"""


def rooms_for(user_id: int, only: Optional[int] = None) -> list[dict]:
    out = []
    for r in db.execute(_ROOMS_SQL, (user_id, user_id, user_id, user_id, only, only)):
        out.append({
            "id": r["id"],
            "name": (r["peer"] or "?") if r["is_direct"] else r["name"],
            "is_direct": bool(r["is_direct"]),
            "kind": "direct" if r["is_direct"] else r["kind"],
            "role": r["my_role"],
            "peer": r["peer"] if r["is_direct"] else None,
            "avatar": _avatar_url(r["peer_avatar"]) if r["is_direct"] else None,
            "epoch": r["key_epoch"],
            "autodelete_seconds": r["autodelete_seconds"],
            "unread": r["unread"],
            "sort_ts": r["sort_ts"],
            # последнее сообщение отдаём в виде шифртекста: превью расшифровывает клиент
            "last": None if r["last_id"] is None else {
                "id": r["last_id"], "room_id": r["id"], "username": r["last_user"],
                "kind": r["last_kind"], "text": r["last_text"], "epoch": r["last_epoch"],
                "ct": r["last_ct"], "sig": r["last_sig"], "ts": r["last_ts"]},
        })
    return out


class RoomIn(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    kind: str = Field(default="group")


class DmIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)


@app.get("/rooms")
async def my_rooms(user=Depends(current_user)):
    return rooms_for(user["id"])


@app.get("/rooms/discover")
async def discover(user=Depends(current_user)):
    rows = db.execute(
        "SELECT r.id, r.name, r.kind, (SELECT COUNT(*) FROM room_members WHERE room_id = r.id) AS members "
        "FROM rooms r WHERE r.is_direct = 0 AND r.id NOT IN "
        "(SELECT room_id FROM room_members WHERE user_id = ?) "
        "ORDER BY members DESC, r.name LIMIT 50", (user["id"],)).fetchall()
    return [dict(r) for r in rows]


@app.post("/rooms")
async def create_room(body: RoomIn, user=Depends(current_user)):
    name = body.name.strip()
    if len(name) < 2:
        raise HTTPException(422, "Название слишком короткое")
    kind = body.kind if body.kind in ("group", "channel") else "group"
    try:
        cur = db.execute("INSERT INTO rooms(name, is_direct, kind, created) VALUES(?, 0, ?, ?)",
                         (name, kind, time.time()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Комната с таким названием уже есть")
    add_member(cur.lastrowid, user["id"], role="owner")
    db.commit()
    return rooms_for(user["id"], cur.lastrowid)[0]


@app.post("/rooms/{room_id}/join")
async def join_room(room_id: int, user=Depends(current_user)):
    room = db.execute("SELECT is_direct FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None or room["is_direct"]:
        raise HTTPException(404, "Комната не найдена")
    add_member(room_id, user["id"])
    db.commit()
    return rooms_for(user["id"], room_id)[0]


@app.post("/dm")
async def open_dm(body: DmIn, user=Depends(current_user)):
    peer = db.execute("SELECT id FROM users WHERE username = ?",
                      (body.username.strip(),)).fetchone()
    if peer is None:
        raise HTTPException(404, "Пользователь не найден")
    if peer["id"] == user["id"]:
        raise HTTPException(400, "Нельзя написать самому себе")
    row = db.execute(
        "SELECT r.id FROM rooms r "
        "JOIN room_members a ON a.room_id = r.id AND a.user_id = ? "
        "JOIN room_members b ON b.room_id = r.id AND b.user_id = ? "
        "WHERE r.is_direct = 1", (user["id"], peer["id"])).fetchone()
    if row:
        return rooms_for(user["id"], row["id"])[0]
    cur = db.execute("INSERT INTO rooms(name, is_direct, created) VALUES(NULL, 1, ?)",
                     (time.time(),))
    add_member(cur.lastrowid, user["id"])
    add_member(cur.lastrowid, peer["id"])
    db.commit()
    await manager.send_to_users([peer["id"]], {"type": "rooms_changed"})
    return rooms_for(user["id"], cur.lastrowid)[0]


# ---------- публичные ключи и ключи комнат (E2EE) ----------

@app.get("/users/{username}/keys")
async def user_keys(username: str, user=Depends(current_user)):
    """Публичные ключи доступны только тем, с кем есть общая комната (или себе)."""
    row = db.execute("SELECT id, username, dh_pub, sig_pub FROM users WHERE username = ?",
                     (username,)).fetchone()
    if row is None:
        raise HTTPException(404, "Пользователь не найден")
    if row["id"] != user["id"] and not db.execute(
            "SELECT 1 FROM room_members a JOIN room_members b ON a.room_id = b.room_id "
            "WHERE a.user_id = ? AND b.user_id = ? LIMIT 1", (user["id"], row["id"])).fetchone():
        raise HTTPException(404, "Пользователь не найден")
    return {"username": row["username"], "dh_pub": row["dh_pub"], "sig_pub": row["sig_pub"]}


# ---------- публичный профиль (био) ----------
# В отличие от /users/{username}/keys (ключи — только с общей комнатой), био — сознательно публичное:
# по замыслу его можно посмотреть по одному только имени пользователя, до первого сообщения — как и
# задумано для QR-кода «добавить контакт» и для discover. Само имя пользователя тоже становится узнаваемым
# по этой ручке (что оно существует) — это и есть смысл «публичного» профиля, а не утечка.

class ProfileIn(BaseModel):
    bio: str = Field(max_length=MAX_BIO, default="")


def _avatar_url(filename: Optional[str]) -> Optional[str]:
    return f"/avatars/{filename}" if filename else None


@app.get("/users/{username}/profile")
async def public_profile(username: str, user=Depends(current_user)):
    row = db.execute("SELECT username, bio, avatar FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        raise HTTPException(404, "Пользователь не найден")
    return {"username": row["username"], "bio": row["bio"], "avatar": _avatar_url(row["avatar"])}


@app.put("/profile")
async def update_profile(body: ProfileIn, user=Depends(current_user)):
    bio = body.bio.strip()
    db.execute("UPDATE users SET bio = ? WHERE id = ?", (bio, user["id"]))
    db.commit()
    avatar = db.execute("SELECT avatar FROM users WHERE id = ?", (user["id"],)).fetchone()["avatar"]
    return {"username": user["username"], "bio": bio, "avatar": _avatar_url(avatar)}


@app.post("/profile/avatar")
async def upload_avatar(file: UploadFile = File(...), user=Depends(current_user)):
    if not limiter.allow(f"avatar:{user['id']}", 10, 3600):
        raise HTTPException(429, "Слишком часто, подождите")
    data = await file.read(MAX_AVATAR_BYTES + 1)
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(413, "Аватар больше 2 МБ")
    if not _looks_like_image(data):
        raise HTTPException(422, "Похоже, это не картинка (поддерживаются JPEG, PNG, GIF, WEBP)")
    name = f"{secrets.token_urlsafe(16)}.{_image_ext(data)}"
    await asyncio.to_thread((AVATAR_DIR / name).write_bytes, data)
    old = db.execute("SELECT avatar FROM users WHERE id = ?", (user["id"],)).fetchone()["avatar"]
    db.execute("UPDATE users SET avatar = ? WHERE id = ?", (name, user["id"]))
    db.commit()
    if old:
        await asyncio.to_thread(lambda: (AVATAR_DIR / old).unlink(missing_ok=True))
    return {"username": user["username"], "avatar": _avatar_url(name)}


@app.delete("/profile/avatar")
async def delete_avatar(user=Depends(current_user)):
    old = db.execute("SELECT avatar FROM users WHERE id = ?", (user["id"],)).fetchone()["avatar"]
    db.execute("UPDATE users SET avatar = NULL WHERE id = ?", (user["id"],))
    db.commit()
    if old:
        await asyncio.to_thread(lambda: (AVATAR_DIR / old).unlink(missing_ok=True))
    return {"username": user["username"], "avatar": None}


@app.get("/avatars/{name}")
async def get_avatar_file(name: str, user=Depends(current_user)):
    if not re.fullmatch(r"[\w-]{16,}\.(jpg|png|gif|webp|bin)", name):
        raise HTTPException(404)
    path = AVATAR_DIR / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"Cache-Control": "public, max-age=86400",
                                 "X-Content-Type-Options": "nosniff"})


@app.get("/rooms/{room_id}/members")
async def room_members(room_id: int, user=Depends(current_user)):
    require_member(room_id, user["id"])
    rows = db.execute(
        "SELECT u.username, u.dh_pub, u.sig_pub, u.avatar, m.role FROM room_members m "
        "JOIN users u ON u.id = m.user_id WHERE m.room_id = ? "
        "ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, u.username",
        (room_id,)).fetchall()
    return [{**dict(r), "avatar": _avatar_url(r["avatar"])} for r in rows]


class RenameIn(BaseModel):
    name: str = Field(min_length=2, max_length=40)


@app.put("/rooms/{room_id}")
async def rename_room(room_id: int, body: RenameIn, user=Depends(current_user)):
    room = db.execute("SELECT is_direct FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        raise HTTPException(404, "Комната не найдена")
    if room["is_direct"]:
        raise HTTPException(400, "Личный чат нельзя переименовать")
    require_member(room_id, user["id"])
    if role_of(room_id, user["id"]) not in ("owner", "admin"):
        raise HTTPException(403, "Переименовать может только владелец или администратор")
    name = body.name.strip()
    if len(name) < 2:
        raise HTTPException(422, "Название слишком короткое")
    try:
        db.execute("UPDATE rooms SET name = ? WHERE id = ?", (name, room_id))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Комната с таким названием уже есть")
    db.commit()
    await manager.send_to_users(member_ids(room_id), {"type": "rooms_changed"})
    return rooms_for(user["id"], room_id)[0]


@app.post("/rooms/{room_id}/leave")
async def leave_room(room_id: int, user=Depends(current_user)):
    room = db.execute("SELECT is_direct FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        raise HTTPException(404, "Комната не найдена")
    if room["is_direct"]:
        raise HTTPException(400, "Из личного чата выйти нельзя — можно только удалить сообщения у себя")
    require_member(room_id, user["id"])
    my_role = role_of(room_id, user["id"])
    db.execute("DELETE FROM room_members WHERE room_id = ? AND user_id = ?", (room_id, user["id"]))
    if my_role == "owner":
        # передаём владение: сперва админу (кто раньше вступил), иначе — самому давнему участнику
        nxt = db.execute(
            "SELECT user_id FROM room_members WHERE room_id = ? "
            "ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, user_id LIMIT 1",
            (room_id,)).fetchone()
        if nxt:
            db.execute("UPDATE room_members SET role = 'owner' WHERE room_id = ? AND user_id = ?",
                      (room_id, nxt["user_id"]))
    db.commit()
    await manager.send_to_users(member_ids(room_id), {"type": "rooms_changed"})
    return {"ok": True}


@app.delete("/rooms/{room_id}/members/{username}")
async def kick_member(room_id: int, username: str, user=Depends(current_user)):
    require_member(room_id, user["id"])
    my_role = role_of(room_id, user["id"])
    if my_role not in ("owner", "admin"):
        raise HTTPException(403, "Удалять участников может только владелец или администратор")
    target = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if target is None:
        raise HTTPException(404, "Пользователь не найден")
    if target["id"] == user["id"]:
        raise HTTPException(400, "Чтобы выйти самому, используйте /leave")
    target_role = role_of(room_id, target["id"])
    if target_role is None:
        raise HTTPException(404, "Этот пользователь не состоит в комнате")
    if target_role == "owner" or (target_role == "admin" and my_role != "owner"):
        raise HTTPException(403, "Недостаточно прав, чтобы удалить этого участника")
    db.execute("DELETE FROM room_members WHERE room_id = ? AND user_id = ?", (room_id, target["id"]))
    db.commit()
    await manager.send_to_users([target["id"], *member_ids(room_id)], {"type": "rooms_changed"})
    return {"ok": True}


@app.post("/rooms/{room_id}/members/{username}/promote")
async def promote_member(room_id: int, username: str, user=Depends(current_user)):
    require_member(room_id, user["id"])
    if role_of(room_id, user["id"]) != "owner":
        raise HTTPException(403, "Назначать администраторов может только владелец")
    target = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if target is None or role_of(room_id, target["id"]) != "member":
        raise HTTPException(404, "Не найден рядовой участник с таким именем")
    db.execute("UPDATE room_members SET role = 'admin' WHERE room_id = ? AND user_id = ?",
              (room_id, target["id"]))
    db.commit()
    await manager.send_to_users(member_ids(room_id), {"type": "rooms_changed"})
    return {"ok": True}


@app.post("/rooms/{room_id}/members/{username}/demote")
async def demote_member(room_id: int, username: str, user=Depends(current_user)):
    require_member(room_id, user["id"])
    if role_of(room_id, user["id"]) != "owner":
        raise HTTPException(403, "Снимать администраторов может только владелец")
    target = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if target is None or role_of(room_id, target["id"]) != "admin":
        raise HTTPException(404, "Не найден администратор с таким именем")
    db.execute("UPDATE room_members SET role = 'member' WHERE room_id = ? AND user_id = ?",
              (room_id, target["id"]))
    db.commit()
    await manager.send_to_users(member_ids(room_id), {"type": "rooms_changed"})
    return {"ok": True}


@app.get("/rooms/{room_id}/read_state")
async def read_state(room_id: int, user=Depends(current_user)):
    """До какого сообщения дочитал каждый участник — для галочек «доставлено/прочитано» (§7). Живые обновления
    приходят по WS (read_state); этот эндпоинт только для начального состояния при открытии чата."""
    require_member(room_id, user["id"])
    rows = db.execute(
        "SELECT u.username, m.last_read_id FROM room_members m "
        "JOIN users u ON u.id = m.user_id WHERE m.room_id = ? AND u.id != ?",
        (room_id, user["id"])).fetchall()
    return [dict(r) for r in rows]


async def mark_read(room_id: int, user_id: int, username: str, msg_id: int) -> None:
    """Двигает last_read_id вперёд (никогда назад — MAX) и рассылает остальным участникам, чем и питаются
    галочки «прочитано». Отдельная функция (не инлайн в цикле WS) — чтобы можно было позвать и из теста."""
    db.execute("UPDATE room_members SET last_read_id = MAX(last_read_id, ?) "
               "WHERE room_id = ? AND user_id = ?", (msg_id, room_id, user_id))
    db.commit()
    others = [m for m in member_ids(room_id) if m != user_id]
    await manager.send_to_users(others, {"type": "read_state", "room_id": room_id,
                                         "username": username, "message_id": msg_id})


@app.get("/keys")
async def my_keys(room_id: Optional[int] = None, user=Depends(current_user)):
    """Конверты с ключами комнат, адресованные мне, и актуальные эпохи."""
    sql = ("SELECT k.room_id, k.epoch, u.username AS sender, k.eph, k.n, k.c, k.sig "
           "FROM room_keys k JOIN users u ON u.id = k.sender_id WHERE k.recipient_id = ?")
    args: list = [user["id"]]
    lsql = ("SELECT r.id AS room_id, r.key_epoch FROM room_members m "
            "JOIN rooms r ON r.id = m.room_id WHERE m.user_id = ?")
    largs: list = [user["id"]]
    if room_id is not None:
        require_member(room_id, user["id"])
        sql += " AND k.room_id = ?"; args.append(room_id)
        lsql += " AND r.id = ?"; largs.append(room_id)
    return {"latest": {r["room_id"]: r["key_epoch"] for r in db.execute(lsql, largs)},
            "envelopes": [dict(r) for r in db.execute(sql + " ORDER BY k.room_id, k.epoch", args)]}


class EnvelopeIn(BaseModel):
    recipient: str = Field(max_length=64)
    eph: str = Field(max_length=200)
    n: str = Field(max_length=40)
    c: str = Field(max_length=120)
    sig: str = Field(max_length=120)


class KeysIn(BaseModel):
    epoch: int = Field(ge=1)
    envelopes: list[EnvelopeIn] = Field(min_length=1, max_length=2000)


@app.post("/rooms/{room_id}/keys")
async def post_keys(room_id: int, body: KeysIn, user=Depends(current_user)):
    """Новая эпоха ключа комнаты. Номер назначает сервер строго по порядку: при гонке
    побеждает первый, остальные получают 409 и берут его ключ. Конверт обязан быть
    выдан каждому текущему участнику, поэтому никого нельзя молча отстранить от чтения."""
    require_member(room_id, user["id"])
    current = db.execute("SELECT key_epoch FROM rooms WHERE id = ?", (room_id,)).fetchone()["key_epoch"]
    if body.epoch != current + 1:
        raise HTTPException(409, "Эпоха ключа уже создана другим участником")
    members = {r["username"]: r["id"] for r in db.execute(
        "SELECT u.id, u.username FROM room_members m JOIN users u ON u.id = m.user_id "
        "WHERE m.room_id = ?", (room_id,))}
    given = [e.recipient for e in body.envelopes]
    if len(set(given)) != len(given) or set(given) - set(members):
        raise HTTPException(422, "Неверный список получателей")
    if set(given) != set(members):
        raise HTTPException(409, "Состав участников изменился, повторите")
    for e in body.envelopes:
        if (b64len(e.eph), b64len(e.n), b64len(e.c), b64len(e.sig)) != (91, 12, 48, 64):
            raise HTTPException(422, "Неверный формат конверта")
    now = time.time()
    for e in body.envelopes:
        db.execute("INSERT INTO room_keys(room_id, epoch, recipient_id, sender_id, eph, n, c, sig, created) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (room_id, body.epoch, members[e.recipient], user["id"], e.eph, e.n, e.c, e.sig, now))
    db.execute("UPDATE rooms SET key_epoch = ? WHERE id = ?", (body.epoch, room_id))
    db.commit()
    await manager.send_to_users(list(members.values()),
                                {"type": "keys_updated", "room_id": room_id, "epoch": body.epoch})
    return {"ok": True, "epoch": body.epoch}


# ---------- сообщения ----------

def message_dict(row, reactions=None, pinned=False, votes=None) -> dict:
    return {"type": "message", "id": row["id"], "room_id": row["room_id"],
            "username": row["username"], "kind": row["kind"], "text": row["text"],
            "epoch": row["epoch"], "ct": row["ct"], "sig": row["sig"],
            "media": f"/files/{row['media']}" if row["media"] else None, "ts": row["ts"],
            "edited_ts": row["edited_ts"], "reactions": reactions or [], "pinned": pinned,
            "votes": votes or []}


@app.get("/rooms/{room_id}/messages")
async def history(room_id: int,
                  limit: int = Query(50, ge=1, le=200),
                  before_id: Optional[int] = None,
                  user=Depends(current_user)):
    require_member(room_id, user["id"])
    await sweep_autodelete(room_id)
    sql = ("SELECT id, room_id, username, kind, text, epoch, ct, sig, media, ts, edited_ts "
           "FROM messages WHERE room_id = ? "
           "AND id NOT IN (SELECT message_id FROM hidden_messages WHERE user_id = ?)")
    args: list = [room_id, user["id"]]
    if before_id is not None:
        sql += " AND id < ?"
        args.append(before_id)
    rows = db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    ids = [r["id"] for r in rows]
    reactions_by_msg: dict[int, list] = {}
    votes_by_msg: dict[int, list] = {}
    pinned_ids: set[int] = set()
    if ids:
        placeholders = ",".join("?" * len(ids))
        for rr in db.execute(
                f"SELECT message_id, username, epoch, ct, sig FROM reactions WHERE message_id IN ({placeholders})",
                ids).fetchall():
            reactions_by_msg.setdefault(rr["message_id"], []).append(
                {"username": rr["username"], "epoch": rr["epoch"], "ct": rr["ct"], "sig": rr["sig"]})
        for vv in db.execute(
                f"SELECT message_id, username, epoch, ct, sig FROM poll_votes WHERE message_id IN ({placeholders})",
                ids).fetchall():
            votes_by_msg.setdefault(vv["message_id"], []).append(
                {"username": vv["username"], "epoch": vv["epoch"], "ct": vv["ct"], "sig": vv["sig"]})
        pinned_ids = {r["message_id"] for r in db.execute(
            f"SELECT message_id FROM pinned_messages WHERE room_id = ? AND message_id IN ({placeholders})",
            (room_id, *ids)).fetchall()}
    return [message_dict(r, reactions_by_msg.get(r["id"]), r["id"] in pinned_ids, votes_by_msg.get(r["id"]))
            for r in reversed(rows)]


def check_e2e(room_id: int, epoch, ct, sig) -> Optional[tuple[str, str]]:
    """Проверяет форму зашифрованного сообщения. Содержимое сервер прочитать не может."""
    cur = db.execute("SELECT key_epoch FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if cur is None or not isinstance(epoch, int) or epoch < 1:
        return ("bad_message", "Некорректное сообщение")
    if epoch != cur["key_epoch"]:
        return ("stale_epoch", "Ключи комнаты обновились, отправьте сообщение ещё раз")
    n = b64len(ct)
    if not isinstance(ct, str) or len(ct) > MAX_CT or n is None or n < 28 or b64len(sig) != 64:
        return ("bad_message", "Некорректное сообщение")
    return None


async def post_message(user, room_id: int, *, kind: str = "e2e", text: str = "",
                       epoch: Optional[int] = None, ct: Optional[str] = None,
                       sig: Optional[str] = None, media: Optional[str] = None,
                       push_enabled: bool = True) -> dict:
    await sweep_autodelete(room_id)
    ts = time.time()
    cur = db.execute(
        "INSERT INTO messages(room_id, user_id, username, kind, text, epoch, ct, sig, media, ts) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (room_id, user["id"], user["username"], kind, text, epoch, ct, sig, media, ts))
    msg_id = cur.lastrowid
    db.execute("UPDATE room_members SET last_read_id = MAX(last_read_id, ?) "
               "WHERE room_id = ? AND user_id = ?", (msg_id, room_id, user["id"]))
    db.commit()
    payload = {"type": "message", "id": msg_id, "room_id": room_id, "username": user["username"],
               "kind": kind, "text": text, "epoch": epoch, "ct": ct, "sig": sig,
               "media": f"/files/{media}" if media else None, "ts": ts, "edited_ts": None,
               "reactions": [], "pinned": False, "votes": []}
    members = member_ids(room_id)
    await manager.send_to_users(members, payload)

    if push_enabled:  # push тем, у кого нет активного сокета
        offline = [uid for uid in members if uid != user["id"] and not manager.is_online(uid)]
        if offline:
            spawn(push_message(offline, user["username"], room_id))
    return payload


async def push_to_users(user_ids: list[int], title: str, body: str, data: dict) -> None:
    try:
        marks = ",".join("?" * len(user_ids))
        tokens = [r["fcm_token"] for r in db.execute(
            f"SELECT fcm_token FROM devices WHERE user_id IN ({marks})", user_ids)]
        if not tokens:
            return
        invalid = await push.notify(tokens, title, body, data)
        for tok in invalid:
            db.execute("DELETE FROM devices WHERE fcm_token = ?", (tok,))
        if invalid:
            db.commit()
    except Exception:
        log.exception("push failed")


async def push_message(user_ids, sender, room_id) -> None:
    """Текст сообщения серверу неизвестен, поэтому в push уходит только общий текст."""
    room = db.execute("SELECT name, is_direct FROM rooms WHERE id = ?", (room_id,)).fetchone()
    title = sender if room["is_direct"] else room["name"]
    await push_to_users(user_ids, title, "🔒 Новое сообщение", {"room_id": room_id, "type": "message"})


# ---------- зашифрованные вложения ----------

@app.post("/rooms/{room_id}/media")
async def upload_media(room_id: int,
                       epoch: int = Form(...), ct: str = Form(...), sig: str = Form(...),
                       file: UploadFile = File(...),
                       user=Depends(current_user)):
    """Файл уже зашифрован на устройстве (ключ лежит внутри зашифрованного сообщения ct)."""
    require_member(room_id, user["id"])
    if not can_post(room_id, user["id"]):
        raise HTTPException(403, "В этом канале публиковать могут только владелец и администраторы")
    if not limiter.allow(f"media:{user['id']}", 20, 60):
        raise HTTPException(429, "Слишком много файлов, подождите")
    err = check_e2e(room_id, epoch, ct, sig)
    if err:
        raise HTTPException(409 if err[0] == "stale_epoch" else 422, err[1])
    data = await file.read(MAX_ENC_FILE + 1)
    if len(data) > MAX_ENC_FILE:
        raise HTTPException(413, "Файл больше 32 МБ")
    if len(data) < 16:
        raise HTTPException(422, "Некорректный файл")
    name = f"{secrets.token_urlsafe(16)}.bin"
    await asyncio.to_thread((UPLOAD_DIR / name).write_bytes, data)
    return await post_message(user, room_id, epoch=epoch, ct=ct, sig=sig, media=name)


@app.get("/files/{name}")
async def get_file(name: str, user=Depends(current_user)):
    if not re.fullmatch(r"[\w-]{16,}\.bin", name):
        raise HTTPException(404)
    allowed = db.execute(
        "SELECT 1 FROM messages m JOIN room_members rm ON rm.room_id = m.room_id "
        "WHERE m.media = ? AND rm.user_id = ?", (name, user["id"])).fetchone()
    path = UPLOAD_DIR / name
    if not allowed or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"Cache-Control": "private, max-age=86400",
                                 "X-Content-Type-Options": "nosniff"})


# ---------- редактирование и удаление ----------
# Сервер не может прочитать сообщение, но видит, кто его отправил (username в строке) — этого достаточно,
# чтобы разрешать редактирование и «удаление для всех» только автору, не читая содержимое.

class EditIn(BaseModel):
    epoch: int = Field(ge=1)
    ct: str = Field(max_length=MAX_CT)
    sig: str = Field(max_length=100)


def _own_message(room_id: int, message_id: int, user) -> sqlite3.Row:
    row = db.execute(
        "SELECT id, room_id, user_id, username, kind, media FROM messages WHERE id = ? AND room_id = ?",
        (message_id, room_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Сообщение не найдено")
    if row["kind"] != "e2e":
        raise HTTPException(403, "Это сообщение нельзя изменить")
    if row["username"] != user["username"]:
        raise HTTPException(403, "Редактировать и удалять для всех можно только свои сообщения")
    return row


@app.put("/rooms/{room_id}/messages/{message_id}")
async def edit_message(room_id: int, message_id: int, body: EditIn, user=Depends(current_user)):
    require_member(room_id, user["id"])
    _own_message(room_id, message_id, user)
    err = check_e2e(room_id, body.epoch, body.ct, body.sig)
    if err:
        raise HTTPException(409 if err[0] == "stale_epoch" else 422, err[1])
    ts = time.time()
    db.execute("UPDATE messages SET epoch = ?, ct = ?, sig = ?, edited_ts = ? WHERE id = ?",
               (body.epoch, body.ct, body.sig, ts, message_id))
    db.commit()
    payload = {"type": "message_edited", "id": message_id, "room_id": room_id,
              "epoch": body.epoch, "ct": body.ct, "sig": body.sig, "edited_ts": ts}
    await manager.send_to_users(member_ids(room_id), payload)
    return payload


@app.delete("/rooms/{room_id}/messages/{message_id}")
async def delete_message(room_id: int, message_id: int, for_everyone: bool = False,
                         user=Depends(current_user)):
    require_member(room_id, user["id"])
    if for_everyone:
        row = _own_message(room_id, message_id, user)
        if row["media"]:
            path = UPLOAD_DIR / row["media"]
            await asyncio.to_thread(lambda: path.unlink(missing_ok=True))
        db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        db.execute("DELETE FROM hidden_messages WHERE message_id = ?", (message_id,))
        db.execute("DELETE FROM reactions WHERE message_id = ?", (message_id,))
        db.execute("DELETE FROM pinned_messages WHERE message_id = ?", (message_id,))
        db.execute("DELETE FROM poll_votes WHERE message_id = ?", (message_id,))
        db.commit()
        await manager.send_to_users(member_ids(room_id),
                                    {"type": "message_deleted", "id": message_id, "room_id": room_id})
    else:
        exists = db.execute("SELECT 1 FROM messages WHERE id = ? AND room_id = ?",
                            (message_id, room_id)).fetchone()
        if not exists:
            raise HTTPException(404, "Сообщение не найдено")
        db.execute("INSERT OR IGNORE INTO hidden_messages(user_id, message_id) VALUES(?, ?)",
                   (user["id"], message_id))
        db.commit()
    return {"ok": True}


# ---------- реакции ----------
# Как обычное сообщение: зашифрованы ключом комнаты, сервер видит лишь кто/когда/на что, не эмодзи.

class ReactionIn(BaseModel):
    epoch: int = Field(ge=1)
    ct: str = Field(max_length=MAX_CT)
    sig: str = Field(max_length=100)


@app.put("/rooms/{room_id}/messages/{message_id}/reaction")
async def set_reaction(room_id: int, message_id: int, body: ReactionIn, user=Depends(current_user)):
    require_member(room_id, user["id"])
    if not db.execute("SELECT 1 FROM messages WHERE id = ? AND room_id = ?", (message_id, room_id)).fetchone():
        raise HTTPException(404, "Сообщение не найдено")
    err = check_e2e(room_id, body.epoch, body.ct, body.sig)
    if err:
        raise HTTPException(409 if err[0] == "stale_epoch" else 422, err[1])
    ts = time.time()
    db.execute(
        "INSERT INTO reactions(room_id, message_id, user_id, username, epoch, ct, sig, ts) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(message_id, user_id) DO UPDATE SET epoch=excluded.epoch, ct=excluded.ct, "
        "sig=excluded.sig, ts=excluded.ts",
        (room_id, message_id, user["id"], user["username"], body.epoch, body.ct, body.sig, ts))
    db.commit()
    payload = {"type": "reaction_set", "room_id": room_id, "message_id": message_id, "username": user["username"],
              "epoch": body.epoch, "ct": body.ct, "sig": body.sig}
    await manager.send_to_users(member_ids(room_id), payload)
    return payload


@app.delete("/rooms/{room_id}/messages/{message_id}/reaction")
async def remove_reaction(room_id: int, message_id: int, user=Depends(current_user)):
    require_member(room_id, user["id"])
    db.execute("DELETE FROM reactions WHERE message_id = ? AND user_id = ?", (message_id, user["id"]))
    db.commit()
    payload = {"type": "reaction_removed", "room_id": room_id, "message_id": message_id, "username": user["username"]}
    await manager.send_to_users(member_ids(room_id), payload)
    return payload


# ---------- голоса в опросах ----------
# Опрос — обычное сообщение с t:"poll" внутри зашифрованного payload (вопрос и варианты), отдельной сущности
# на сервере для него нет. Голос — как реакция: зашифрован ключом комнаты, сервер видит только кто и когда
# проголосовал, не за какой вариант. Один голос на человека (переголосование заменяет прежний выбор).

class VoteIn(BaseModel):
    epoch: int = Field(ge=1)
    ct: str = Field(max_length=MAX_CT)
    sig: str = Field(max_length=100)


@app.put("/rooms/{room_id}/messages/{message_id}/vote")
async def set_vote(room_id: int, message_id: int, body: VoteIn, user=Depends(current_user)):
    require_member(room_id, user["id"])
    if not db.execute("SELECT 1 FROM messages WHERE id = ? AND room_id = ?", (message_id, room_id)).fetchone():
        raise HTTPException(404, "Сообщение не найдено")
    err = check_e2e(room_id, body.epoch, body.ct, body.sig)
    if err:
        raise HTTPException(409 if err[0] == "stale_epoch" else 422, err[1])
    ts = time.time()
    db.execute(
        "INSERT INTO poll_votes(room_id, message_id, user_id, username, epoch, ct, sig, ts) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(message_id, user_id) DO UPDATE SET epoch=excluded.epoch, ct=excluded.ct, "
        "sig=excluded.sig, ts=excluded.ts",
        (room_id, message_id, user["id"], user["username"], body.epoch, body.ct, body.sig, ts))
    db.commit()
    payload = {"type": "vote_set", "room_id": room_id, "message_id": message_id, "username": user["username"],
              "epoch": body.epoch, "ct": body.ct, "sig": body.sig}
    await manager.send_to_users(member_ids(room_id), payload)
    return payload


@app.delete("/rooms/{room_id}/messages/{message_id}/vote")
async def remove_vote(room_id: int, message_id: int, user=Depends(current_user)):
    require_member(room_id, user["id"])
    db.execute("DELETE FROM poll_votes WHERE message_id = ? AND user_id = ?", (message_id, user["id"]))
    db.commit()
    payload = {"type": "vote_removed", "room_id": room_id, "message_id": message_id, "username": user["username"]}
    await manager.send_to_users(member_ids(room_id), payload)
    return payload


# ---------- закреплённые сообщения ----------
# Прав/ролей в комнатах нет (см. README) — закрепить или открепить может любой участник, как и удалить «у себя».

@app.post("/rooms/{room_id}/pin/{message_id}")
async def pin_message(room_id: int, message_id: int, user=Depends(current_user)):
    require_member(room_id, user["id"])
    if not db.execute("SELECT 1 FROM messages WHERE id = ? AND room_id = ?", (message_id, room_id)).fetchone():
        raise HTTPException(404, "Сообщение не найдено")
    db.execute("INSERT OR REPLACE INTO pinned_messages(room_id, message_id, pinned_by, pinned_at) VALUES(?,?,?,?)",
              (room_id, message_id, user["username"], time.time()))
    db.commit()
    payload = {"type": "message_pinned", "room_id": room_id, "message_id": message_id, "pinned_by": user["username"]}
    await manager.send_to_users(member_ids(room_id), payload)
    return payload


@app.delete("/rooms/{room_id}/pin/{message_id}")
async def unpin_message(room_id: int, message_id: int, user=Depends(current_user)):
    require_member(room_id, user["id"])
    db.execute("DELETE FROM pinned_messages WHERE room_id = ? AND message_id = ?", (room_id, message_id))
    db.commit()
    payload = {"type": "message_unpinned", "room_id": room_id, "message_id": message_id}
    await manager.send_to_users(member_ids(room_id), payload)
    return payload


@app.get("/rooms/{room_id}/pins")
async def list_pins(room_id: int, user=Depends(current_user)):
    require_member(room_id, user["id"])
    rows = db.execute(
        "SELECT m.id, m.room_id, m.username, m.kind, m.text, m.epoch, m.ct, m.sig, m.media, m.ts, m.edited_ts "
        "FROM pinned_messages p JOIN messages m ON m.id = p.message_id "
        "WHERE p.room_id = ? ORDER BY p.pinned_at", (room_id,)).fetchall()
    return [message_dict(r) for r in rows]


# ---------- самоудаление сообщений ----------

class AutodeleteIn(BaseModel):
    seconds: Optional[int] = Field(default=None, ge=60, le=31_536_000)  # от минуты до года; null — выключено


@app.put("/rooms/{room_id}/autodelete")
async def set_autodelete(room_id: int, body: AutodeleteIn, user=Depends(current_user)):
    require_member(room_id, user["id"])
    db.execute("UPDATE rooms SET autodelete_seconds = ? WHERE id = ?", (body.seconds, room_id))
    db.commit()
    payload = {"type": "autodelete_changed", "room_id": room_id, "seconds": body.seconds}
    await manager.send_to_users(member_ids(room_id), payload)
    return payload


async def sweep_autodelete(room_id: int) -> None:
    """Стирает сообщения старше room.autodelete_seconds. Вызывается лениво — при чтении истории и при
    отправке нового сообщения — а не отдельным фоновым процессом, чтобы не держать лишнюю задачу постоянно
    живой. Раз сообщение всё равно принадлежит только этой комнате, гостю без доступа его не покажут и без
    подчистки (см. require_member), так что задержка до следующего обращения не протекает наружу."""
    row = db.execute("SELECT autodelete_seconds FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if row is None or not row["autodelete_seconds"]:
        return
    cutoff = time.time() - row["autodelete_seconds"]
    old = db.execute("SELECT id, media FROM messages WHERE room_id = ? AND ts < ? AND kind = 'e2e'",
                     (room_id, cutoff)).fetchall()
    if not old:
        return
    ids = [r["id"] for r in old]
    for r in old:
        if r["media"]:
            path = UPLOAD_DIR / r["media"]
            await asyncio.to_thread(lambda p=path: p.unlink(missing_ok=True))
    placeholders = ",".join("?" * len(ids))
    db.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", ids)
    db.execute(f"DELETE FROM hidden_messages WHERE message_id IN ({placeholders})", ids)
    db.execute(f"DELETE FROM reactions WHERE message_id IN ({placeholders})", ids)
    db.execute(f"DELETE FROM pinned_messages WHERE message_id IN ({placeholders})", ids)
    db.execute(f"DELETE FROM poll_votes WHERE message_id IN ({placeholders})", ids)
    db.commit()
    member_ids_list = member_ids(room_id)
    for mid in ids:
        await manager.send_to_users(member_ids_list, {"type": "message_deleted", "id": mid, "room_id": room_id})


# ---------- отложенная отправка ----------
# Только текст — файл на момент планирования ещё не обязательно выбран/зашифрован, а откладывать сам процесс
# загрузки усложнило бы протокол непропорционально ценности функции. Видно и отменить может только автор:
# другим участникам о существовании отложенного сообщения знать не положено, оно ничем не отличается от
# сообщения, которое человек просто ещё не написал.

MAX_SCHEDULE_SECONDS = 30 * 24 * 3600  # не дальше месяца вперёд — подстраховка от опечатки в дате
_sweep_task: Optional[asyncio.Task] = None


def ensure_scheduler_running() -> None:
    global _sweep_task
    if _sweep_task is None or _sweep_task.done():
        _sweep_task = spawn(_scheduled_sweep_loop())


async def _scheduled_sweep_loop() -> None:
    while True:
        try:
            await sweep_scheduled_messages()
        except Exception:
            pass  # один сбой не должен останавливать цикл целиком
        await asyncio.sleep(10)


async def sweep_scheduled_messages() -> None:
    due = db.execute("SELECT * FROM scheduled_messages WHERE send_at <= ?", (time.time(),)).fetchall()
    for row in due:
        user = {"id": row["user_id"], "username": row["username"]}
        try:
            await post_message(user, row["room_id"], epoch=row["epoch"], ct=row["ct"], sig=row["sig"])
        finally:
            db.execute("DELETE FROM scheduled_messages WHERE id = ?", (row["id"],))
            db.commit()


class ScheduleIn(BaseModel):
    epoch: int = Field(ge=1)
    ct: str = Field(max_length=MAX_CT)
    sig: str = Field(max_length=100)
    send_at: float


def _schedule_dict(row) -> dict:
    return {"id": row["id"], "room_id": row["room_id"], "send_at": row["send_at"], "created": row["created"]}


@app.post("/rooms/{room_id}/schedule")
async def schedule_message(room_id: int, body: ScheduleIn, user=Depends(current_user)):
    require_member(room_id, user["id"])
    if not can_post(room_id, user["id"]):
        raise HTTPException(403, "В этом канале публиковать могут только владелец и администраторы")
    err = check_e2e(room_id, body.epoch, body.ct, body.sig)
    if err:
        raise HTTPException(409 if err[0] == "stale_epoch" else 422, err[1])
    now = time.time()
    if body.send_at <= now:
        raise HTTPException(422, "Время отправки должно быть в будущем")
    if body.send_at > now + MAX_SCHEDULE_SECONDS:
        raise HTTPException(422, "Не более чем на месяц вперёд")
    cur = db.execute(
        "INSERT INTO scheduled_messages(room_id, user_id, username, epoch, ct, sig, send_at, created) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (room_id, user["id"], user["username"], body.epoch, body.ct, body.sig, body.send_at, now))
    db.commit()
    ensure_scheduler_running()
    row = db.execute("SELECT * FROM scheduled_messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _schedule_dict(row)


@app.get("/rooms/{room_id}/scheduled")
async def list_scheduled(room_id: int, user=Depends(current_user)):
    require_member(room_id, user["id"])
    rows = db.execute(
        "SELECT * FROM scheduled_messages WHERE room_id = ? AND user_id = ? ORDER BY send_at",
        (room_id, user["id"])).fetchall()
    return [_schedule_dict(r) for r in rows]


@app.delete("/rooms/{room_id}/scheduled/{scheduled_id}")
async def cancel_scheduled(room_id: int, scheduled_id: int, user=Depends(current_user)):
    require_member(room_id, user["id"])
    row = db.execute("SELECT id FROM scheduled_messages WHERE id = ? AND room_id = ? AND user_id = ?",
                     (scheduled_id, room_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(404, "Не найдено (или это не ваше отложенное сообщение)")
    db.execute("DELETE FROM scheduled_messages WHERE id = ?", (scheduled_id,))
    db.commit()
    return {"ok": True}


# ---------- каталог стикеров ----------
# Открытый (не E2EE) общий каталог: паки создают сами пользователи, содержимое видно всем. Это не переписка,
# а витрина — тот же уровень публичности, что и профиль/био (§6). Стикер отправляется в чат уже потом, как
# самое обычное зашифрованное фото — сервер здесь просто раздаёт картинки каталога всем авторизованным.

_STICKER_MAGIC = (
    (b"\xff\xd8\xff", None),                       # JPEG
    (b"\x89PNG\r\n\x1a\n", None),                  # PNG
    (b"GIF87a", None), (b"GIF89a", None),          # GIF
    (b"RIFF", b"WEBP"),                            # WEBP (RIFF....WEBP)
)


def _looks_like_image(data: bytes) -> bool:
    for prefix, mid in _STICKER_MAGIC:
        if data.startswith(prefix) and (mid is None or data[8:12] == mid):
            return True
    return False


def _image_ext(data: bytes) -> str:
    return (
        "jpg" if data.startswith(b"\xff\xd8\xff") else
        "png" if data.startswith(b"\x89PNG") else
        "gif" if data.startswith((b"GIF87a", b"GIF89a")) else
        "webp" if data.startswith(b"RIFF") else "bin"
    )


def _pack_dict(row) -> dict:
    count = db.execute("SELECT COUNT(*) c FROM stickers WHERE pack_id = ?", (row["id"],)).fetchone()["c"]
    return {"id": row["id"], "name": row["name"], "creator_username": row["creator_username"],
            "created": row["created"], "sticker_count": count}


def _sticker_dict(row) -> dict:
    return {"id": row["id"], "url": f"/stickers/files/{row['filename']}", "position": row["position"]}


class PackIn(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_PACK_NAME)


@app.post("/stickers/packs")
async def create_pack(body: PackIn, user=Depends(current_user)):
    count = db.execute("SELECT COUNT(*) c FROM sticker_packs WHERE creator_id = ?", (user["id"],)).fetchone()["c"]
    if count >= MAX_PACKS_PER_USER:
        raise HTTPException(422, f"Не больше {MAX_PACKS_PER_USER} паков от одного пользователя")
    if not limiter.allow(f"pack_create:{user['id']}", 10, 3600):
        raise HTTPException(429, "Слишком много новых паков, подождите")
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Название не может быть пустым")
    now = time.time()
    cur = db.execute("INSERT INTO sticker_packs(name, creator_id, creator_username, created) VALUES(?,?,?,?)",
                     (name, user["id"], user["username"], now))
    db.commit()
    row = db.execute("SELECT * FROM sticker_packs WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _pack_dict(row)


@app.get("/stickers/packs")
async def search_packs(q: str = "", limit: int = Query(30, ge=1, le=100),
                       before_id: Optional[int] = None, user=Depends(current_user)):
    sql = "SELECT * FROM sticker_packs WHERE 1=1"
    args: list = []
    if q.strip():
        sql += " AND name LIKE ? ESCAPE '\\'"
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.append(f"%{escaped}%")
    if before_id is not None:
        sql += " AND id < ?"
        args.append(before_id)
    rows = db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return [_pack_dict(r) for r in rows]


@app.get("/stickers/packs/{pack_id}")
async def get_pack(pack_id: int, user=Depends(current_user)):
    row = db.execute("SELECT * FROM sticker_packs WHERE id = ?", (pack_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Пак не найден")
    stickers = db.execute("SELECT * FROM stickers WHERE pack_id = ? ORDER BY position", (pack_id,)).fetchall()
    installed = db.execute("SELECT 1 FROM installed_packs WHERE user_id = ? AND pack_id = ?",
                           (user["id"], pack_id)).fetchone() is not None
    return {**_pack_dict(row), "installed": installed, "stickers": [_sticker_dict(s) for s in stickers]}


@app.delete("/stickers/packs/{pack_id}")
async def delete_pack(pack_id: int, user=Depends(current_user)):
    row = db.execute("SELECT creator_id FROM sticker_packs WHERE id = ?", (pack_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Пак не найден")
    if row["creator_id"] != user["id"]:
        raise HTTPException(403, "Удалить пак может только его автор")
    files = [r["filename"] for r in db.execute("SELECT filename FROM stickers WHERE pack_id = ?", (pack_id,)).fetchall()]
    db.execute("DELETE FROM stickers WHERE pack_id = ?", (pack_id,))
    db.execute("DELETE FROM installed_packs WHERE pack_id = ?", (pack_id,))
    db.execute("DELETE FROM sticker_packs WHERE id = ?", (pack_id,))
    db.commit()
    for fn in files:
        await asyncio.to_thread(lambda p=fn: (STICKER_DIR / p).unlink(missing_ok=True))
    return {"ok": True}


@app.post("/stickers/packs/{pack_id}/stickers")
async def add_sticker(pack_id: int, file: UploadFile = File(...), user=Depends(current_user)):
    row = db.execute("SELECT creator_id FROM sticker_packs WHERE id = ?", (pack_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Пак не найден")
    if row["creator_id"] != user["id"]:
        raise HTTPException(403, "Добавлять стикеры может только автор пака")
    count = db.execute("SELECT COUNT(*) c FROM stickers WHERE pack_id = ?", (pack_id,)).fetchone()["c"]
    if count >= MAX_STICKERS_PER_PACK:
        raise HTTPException(422, f"Не больше {MAX_STICKERS_PER_PACK} стикеров в паке")
    if not limiter.allow(f"sticker_add:{user['id']}", 30, 60):
        raise HTTPException(429, "Слишком много стикеров, подождите")
    data = await file.read(MAX_STICKER_BYTES + 1)
    if len(data) > MAX_STICKER_BYTES:
        raise HTTPException(413, "Стикер больше 2 МБ")
    if not _looks_like_image(data):
        raise HTTPException(422, "Похоже, это не картинка (поддерживаются JPEG, PNG, GIF, WEBP)")
    ext = _image_ext(data)
    name = f"{secrets.token_urlsafe(16)}.{ext}"
    await asyncio.to_thread((STICKER_DIR / name).write_bytes, data)
    now = time.time()
    cur = db.execute("INSERT INTO stickers(pack_id, filename, position, created) VALUES(?,?,?,?)",
                     (pack_id, name, count, now))
    db.commit()
    srow = db.execute("SELECT * FROM stickers WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _sticker_dict(srow)


@app.delete("/stickers/packs/{pack_id}/stickers/{sticker_id}")
async def delete_sticker(pack_id: int, sticker_id: int, user=Depends(current_user)):
    prow = db.execute("SELECT creator_id FROM sticker_packs WHERE id = ?", (pack_id,)).fetchone()
    if prow is None:
        raise HTTPException(404, "Пак не найден")
    if prow["creator_id"] != user["id"]:
        raise HTTPException(403, "Удалять стикеры может только автор пака")
    srow = db.execute("SELECT filename FROM stickers WHERE id = ? AND pack_id = ?", (sticker_id, pack_id)).fetchone()
    if srow is None:
        raise HTTPException(404, "Стикер не найден")
    db.execute("DELETE FROM stickers WHERE id = ?", (sticker_id,))
    db.commit()
    await asyncio.to_thread(lambda: (STICKER_DIR / srow["filename"]).unlink(missing_ok=True))
    return {"ok": True}


@app.post("/stickers/packs/{pack_id}/install")
async def install_pack(pack_id: int, user=Depends(current_user)):
    if db.execute("SELECT 1 FROM sticker_packs WHERE id = ?", (pack_id,)).fetchone() is None:
        raise HTTPException(404, "Пак не найден")
    db.execute("INSERT OR IGNORE INTO installed_packs(user_id, pack_id, installed_at) VALUES(?,?,?)",
              (user["id"], pack_id, time.time()))
    db.commit()
    return {"ok": True}


@app.delete("/stickers/packs/{pack_id}/install")
async def uninstall_pack(pack_id: int, user=Depends(current_user)):
    db.execute("DELETE FROM installed_packs WHERE user_id = ? AND pack_id = ?", (user["id"], pack_id))
    db.commit()
    return {"ok": True}


@app.get("/stickers/installed")
async def installed_packs(user=Depends(current_user)):
    rows = db.execute(
        "SELECT p.* FROM installed_packs i JOIN sticker_packs p ON p.id = i.pack_id "
        "WHERE i.user_id = ? ORDER BY i.installed_at", (user["id"],)).fetchall()
    out = []
    for row in rows:
        stickers = db.execute("SELECT * FROM stickers WHERE pack_id = ? ORDER BY position", (row["id"],)).fetchall()
        out.append({**_pack_dict(row), "stickers": [_sticker_dict(s) for s in stickers]})
    return out


@app.get("/stickers/files/{name}")
async def get_sticker_file(name: str, user=Depends(current_user)):
    if not re.fullmatch(r"[\w-]{16,}\.(jpg|png|gif|webp|bin)", name):
        raise HTTPException(404)
    path = STICKER_DIR / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"Cache-Control": "public, max-age=86400",
                                 "X-Content-Type-Options": "nosniff"})


# ---------- распознавание речи (по желанию, выключено по умолчанию) ----------
# Единственное место в проекте, где сервер намеренно видит расшифрованное содержимое: клиент сам расшифровывает
# голосовое (как и для обычного воспроизведения) и шлёт сюда уже открытые байты отдельным запросом — это не
# часть обычного зашифрованного сообщения и никак на него не влияет. Сервер ничего не сохраняет: ни аудио,
# ни результат, оба живут только в памяти одного запроса. Это осмысленно только для самостоятельно
# размещённого сервера, которым управляет тот же человек, что и пользуется им, — поэтому выключено по
# умолчанию и требует явного действия оператора (см. README §14), а не включено "из коробки".
WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL") or None   # например "small" — пусто/не задано = выключено
_whisper_model = None
_whisper_lock = threading.Lock()


def _load_whisper():
    """Загружает модель один раз и держит в памяти процесса; вызывается из рабочего потока (см. ниже),
    не из event loop — загрузка идёт секунды, блокировать на ней асинхронный цикл нельзя."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel  # импорт внутри функции: не требуем пакет, если функция выключена
                _whisper_model = WhisperModel(WHISPER_MODEL_NAME, device="cpu", compute_type="int8")
    return _whisper_model


def _transcribe_sync(data: bytes) -> str:
    """Синхронная часть (модель и ffmpeg внутри неё не умеют в asyncio) — вызывается через asyncio.to_thread."""
    model = _load_whisper()
    with tempfile.NamedTemporaryFile(suffix=".m4a") as tmp:
        tmp.write(data)
        tmp.flush()
        segments, _info = model.transcribe(tmp.name)
        return " ".join(seg.text.strip() for seg in segments).strip()


@app.post("/speech/transcribe")
async def transcribe_speech(file: UploadFile = File(...), user=Depends(current_user)):
    if not WHISPER_MODEL_NAME:
        raise HTTPException(503, "Распознавание речи не настроено на этом сервере")
    if not limiter.allow(f"transcribe:{user['id']}", 10, 60):
        raise HTTPException(429, "Слишком часто, подождите")
    data = await file.read(MAX_TRANSCRIBE_BYTES + 1)
    if len(data) > MAX_TRANSCRIBE_BYTES:
        raise HTTPException(413, "Файл больше 25 МБ")
    if len(data) < 16:
        raise HTTPException(422, "Некорректный файл")
    try:
        text = await asyncio.to_thread(_transcribe_sync, data)
    except HTTPException:
        raise
    except Exception:
        log.exception("распознавание речи не удалось")
        raise HTTPException(500, "Не удалось распознать речь")
    return {"text": text}


# ---------- устройства для push ----------

class DeviceIn(BaseModel):
    token: str = Field(min_length=20, max_length=4096)


@app.post("/devices")
async def register_device(body: DeviceIn, user=Depends(current_user)):
    db.execute("INSERT OR REPLACE INTO devices(fcm_token, user_id) VALUES(?, ?)",
               (body.token, user["id"]))
    db.commit()
    return {"ok": True}


@app.delete("/devices")
async def unregister_device(token: str = Query(...), user=Depends(current_user)):
    db.execute("DELETE FROM devices WHERE fcm_token = ? AND user_id = ?",
               (token, user["id"]))
    db.commit()
    return {"ok": True}


# ---------- WebRTC: ICE-серверы ----------

@app.get("/webrtc/config")
async def webrtc_config(user=Depends(current_user)):
    """STUN всегда; TURN — если заданы TURN_URLS и TURN_SECRET (схема coturn use-auth-secret)."""
    servers: list[dict] = [{"urls": ["stun:stun.l.google.com:19302"]}]
    urls = [u.strip() for u in os.getenv("TURN_URLS", "").split(",") if u.strip()]
    secret = os.getenv("TURN_SECRET")
    if urls and secret:
        username = f"{int(time.time()) + 3600}:{user['id']}"
        credential = base64.b64encode(
            hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()).decode()
        servers.append({"urls": urls, "username": username, "credential": credential})
    return {"iceServers": servers}


# ---------- звонки (сигналинг; личный звонок — просто групповой с двумя участниками) ----------
# SDP и ICE-кандидаты приходят уже зашифрованными ключом конкретного получателя (E2EE.md), сервер лишь
# пересылает их адресно между парой участников. Аудио и видео идут напрямую между клиентами (DTLS-SRTP,
# mesh — каждый с каждым), сервер их не видит и не трогает; поэтому участников группового звонка стоит
# держать небольшим числом — соединений растёт как N*(N-1)/2 (см. README, ограничение MAX_CALL_PARTICIPANTS).

MAX_CALL_PARTICIPANTS = 8


@dataclass
class Call:
    id: str
    room_id: int
    video: bool
    initiator: int
    initiator_name: str
    participants: dict[int, str] = field(default_factory=dict)  # uid -> username, уже в звонке
    invited: dict[int, str] = field(default_factory=dict)       # uid -> username, приглашены, не ответили
    started: Optional[float] = None   # когда стало >= 2 участников — звонок состоялся, не просто гудки
    ring_timers: dict[int, asyncio.Task] = field(default_factory=dict)


calls: dict[str, Call] = {}


def call_of(user_id: int) -> Optional[Call]:
    for c in calls.values():
        if user_id in c.participants or user_id in c.invited:
            return c
    return None


def invite_payload(c: Call) -> dict:
    return {"type": "call_invite", "call_id": c.id, "room_id": c.room_id, "from": c.initiator_name,
            "video": c.video, "group": len(c.participants) + len(c.invited) > 2}


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


async def finish_call(c: Call, reason: str) -> None:
    """Завершает звонок целиком (для всех), пишет итог в чат (метаданные, не шифруются)."""
    if calls.pop(c.id, None) is None:
        return
    for t in c.ring_timers.values():
        if t is not asyncio.current_task():
            t.cancel()
    everyone = list(set(c.participants) | set(c.invited))
    await manager.send_to_users(everyone, {"type": "call_end", "call_id": c.id, "reason": reason})
    word = "видеозвонок" if c.video else "аудиозвонок"
    if c.started:
        text = f"{word.capitalize()} · {fmt_dur(time.time() - c.started)}"
    elif reason == "declined":
        text = f"{word.capitalize()} отклонён"
    else:
        text = f"Пропущенный {word}"
    await post_message({"id": c.initiator, "username": c.initiator_name}, c.room_id,
                       kind="call", text=text, push_enabled=False)


def _only_initiator_left(c: Call) -> bool:
    return not c.invited and set(c.participants) == {c.initiator}


async def _ring_timeout_one(call_id: str, invitee: int) -> None:
    await asyncio.sleep(RING_TIMEOUT)
    c = calls.get(call_id)
    if c is None or invitee not in c.invited:
        return
    c.invited.pop(invitee, None)
    c.ring_timers.pop(invitee, None)
    if _only_initiator_left(c):
        await finish_call(c, "no_answer")
    else:
        await manager.send_to_users(list(c.participants), {"type": "call_peer_left", "call_id": c.id, "user_id": invitee})


async def _leave_call(c: Call, uid: int, reason: str) -> None:
    """Один участник выходит — если остаётся хотя бы двое (участников и ещё звонящих суммарно), звонок для
    них продолжается; иначе одному человеку разговаривать не с кем — звонок завершается целиком."""
    if uid not in c.participants:
        return
    username = c.participants.pop(uid)
    remaining = list(c.participants) + list(c.invited)
    if len(remaining) < 2:
        c.participants[uid] = username   # вернуть — нужен для finish_call (итог в чат пишется от initiator)
        await finish_call(c, reason)
        return
    await manager.send_to_users(list(c.participants), {"type": "call_peer_left", "call_id": c.id, "user_id": uid})
    await manager.send_to_users([uid], {"type": "call_end", "call_id": c.id, "reason": reason})


async def _reconnect_grace(call_id: str, user_id: int) -> None:
    await asyncio.sleep(RECONNECT_GRACE)
    c = calls.get(call_id)
    if c and user_id in c.participants and not manager.is_online(user_id):
        await _leave_call(c, user_id, "connection_lost")


async def handle_call(user, kind: str, data: dict, error) -> None:
    uid = user["id"]

    if kind == "call_invite":
        room_id = int(data["room_id"])
        call_id = str(data["call_id"])[:64]
        video = bool(data.get("video"))
        if not is_member(room_id, uid):
            return await error("Нет доступа к этой комнате")
        if not limiter.allow(f"call:{uid}", 6, 60):
            return await error("Слишком много звонков, подождите")
        if call_of(uid) or call_id in calls:
            return await error("Вы уже в звонке")
        others = [m for m in member_ids(room_id) if m != uid][:MAX_CALL_PARTICIPANTS - 1]
        if not others:
            return await error("В комнате больше никого нет")
        free = [m for m in others if not call_of(m)]
        if not free:
            await manager.send_to_users([uid], {"type": "call_end", "call_id": call_id, "reason": "busy"})
            return
        placeholders = ",".join("?" * len(free))
        names = {r["id"]: r["username"] for r in
                db.execute(f"SELECT id, username FROM users WHERE id IN ({placeholders})", free).fetchall()}
        c = Call(call_id, room_id, video, uid, user["username"],
                participants={uid: user["username"]}, invited=names)
        calls[call_id] = c
        await manager.send_to_users(free, invite_payload(c))
        offline = [m for m in free if not manager.is_online(m)]
        if offline:
            word = "видеозвонок" if video else "аудиозвонок"
            spawn(push_to_users(offline, user["username"], f"Входящий {word}",
                                {"room_id": room_id, "type": "call"}))
        for m in free:
            c.ring_timers[m] = spawn(_ring_timeout_one(call_id, m))
        return

    c = calls.get(str(data.get("call_id")))
    if c is None or (uid not in c.participants and uid not in c.invited):
        return

    if kind == "call_accept":
        if uid in c.invited:
            username = c.invited.pop(uid)
            t = c.ring_timers.pop(uid, None)
            if t:
                t.cancel()
            c.participants[uid] = username
            if c.started is None and len(c.participants) >= 2:
                c.started = time.time()
            others = [p for p in c.participants if p != uid]
            await manager.send_to_users([uid], {
                "type": "call_joined", "call_id": c.id, "video": c.video,
                "participants": [{"id": p, "username": c.participants[p]} for p in others],
            })
            await manager.send_to_users(others, {"type": "call_peer_joined", "call_id": c.id,
                                                  "user_id": uid, "username": username})
    elif kind == "call_reject":
        if uid in c.invited:
            c.invited.pop(uid, None)
            t = c.ring_timers.pop(uid, None)
            if t:
                t.cancel()
            if _only_initiator_left(c):
                await finish_call(c, "declined")
            else:
                await manager.send_to_users(list(c.participants), {"type": "call_peer_left", "call_id": c.id, "user_id": uid})
    elif kind == "call_end":
        if uid in c.invited:
            c.invited.pop(uid, None)
            t = c.ring_timers.pop(uid, None)
            if t:
                t.cancel()
            if _only_initiator_left(c):
                await finish_call(c, "cancelled")
            else:
                await manager.send_to_users(list(c.participants), {"type": "call_peer_left", "call_id": c.id, "user_id": uid})
        else:
            reason = "cancelled" if c.started is None else "ended"
            await _leave_call(c, uid, reason)
    elif kind == "call_signal":
        sealed = data.get("data")
        try:
            to_uid = int(data.get("to"))
        except (TypeError, ValueError):
            return
        if (to_uid in c.participants and uid in c.participants and isinstance(sealed, dict)
                and isinstance(sealed.get("n"), str) and isinstance(sealed.get("c"), str)
                and len(sealed["n"]) + len(sealed["c"]) <= MAX_SIGNAL):
            await manager.send_to_users([to_uid], {"type": "call_signal", "call_id": c.id,
                                                    "from": user["username"], "from_id": uid,
                                                    "data": {"n": sealed["n"], "c": sealed["c"]}})


# ---------- WebSocket ----------
# клиент -> сервер:
#   {"type":"message","room_id":1,"epoch":2,"ct":"...","sig":"..."}   (ct/sig — E2EE)
#   {"type":"typing","room_id":1}
#   {"type":"read","room_id":1,"message_id":42}
#   {"type":"call_invite","room_id":5,"call_id":"uuid","video":true}
#   {"type":"call_accept"|"call_reject"|"call_end","call_id":"uuid"}
#   {"type":"call_signal","call_id":"uuid","data":{"n":"...","c":"..."}}   (зашифровано)
# сервер -> клиент:
#   message | typing | rooms_changed | keys_updated | error{code,room_id} |
#   call_invite | call_accepted | call_signal | call_end{reason}

@app.websocket("/ws")
async def chat_ws(ws: WebSocket, token: str = ""):
    user = user_by_token(token)
    if user is None:
        await ws.close(code=4401)
        return
    uid = user["id"]
    await manager.connect(uid, ws)

    async def error(text: str, code: str = "error", room_id: Optional[int] = None):
        await ws.send_json({"type": "error", "message": text, "code": code, "room_id": room_id})

    try:
        # входящий звонок, который начался, пока приложение было закрыто
        for c in list(calls.values()):
            if uid in c.invited:
                await ws.send_json(invite_payload(c))

        while True:
            try:
                data = await ws.receive_json()
                kind = str(data.get("type"))
            except (ValueError, TypeError, AttributeError):
                continue  # мусор вместо JSON — игнорируем

            if kind.startswith("call_"):
                try:
                    await handle_call(user, kind, data, error)
                except (KeyError, ValueError, TypeError, StopIteration):
                    pass
                continue

            try:
                room_id = int(data.get("room_id"))
            except (TypeError, ValueError):
                continue
            if not is_member(room_id, uid):
                await error("Вы не участник этой комнаты")
                continue

            if kind == "message":
                if not limiter.allow(f"msg:{uid}", 10, 10):
                    await error("Слишком часто, подождите немного")
                    continue
                if not can_post(room_id, uid):
                    await error("В этом канале писать могут только владелец и администраторы", room_id=room_id)
                    continue
                epoch, ct, sig = data.get("epoch"), data.get("ct"), data.get("sig")
                bad = check_e2e(room_id, epoch, ct, sig)
                if bad:
                    await error(bad[1], bad[0], room_id)
                    continue
                await post_message(user, room_id, epoch=epoch, ct=ct, sig=sig)
            elif kind == "typing":
                others = [m for m in member_ids(room_id) if m != uid]
                await manager.send_to_users(
                    others, {"type": "typing", "room_id": room_id,
                             "username": user["username"]})
            elif kind == "read":
                try:
                    msg_id = int(data.get("message_id"))
                except (ValueError, TypeError):
                    continue
                await mark_read(room_id, uid, user["username"], msg_id)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(uid, ws)
        c = call_of(uid)
        if not manager.is_online(uid) and c and uid in c.participants:
            spawn(_reconnect_grace(c.id, uid))


# ---------- клиенты для компьютера ----------
# Собранные установщики лежат в папке downloads/ и отдаются без авторизации: ссылки показывает страница входа
# веб-версии. Имя файла строго проверяется, поэтому отдать что-то постороннее из папки нельзя.

DOWNLOAD_RE = re.compile(r"Freedom[-_][\w.+~-]{1,120}\.(exe|deb|rpm|apk)", re.ASCII)
_DL_ORDER = {"exe": 0, "deb": 1, "rpm": 2, "apk": 3}
_DL_PLATFORM = {"exe": "windows", "deb": "linux", "rpm": "linux", "apk": "android"}
_hash_cache: dict[str, tuple[int, int, str]] = {}


def _file_sha256(path: Path) -> str:
    st = path.stat()
    hit = _hash_cache.get(str(path))
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    _hash_cache[str(path)] = (st.st_mtime_ns, st.st_size, h.hexdigest())
    return h.hexdigest()


def _list_downloads() -> list[dict]:
    """По одному (самому свежему) установщику каждого типа: Windows .exe, Linux .deb/.rpm, Android .apk."""
    if not DOWNLOADS_DIR.is_dir():
        return []
    newest: dict[str, Path] = {}
    for p in DOWNLOADS_DIR.iterdir():
        if p.is_symlink() or not p.is_file() or not DOWNLOAD_RE.fullmatch(p.name):
            continue
        kind = p.suffix[1:]
        if kind not in newest or p.stat().st_mtime > newest[kind].stat().st_mtime:
            newest[kind] = p
    out = []
    for kind in sorted(newest, key=_DL_ORDER.__getitem__):
        p = newest[kind]
        ver = re.search(r"\d+\.\d+(?:\.\d+)?", p.name)
        out.append({"name": p.name, "url": f"/downloads/{p.name}", "kind": kind,
                    "platform": _DL_PLATFORM[kind],
                    "size": p.stat().st_size, "sha256": _file_sha256(p),
                    "version": ver.group(0) if ver else None})
    return out


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


@app.get("/api/ping")
async def ping():
    """Опознавательный ответ: по нему клиент убеждается, что по адресу работает Freedom, и проверяет версию."""
    return {"app": "freedom", "protocol": "FRDM-E2EE v1", "version": APP_VERSION,
            "min_client_version": MIN_CLIENT_VERSION}


@app.get("/api/downloads")
async def downloads_list(request: Request):
    if not limiter.allow(f"dlist:{_client_ip(request)}", 60, 60):
        raise HTTPException(429, "Слишком много запросов, подождите")
    return await asyncio.to_thread(_list_downloads)


@app.get("/downloads/{name}")
async def download_file(name: str, request: Request):
    if not DOWNLOAD_RE.fullmatch(name):
        raise HTTPException(404)
    if not limiter.allow(f"dl:{_client_ip(request)}", 20, 60):
        raise HTTPException(429, "Слишком много загрузок, подождите")
    path = DOWNLOADS_DIR / name
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/octet-stream", filename=name,
                        headers={"X-Content-Type-Options": "nosniff"})


# ---------- веб-версия (монтируется последней, чтобы не перекрывать API) ----------

if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
