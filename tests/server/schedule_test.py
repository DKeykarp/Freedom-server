"""Отложенная отправка: main.py, FastAPI подменён заглушкой (как в остальных server-тестах)."""
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
    for n in ("alice", "bob"):
        await reg(n)
    A, B = users["alice"], users["bob"]
    wa, wb = W(), W()
    main.manager.by_user = {A["id"]: {wa}, B["id"]: {wb}}

    dm = await main.open_dm(main.DmIn(username="bob"), A)
    rid = dm["id"]
    db.execute("UPDATE rooms SET key_epoch = 1 WHERE id = ?", (rid,))
    db.commit()
    wb.o.clear()  # open_dm уже разослал rooms_changed собеседнику — это не то, что проверяем здесь

    # ---------- проверки входных данных ----------
    await expect(422, main.schedule_message(rid, main.ScheduleIn(epoch=1, ct=fake_ct(), sig=fake_sig(), send_at=time.time() - 5), A))
    print("SCH: время в прошлом отклоняется — OK")

    await expect(422, main.schedule_message(rid, main.ScheduleIn(epoch=1, ct=fake_ct(), sig=fake_sig(), send_at=time.time() + main.MAX_SCHEDULE_SECONDS * 2), A))
    print("SCH: слишком далёкое будущее отклоняется — OK")

    # ---------- планирование и приватность списка ----------
    sch = await main.schedule_message(rid, main.ScheduleIn(epoch=1, ct=fake_ct(), sig=fake_sig(), send_at=time.time() + 3600), A)
    assert sch["room_id"] == rid
    listed_a = await main.list_scheduled(rid, A)
    assert len(listed_a) == 1 and listed_a[0]["id"] == sch["id"]
    listed_b = await main.list_scheduled(rid, B)
    assert listed_b == [], "чужие отложенные сообщения не видны — даже в личном чате с тем же собеседником"
    print("SCH: запланированное видно только автору, список у собеседника пуст — OK")

    # никакого сообщения в истории пока нет и никто ничего не получил
    hist = await main.history(rid, 50, None, B)
    assert hist == []
    assert wb.o == []
    print("SCH: до наступления времени в истории и по WS ничего не появляется — OK")

    # ---------- отмена ----------
    await expect(404, main.cancel_scheduled(rid, sch["id"], B))
    print("SCH: отменить чужое отложенное сообщение нельзя (даже само его существование не подтверждается) — OK")

    await main.cancel_scheduled(rid, sch["id"], A)
    assert await main.list_scheduled(rid, A) == []
    print("SCH: автор отменяет своё — пропадает из списка — OK")

    # ---------- сама отправка по наступлении времени ----------
    ct2 = fake_ct()
    sch2 = await main.schedule_message(rid, main.ScheduleIn(epoch=1, ct=ct2, sig=fake_sig(), send_at=time.time() + 3600), A)
    db.execute("UPDATE scheduled_messages SET send_at = ? WHERE id = ?", (time.time() - 1, sch2["id"]))
    db.commit()

    wa.o.clear(); wb.o.clear()
    await main.sweep_scheduled_messages()
    assert await main.list_scheduled(rid, A) == [], "после отправки пропадает из отложенных"
    hist = await main.history(rid, 50, None, B)
    assert len(hist) == 1 and hist[0]["ct"] == ct2 and hist[0]["username"] == "alice"
    assert wb.o and wb.o[-1]["type"] == "message" and wb.o[-1]["ct"] == ct2
    print("SCH: по наступлении времени появляется как обычное сообщение и рассылается по WS — OK")

    # ---------- фоновая задача не плодится и не виснет ----------
    ensure_before = main._sweep_task
    main.ensure_scheduler_running()
    main.ensure_scheduler_running()
    assert main._sweep_task is not None and main._sweep_task is ensure_before, "повторный вызов не должен пересоздавать задачу"
    main._sweep_task.cancel()
    try:
        await main._sweep_task
    except asyncio.CancelledError:
        pass
    print("SCH: повторный запуск планировщика не плодит задачи; аккуратно остановлена — OK")

asyncio.run(go())
