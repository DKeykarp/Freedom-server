"""Редактирование и удаление сообщений: main.py, FastAPI подменён заглушкой (как в остальных server-тестах)."""
import asyncio
import base64
import os
import sys
import types

import tempfile
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
    r = await main.register(main.RegisterIn(
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
    # неважно, что ключ комнаты никто по-настоящему не создавал — check_e2e требует лишь совпадения epoch,
    # а сама расшифровка серверу недоступна и не нужна для этих тестов
    db.execute("UPDATE rooms SET key_epoch = 1 WHERE id = ?", (rid,))
    db.commit()

    sent = await main.post_message(A, rid, epoch=1, ct=fake_ct(), sig=fake_sig())
    mid = sent["id"]
    assert sent["edited_ts"] is None
    print("SRV: у нового сообщения edited_ts = null — OK")

    # ---- редактирование ----
    await expect(403, main.edit_message(rid, mid, main.EditIn(epoch=1, ct=fake_ct(), sig=fake_sig()), B))
    await expect(404, main.edit_message(rid, 999999, main.EditIn(epoch=1, ct=fake_ct(), sig=fake_sig()), A))
    await expect(422, main.edit_message(rid, mid, main.EditIn(epoch=1, ct="!!!", sig=fake_sig()), A))
    await expect(409, main.edit_message(rid, mid, main.EditIn(epoch=2, ct=fake_ct(), sig=fake_sig()), A))
    print("SRV: редактировать чужое — 403; несуществующее — 404; кривая форма — 422; неверная эпоха — 409 — OK")

    new_ct, new_sig = fake_ct(), fake_sig()
    wb.o.clear()
    r = await main.edit_message(rid, mid, main.EditIn(epoch=1, ct=new_ct, sig=new_sig), A)
    assert r["type"] == "message_edited" and r["ct"] == new_ct and r["edited_ts"] is not None
    assert wb.o and wb.o[-1]["type"] == "message_edited" and wb.o[-1]["ct"] == new_ct
    row = db.execute("SELECT ct, edited_ts FROM messages WHERE id = ?", (mid,)).fetchone()
    assert row["ct"] == new_ct and row["edited_ts"] is not None
    hist = await main.history(rid, 50, None, A)
    assert hist[-1]["ct"] == new_ct and hist[-1]["edited_ts"] is not None
    print("SRV: редактирование меняет ct и edited_ts, рассылает message_edited, видно в истории — OK")

    call_sent = await main.post_message(A, rid, kind="call", text="Аудиозвонок · 00:05")
    await expect(403, main.edit_message(rid, call_sent["id"], main.EditIn(epoch=1, ct=fake_ct(), sig=fake_sig()), A))
    await expect(403, main.delete_message(rid, call_sent["id"], True, A))
    print("SRV: итог звонка (kind=call) нельзя ни редактировать, ни удалить «для всех» — OK")

    # ---- удаление «для себя» ----
    msg2 = await main.post_message(B, rid, epoch=1, ct=fake_ct(), sig=fake_sig())
    r = await main.delete_message(rid, msg2["id"], False, A)
    assert r == {"ok": True}
    hist_a = await main.history(rid, 50, None, A)
    hist_b = await main.history(rid, 50, None, B)
    assert msg2["id"] not in [m["id"] for m in hist_a]
    assert msg2["id"] in [m["id"] for m in hist_b]
    await expect(404, main.delete_message(rid, 999999, False, A))
    print("SRV: «удалить для себя» прячет сообщение только у инициатора, у остальных остаётся — OK")

    # ---- удаление «для всех» ----
    msg3 = await main.post_message(A, rid, epoch=1, ct=fake_ct(), sig=fake_sig())
    await expect(403, main.delete_message(rid, msg3["id"], True, B))
    wb.o.clear(); wc.o.clear()
    r = await main.delete_message(rid, msg3["id"], True, A)
    assert r == {"ok": True}
    assert db.execute("SELECT 1 FROM messages WHERE id = ?", (msg3["id"],)).fetchone() is None
    assert wb.o[-1] == {"type": "message_deleted", "id": msg3["id"], "room_id": rid}
    hist_b2 = await main.history(rid, 50, None, B)
    assert msg3["id"] not in [m["id"] for m in hist_b2]
    print("SRV: «удалить для всех» — только автор, строка стирается из БД, рассылается message_deleted — OK")

    # файл удаляется при «удалить для всех»
    (main.UPLOAD_DIR / "f1.bin").write_bytes(b"secret-file-bytes")
    msg4 = await main.post_message(A, rid, epoch=1, ct=fake_ct(), sig=fake_sig(), media="f1.bin")
    assert (main.UPLOAD_DIR / "f1.bin").exists()
    await main.delete_message(rid, msg4["id"], True, A)
    assert not (main.UPLOAD_DIR / "f1.bin").exists()
    print("SRV: «удалить для всех» стирает и зашифрованный файл вложения — OK")

    # скрытая «для себя» запись не мешает удалить «для всех» позже (и не остаётся мусором в hidden_messages)
    msg5 = await main.post_message(B, rid, epoch=1, ct=fake_ct(), sig=fake_sig())
    await main.delete_message(rid, msg5["id"], False, A)
    await main.delete_message(rid, msg5["id"], True, B)
    assert db.execute("SELECT 1 FROM hidden_messages WHERE message_id = ?", (msg5["id"],)).fetchone() is None
    print("SRV: удаление «для всех» подчищает и связанные записи hidden_messages — OK")

asyncio.run(go())
