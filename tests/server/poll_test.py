"""Опросы (голоса): main.py, FastAPI подменён заглушкой (как в остальных server-тестах)."""
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
    for n in ("alice", "bob", "carol"):
        await reg(n)
    A, B, C = users["alice"], users["bob"], users["carol"]
    wa, wb, wc = W(), W(), W()
    main.manager.by_user = {A["id"]: {wa}, B["id"]: {wb}, C["id"]: {wc}}

    room = await main.create_room(main.RoomIn(name="Опрос-тест"), A)
    rid = room["id"]
    await main.join_room(rid, B)
    await main.join_room(rid, C)
    db.execute("UPDATE rooms SET key_epoch = 1 WHERE id = ?", (rid,))
    db.commit()

    # опрос — просто сообщение (payload с t:"poll" собирает клиент, сервер об этом ничего не знает)
    poll = await main.post_message(A, rid, epoch=1, ct=fake_ct(), sig=fake_sig())
    pid = poll["id"]
    assert poll["votes"] == []
    print("POLL: у нового сообщения (в т.ч. опроса) пустые votes — OK")

    await expect(404, main.set_vote(rid, 999999, main.VoteIn(epoch=1, ct=fake_ct(), sig=fake_sig()), B))
    print("POLL: голос за несуществующее сообщение — 404 — OK")

    wa.o.clear()
    r1 = await main.set_vote(rid, pid, main.VoteIn(epoch=1, ct=fake_ct(), sig=fake_sig()), B)
    assert r1["type"] == "vote_set" and r1["username"] == "bob"
    assert wa.o and wa.o[-1]["type"] == "vote_set"
    hist = await main.history(rid, 50, None, A)
    got = next(m for m in hist if m["id"] == pid)
    assert len(got["votes"]) == 1 and got["votes"][0]["username"] == "bob"
    print("POLL: голос появляется в истории и рассылается остальным — OK")

    new_ct = fake_ct()
    await main.set_vote(rid, pid, main.VoteIn(epoch=1, ct=new_ct, sig=fake_sig()), B)
    hist = await main.history(rid, 50, None, A)
    got = next(m for m in hist if m["id"] == pid)
    assert len(got["votes"]) == 1 and got["votes"][0]["ct"] == new_ct
    print("POLL: переголосование заменяет прежний выбор, не дублирует — OK")

    await main.set_vote(rid, pid, main.VoteIn(epoch=1, ct=fake_ct(), sig=fake_sig()), C)
    hist = await main.history(rid, 50, None, A)
    got = next(m for m in hist if m["id"] == pid)
    assert len(got["votes"]) == 2
    print("POLL: разные люди — разные голоса, оба видны — OK")

    wb.o.clear()
    r2 = await main.remove_vote(rid, pid, C)
    assert r2["type"] == "vote_removed"
    assert wb.o and wb.o[-1]["type"] == "vote_removed"
    hist = await main.history(rid, 50, None, A)
    got = next(m for m in hist if m["id"] == pid)
    assert len(got["votes"]) == 1 and got["votes"][0]["username"] == "bob"
    print("POLL: отзыв голоса убирает только свой, рассылается — OK")

    # удаление «для всех» подчищает голоса осиротевшего сообщения
    await main.set_vote(rid, pid, main.VoteIn(epoch=1, ct=fake_ct(), sig=fake_sig()), C)
    await main.delete_message(rid, pid, True, A)
    assert db.execute("SELECT 1 FROM poll_votes WHERE message_id = ?", (pid,)).fetchone() is None
    print("POLL: «удалить у всех» подчищает голоса — OK")

    # самоудаление тоже подчищает голоса просроченного опроса
    poll2 = await main.post_message(A, rid, epoch=1, ct=fake_ct(), sig=fake_sig())
    await main.set_vote(rid, poll2["id"], main.VoteIn(epoch=1, ct=fake_ct(), sig=fake_sig()), B)
    db.execute("UPDATE messages SET ts = ? WHERE id = ?", (time.time() - 7200, poll2["id"]))
    db.commit()
    await main.set_autodelete(rid, main.AutodeleteIn(seconds=3600), A)
    await main.history(rid, 50, None, A)
    assert db.execute("SELECT 1 FROM messages WHERE id = ?", (poll2["id"],)).fetchone() is None
    assert db.execute("SELECT 1 FROM poll_votes WHERE message_id = ?", (poll2["id"],)).fetchone() is None
    print("POLL: самоудаление просроченного опроса подчищает и его голоса — OK")

asyncio.run(go())
