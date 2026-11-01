"""Роли участников и каналы: main.py, FastAPI подменён заглушкой (как в остальных server-тестах)."""
import asyncio
import base64
import os
import sys
import tempfile
import time
import types

_tmp = tempfile.mkdtemp()
os.environ["CHAT_DB"] = os.path.join(_tmp, "t.db")
os.environ["CHAT_UPLOADS"] = os.path.join(_tmp, "up")


def mod(name, **kw):
    m = types.ModuleType(name); m.__dict__.update(kw); sys.modules[name] = m


class FastAPI:
    def __init__(self, **k): pass
    def _dec(self, *a, **k): return lambda f: f
    get = post = delete = put = websocket = middleware = _dec
    def mount(self, *a, **k): pass


class HTTPException(Exception):
    def __init__(self, code, detail=None): self.code, self.detail = code, detail


class Base:
    def __init__(self, **kw): self.__dict__.update(kw)


mod("fastapi", FastAPI=FastAPI, HTTPException=HTTPException, Depends=lambda f=None: None, File=lambda *a, **k: None,
    Form=lambda *a, **k: None, Header=lambda *a, **k: None, Query=lambda *a, **k: None, Request=object, UploadFile=object,
    WebSocket=object, WebSocketDisconnect=Exception)
mod("fastapi.responses", FileResponse=lambda p, **k: ("file", str(p), k))
mod("fastapi.staticfiles", StaticFiles=lambda *a, **k: None)
mod("pydantic", BaseModel=Base, Field=lambda *a, **k: None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
import main
from db import db

fake_ct = lambda: base64.b64encode(os.urandom(32)).decode()
fake_sig = lambda: base64.b64encode(os.urandom(64)).decode()

users = {}


async def reg(name):
    await main.register(main.RegisterIn(
        username=name, password="pw-" + name * 6, dh_pub=base64.b64encode(os.urandom(91)).decode(),
        sig_pub=base64.b64encode(os.urandom(91)).decode(), key_blob="{" + "x" * 30 + "}"))
    users[name] = {"id": db.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()["id"],
                   "username": name}


async def expect(code, coro):
    try:
        await coro
    except main.HTTPException as e:
        assert e.code == code, f"ожидали {code}, получили {e.code}: {e.detail}"
        return e.detail
    raise AssertionError(f"ожидали HTTP {code}")


class W:
    def __init__(self): self.o = []
    async def send_json(self, p): self.o.append(p)


async def go():
    for n in ("alice", "bob", "carol", "dave"):
        await reg(n)
    A, B, C, D = users["alice"], users["bob"], users["carol"], users["dave"]
    ws = {n: W() for n in users}
    main.manager.by_user = {users[n]["id"]: {ws[n]} for n in users}

    # ---------- создание: kind и роль автора ----------
    group = await main.create_room(main.RoomIn(name="Обычная группа", kind="group"), A)
    assert group["kind"] == "group" and group["role"] == "owner"
    channel = await main.create_room(main.RoomIn(name="Мой канал"), A)  # kind по умолчанию — group
    assert channel["kind"] == "group"
    channel = await main.create_room(main.RoomIn(name="Настоящий канал", kind="channel"), A)
    assert channel["kind"] == "channel" and channel["role"] == "owner"
    cid = channel["id"]
    for m in (B, C, D):
        await main.join_room(cid, m)
    print("CHAN: create_room с kind — правильный тип, создатель становится owner — OK")

    rooms_bob = await main.my_rooms(B)
    assert next(r for r in rooms_bob if r["id"] == cid)["role"] == "member"
    print("CHAN: присоединившийся через join — обычный участник (role=member) — OK")

    members = await main.room_members(cid, A)
    assert {m["username"]: m["role"] for m in members} == {"alice": "owner", "bob": "member", "carol": "member", "dave": "member"}
    print("CHAN: GET .../members отдаёт роли — OK")

    # ---------- в канале постить может только owner/admin ----------
    db.execute("UPDATE rooms SET key_epoch = 1 WHERE id = ?", (cid,)); db.commit()

    ok_post = main.can_post(cid, A["id"])
    bad_post = main.can_post(cid, B["id"])
    assert ok_post is True and bad_post is False
    print("CHAN: can_post — true для владельца, false для рядового участника канала — OK")

    class FakeUpload:
        async def read(self, n): return b"0" * 20
    detail = await expect(403, main.upload_media(cid, epoch=1, ct=fake_ct(), sig=fake_sig(), file=FakeUpload(), user=B))
    assert "владелец" in detail.lower() or "администратор" in detail.lower()
    await main.upload_media(cid, epoch=1, ct=fake_ct(), sig=fake_sig(), file=FakeUpload(), user=A)  # owner — можно
    print("CHAN: рядовому участнику канала upload_media отказывает (403), владельцу — можно — OK")

    await expect(403, main.schedule_message(cid, main.ScheduleIn(epoch=1, ct=fake_ct(), sig=fake_sig(),
                                                                  send_at=time.time() + 3600), B))
    await main.schedule_message(cid, main.ScheduleIn(epoch=1, ct=fake_ct(), sig=fake_sig(),
                                                     send_at=time.time() + 3600), A)
    print("CHAN: рядовому участнику канала schedule_message отказывает, владельцу — можно — OK")

    # в обычной группе (не канале) писать может кто угодно
    db.execute("UPDATE rooms SET key_epoch = 1 WHERE id = ?", (group["id"],)); db.commit()
    for m in (B, C):
        await main.join_room(group["id"], m)
    assert main.can_post(group["id"], B["id"]) is True
    print("CHAN: в обычной группе (не канале) can_post — true для всех участников — OK")

    # ---------- повышение/понижение ----------
    await expect(403, main.promote_member(cid, "dave", B))  # не владелец
    await main.promote_member(cid, "dave", A)
    assert main.role_of(cid, D["id"]) == "admin"
    await expect(404, main.promote_member(cid, "dave", A))  # уже admin, не member
    await expect(404, main.promote_member(cid, "nobody", A))
    print("CHAN: повысить может только владелец, повторно/несуществующего — 404 — OK")

    # теперь dave (admin) может постить
    assert main.can_post(cid, D["id"]) is True

    await expect(403, main.demote_member(cid, "dave", B))
    await main.demote_member(cid, "dave", A)
    assert main.role_of(cid, D["id"]) == "member"
    assert main.can_post(cid, D["id"]) is False
    print("CHAN: понизить тоже может только владелец, роль и право писать меняются вместе — OK")

    # ---------- исключение ----------
    await main.promote_member(cid, "dave", A)  # dave снова admin для проверки прав исключения
    await expect(403, main.kick_member(cid, "dave", B))  # рядовой не может исключить admin
    await expect(400, main.kick_member(cid, "alice", A))  # нельзя исключить самого себя этой ручкой
    await main.kick_member(cid, "carol", D)  # admin может исключить рядового участника
    assert main.role_of(cid, C["id"]) is None

    await main.promote_member(cid, "bob", A)  # второй admin — специально для проверки admin-vs-admin
    await expect(403, main.kick_member(cid, "bob", D))  # admin не может исключить другого admin
    await main.demote_member(cid, "bob", A)  # возвращаем как было — дальше по тесту bob снова обычный участник
    assert main.role_of(cid, B["id"]) == "member"

    await expect(403, main.kick_member(cid, "alice", D))  # admin не может исключить владельца
    await main.kick_member(cid, "dave", A)  # владелец может исключить admin
    assert main.role_of(cid, D["id"]) is None
    await expect(404, main.kick_member(cid, "carol", A))  # уже не участник
    print("CHAN: исключение — по иерархии прав (admin не трогает другого admin и владельца) — OK")

    # ---------- выход и передача владения ----------
    assert main.role_of(cid, B["id"]) == "member"
    await main.promote_member(cid, "bob", A)
    await main.leave_room(cid, A)
    assert main.role_of(cid, A["id"]) is None
    assert main.role_of(cid, B["id"]) == "owner", "владение должно перейти админу (bob), а не рядовому"
    print("CHAN: владелец вышел — владение переходит админу — OK")

    solo = await main.create_room(main.RoomIn(name="Соло", kind="group"), C)
    await main.leave_room(solo["id"], C)
    print("CHAN: последний участник выходит — не падает — OK")

    await expect(400, main.leave_room((await main.open_dm(main.DmIn(username="carol"), users["dave"]))["id"], users["dave"]))
    print("CHAN: из личного чата выйти нельзя (400) — OK")

    # ---------- переименование ----------
    ren = await main.rename_room(cid, main.RenameIn(name="Новое имя"), B)  # bob теперь owner
    assert ren["name"] == "Новое имя"
    await expect(403, main.rename_room(group["id"], main.RenameIn(name="Не выйдет"), C))  # carol — рядовой участник group
    dm = await main.open_dm(main.DmIn(username="carol"), users["dave"])
    await expect(400, main.rename_room(dm["id"], main.RenameIn(name="Нельзя"), users["dave"]))
    print("CHAN: переименование — владельцу/админу можно, рядовому и личному чату — нельзя (403/400) — OK")

    # ---------- discover отдаёт kind ----------
    disc = await main.discover(users["dave"])
    kinds = {r["name"]: r["kind"] for r in disc}
    assert kinds.get("Обычная группа") == "group"
    print("CHAN: /rooms/discover отдаёт kind — OK")

asyncio.run(go())
