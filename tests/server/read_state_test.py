"""Галочки «доставлено/прочитано»: main.py, FastAPI подменён заглушкой (как в остальных server-тестах)."""
import asyncio
import base64
import os
import sys
import tempfile
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


class W:
    def __init__(self): self.o = []
    async def send_json(self, p): self.o.append(p)


async def go():
    for n in ("alice", "bob", "carol"):
        await reg(n)
    A, B, C = users["alice"], users["bob"], users["carol"]
    wa, wb, wc = W(), W(), W()
    main.manager.by_user = {A["id"]: {wa}, B["id"]: {wb}, C["id"]: {wc}}

    dm = await main.open_dm(main.DmIn(username="bob"), A)
    rid = dm["id"]
    db.execute("UPDATE rooms SET key_epoch = 1 WHERE id = ?", (rid,))
    db.commit()

    rs = await main.read_state(rid, A)
    assert rs == [{"username": "bob", "last_read_id": 0}], rs
    print("READ: изначально last_read_id=0 у собеседника — OK")

    m1 = await main.post_message(A, rid, epoch=1, ct=fake_ct(), sig=fake_sig())
    m2 = await main.post_message(A, rid, epoch=1, ct=fake_ct(), sig=fake_sig())

    wa.o.clear()
    await main.mark_read(rid, B["id"], "bob", m1["id"])
    assert wa.o and wa.o[-1] == {"type": "read_state", "room_id": rid, "username": "bob", "message_id": m1["id"]}
    rs = await main.read_state(rid, A)
    assert rs == [{"username": "bob", "last_read_id": m1["id"]}]
    print("READ: mark_read двигает last_read_id и рассылает остальным (но не себе) — OK")

    # назад не откатывается (MAX), а вперёд — двигается дальше
    await main.mark_read(rid, B["id"], "bob", 0)
    rs = await main.read_state(rid, A)
    assert rs[0]["last_read_id"] == m1["id"], "не должно откатиться назад при более раннем message_id"
    await main.mark_read(rid, B["id"], "bob", m2["id"])
    rs = await main.read_state(rid, A)
    assert rs[0]["last_read_id"] == m2["id"]
    print("READ: last_read_id только растёт (MAX), не откатывается — OK")

    # групповая комната: read_state отдаёт всех, кроме себя, и не путает с ЛС
    room2 = await main.create_room(main.RoomIn(name="Команда"), A)
    await main.join_room(room2["id"], B)
    await main.join_room(room2["id"], C)
    rs = await main.read_state(room2["id"], A)
    assert {r["username"] for r in rs} == {"bob", "carol"}
    assert all(r["last_read_id"] == 0 for r in rs)
    print("READ: в групповой комнате видно всех участников, кроме себя — OK")

    wb.o.clear(); wc.o.clear()
    m3 = await main.post_message(A, room2["id"], epoch=1, ct=fake_ct(), sig=fake_sig())
    wb.o.clear(); wc.o.clear()   # это разослал сам post_message — очищаем ещё раз перед проверкой именно mark_read
    await main.mark_read(room2["id"], B["id"], "bob", m3["id"])
    assert not wb.o, "себе (инициатору) рассылать не нужно"
    assert wc.o and wc.o[-1]["username"] == "bob"
    print("READ: в группе видят все остальные, включая тех, кто ещё не прочитал сам — OK")

    # без общей комнаты — 404 (та же приватность, что и у ключей/удаления)
    try:
        await main.read_state(999999, A)
        raise AssertionError("ожидали HTTPException")
    except main.HTTPException as e:
        assert e.code == 403
    print("READ: без доступа к комнате — отказ, как и у остальных ручек — OK")

asyncio.run(go())
