"""Реакции, закреплённые сообщения, самоудаление: main.py, FastAPI подменён заглушкой (как в остальных
server-тестах)."""
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


async def expect(code, coro):
    try:
        await coro
    except main.HTTPException as e:
        assert e.code == code, f"ожидали {code}, получили {e.code}: {e.detail}"
        return e.detail
    raise AssertionError(f"ожидали HTTP {code}")


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
    msg = await main.post_message(A, rid, epoch=1, ct=fake_ct(), sig=fake_sig())
    mid = msg["id"]
    assert msg["reactions"] == [] and msg["pinned"] is False
    print("SOC: у нового сообщения пустые reactions и pinned=false — OK")

    # ---------- реакции ----------
    await expect(404, main.set_reaction(rid, 999999, main.ReactionIn(epoch=1, ct=fake_ct(), sig=fake_sig()), B))

    wa.o.clear()
    r1 = await main.set_reaction(rid, mid, main.ReactionIn(epoch=1, ct=fake_ct(), sig=fake_sig()), B)
    assert r1["type"] == "reaction_set" and r1["username"] == "bob"
    assert wa.o and wa.o[-1]["type"] == "reaction_set"
    hist = await main.history(rid, 50, None, A)
    got = next(m for m in hist if m["id"] == mid)
    assert len(got["reactions"]) == 1 and got["reactions"][0]["username"] == "bob"
    print("SOC: реакция появляется в истории и рассылается собеседнику — OK")

    new_ct = fake_ct()
    await main.set_reaction(rid, mid, main.ReactionIn(epoch=1, ct=new_ct, sig=fake_sig()), B)
    hist = await main.history(rid, 50, None, A)
    got = next(m for m in hist if m["id"] == mid)
    assert len(got["reactions"]) == 1 and got["reactions"][0]["ct"] == new_ct
    print("SOC: повторная реакция того же человека заменяет прежнюю, не дублирует — OK")

    await main.set_reaction(rid, mid, main.ReactionIn(epoch=1, ct=fake_ct(), sig=fake_sig()), A)
    hist = await main.history(rid, 50, None, A)
    got = next(m for m in hist if m["id"] == mid)
    assert len(got["reactions"]) == 2
    print("SOC: разные люди — разные реакции, обе видны — OK")

    wb.o.clear()
    r2 = await main.remove_reaction(rid, mid, B)
    assert r2["type"] == "reaction_removed"
    assert wb.o and wb.o[-1]["type"] == "reaction_removed"
    hist = await main.history(rid, 50, None, A)
    got = next(m for m in hist if m["id"] == mid)
    assert len(got["reactions"]) == 1 and got["reactions"][0]["username"] == "alice"
    print("SOC: снятие реакции убирает только свою, рассылается — OK")

    # ---------- закреплённые сообщения ----------
    await expect(404, main.pin_message(rid, 999999, A))

    wb.o.clear()
    p1 = await main.pin_message(rid, mid, B)
    assert p1["type"] == "message_pinned" and p1["pinned_by"] == "bob"
    assert wb.o and wb.o[-1]["type"] == "message_pinned"
    pins = await main.list_pins(rid, A)
    assert len(pins) == 1 and pins[0]["id"] == mid
    hist = await main.history(rid, 50, None, A)
    assert next(m for m in hist if m["id"] == mid)["pinned"] is True
    print("SOC: закрепить может любой участник (нет ролей), видно в /pins и в истории — OK")

    await main.unpin_message(rid, mid, A)
    pins = await main.list_pins(rid, A)
    assert pins == []
    hist = await main.history(rid, 50, None, A)
    assert next(m for m in hist if m["id"] == mid)["pinned"] is False
    print("SOC: открепить тоже может любой участник — OK")

    # удаление «для всех» подчищает и реакции, и закрепление
    await main.set_reaction(rid, mid, main.ReactionIn(epoch=1, ct=fake_ct(), sig=fake_sig()), B)
    await main.pin_message(rid, mid, B)
    await main.delete_message(rid, mid, True, A)
    assert db.execute("SELECT 1 FROM reactions WHERE message_id = ?", (mid,)).fetchone() is None
    assert db.execute("SELECT 1 FROM pinned_messages WHERE message_id = ?", (mid,)).fetchone() is None
    print("SOC: «удалить у всех» подчищает реакции и закрепление осиротевшего сообщения — OK")

    # ---------- самоудаление ----------
    wa.o.clear(); wb.o.clear()
    ad = await main.set_autodelete(rid, main.AutodeleteIn(seconds=3600), A)
    assert ad == {"type": "autodelete_changed", "room_id": rid, "seconds": 3600}
    assert wb.o and wb.o[-1] == ad
    rooms = main.rooms_for(A["id"])
    assert next(r for r in rooms if r["id"] == rid)["autodelete_seconds"] == 3600
    print("SOC: включение самоудаления рассылается и видно в списке комнат — OK")

    fresh = await main.post_message(A, rid, epoch=1, ct=fake_ct(), sig=fake_sig())
    old = await main.post_message(B, rid, epoch=1, ct=fake_ct(), sig=fake_sig())
    db.execute("UPDATE messages SET ts = ? WHERE id = ?", (time.time() - 7200, old["id"]))
    db.commit()
    db.execute("INSERT OR IGNORE INTO hidden_messages(user_id, message_id) VALUES(?, ?)", (A["id"], old["id"]))
    db.commit()

    wa.o.clear(); wb.o.clear()
    await main.history(rid, 50, None, A)  # чтение истории и запускает подметание просроченных сообщений
    assert db.execute("SELECT 1 FROM messages WHERE id = ?", (old["id"],)).fetchone() is None
    assert db.execute("SELECT 1 FROM messages WHERE id = ?", (fresh["id"],)).fetchone() is not None
    assert db.execute("SELECT 1 FROM hidden_messages WHERE message_id = ?", (old["id"],)).fetchone() is None
    assert any(p["type"] == "message_deleted" and p["id"] == old["id"] for p in wb.o)
    print("SOC: просроченное сообщение стирается при чтении истории (и связанные записи), свежее остаётся — OK")

    await main.set_autodelete(rid, main.AutodeleteIn(seconds=None), A)
    untouched = await main.post_message(A, rid, epoch=1, ct=fake_ct(), sig=fake_sig())
    db.execute("UPDATE messages SET ts = ? WHERE id = ?", (time.time() - 999999, untouched["id"]))
    db.commit()
    await main.history(rid, 50, None, A)
    assert db.execute("SELECT 1 FROM messages WHERE id = ?", (untouched["id"],)).fetchone() is not None
    print("SOC: выключенное самоудаление (seconds=null) ничего не трогает, даже старые сообщения — OK")

asyncio.run(go())
