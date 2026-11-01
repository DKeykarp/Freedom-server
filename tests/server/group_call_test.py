"""Групповые звонки (сигналинг): main.py, FastAPI подменён заглушкой. RING_TIMEOUT/RECONNECT_GRACE
сведены к почти нулю — реального ожидания в тесте нет."""
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

main.RING_TIMEOUT = 0.02
main.RECONNECT_GRACE = 0.02

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


class Err(Exception):
    def __init__(self, msg): self.msg = msg


async def expect_err(coro):
    try:
        await coro
    except Err:
        return
    raise AssertionError("ожидали ошибку (error()), её не было")


async def go():
    for n in ("alice", "bob", "carol", "dave"):
        await reg(n)
    A, B, C, D = users["alice"], users["bob"], users["carol"], users["dave"]
    ws = {n: W() for n in users}
    main.manager.by_user = {users[n]["id"]: {ws[n]} for n in users}

    async def err(msg):
        raise Err(msg)

    room = await main.create_room(main.RoomIn(name="Команда"), A)
    rid = room["id"]
    for m in (B, C, D):
        await main.join_room(rid, m)
    for w in ws.values():
        w.o.clear()

    # ---------- базовый 1:1-подобный сценарий (двое в личном чате) продолжает работать ----------
    dm = await main.open_dm(main.DmIn(username="bob"), A)
    drid = dm["id"]
    for w in ws.values():
        w.o.clear()
    await main.handle_call(A, "call_invite", {"room_id": drid, "call_id": "c-11", "video": False}, err)
    assert ws["bob"].o[-1]["type"] == "call_invite" and ws["bob"].o[-1]["group"] is False
    await main.handle_call(B, "call_accept", {"call_id": "c-11"}, err)
    assert ws["alice"].o[-1] == {"type": "call_peer_joined", "call_id": "c-11", "user_id": B["id"], "username": "bob"}
    assert ws["bob"].o[-1]["type"] == "call_joined" and ws["bob"].o[-1]["participants"] == [{"id": A["id"], "username": "alice"}]
    await main.handle_call(A, "call_signal", {"call_id": "c-11", "to": B["id"], "data": {"n": "N", "c": "C"}}, err)
    assert ws["bob"].o[-1]["type"] == "call_signal" and ws["bob"].o[-1]["from_id"] == A["id"]
    assert "c-11" in main.calls
    await main.handle_call(A, "call_end", {"call_id": "c-11"}, err)
    assert "c-11" not in main.calls
    print("GCALL: обычный звонок вдвоём (личный чат) работает как раньше, group=false — OK")

    # ---------- групповой звонок: приглашение всех, кто-то принимает, кто-то отклоняет ----------
    for w in ws.values():
        w.o.clear()
    await main.handle_call(A, "call_invite", {"room_id": rid, "call_id": "g-1", "video": True}, err)
    for n in ("bob", "carol", "dave"):
        assert ws[n].o[-1]["type"] == "call_invite" and ws[n].o[-1]["group"] is True
    c = main.calls["g-1"]
    assert set(c.invited) == {B["id"], C["id"], D["id"]} and set(c.participants) == {A["id"]}
    print("GCALL: группового звонка приглашены все участники комнаты, кроме инициатора — OK")

    await main.handle_call(B, "call_accept", {"call_id": "g-1"}, err)
    assert set(c.participants) == {A["id"], B["id"]}
    assert ws["carol"].o[-1]["type"] != "call_peer_joined" and ws["dave"].o[-1]["type"] != "call_peer_joined", \
        "тех, кто ещё не ответил, не должно быть в рассылке call_peer_joined"
    joined_msg = ws["bob"].o[-1]
    assert joined_msg["type"] == "call_joined" and joined_msg["participants"] == [{"id": A["id"], "username": "alice"}]

    await main.handle_call(C, "call_reject", {"call_id": "g-1"}, err)
    assert C["id"] not in c.invited and "g-1" in main.calls, "отказ одного не должен завершать звонок для остальных"
    print("GCALL: один принял, другой отклонил — звонок продолжается для принявших — OK")

    await main.handle_call(D, "call_accept", {"call_id": "g-1"}, err)
    assert set(c.participants) == {A["id"], B["id"], D["id"]}
    dave_joined = ws["dave"].o[-1]
    assert sorted(p["username"] for p in dave_joined["participants"]) == ["alice", "bob"]
    print("GCALL: третий участник видит уже присоединившихся (без отклонившего) — OK")

    # ---------- сигнал адресуется конкретному участнику, не всем ----------
    ws["alice"].o.clear(); ws["bob"].o.clear(); ws["dave"].o.clear()
    await main.handle_call(A, "call_signal", {"call_id": "g-1", "to": D["id"], "data": {"n": "n1", "c": "c1"}}, err)
    assert ws["dave"].o and ws["dave"].o[-1]["type"] == "call_signal"
    assert ws["bob"].o == [], "сигнал должен уйти только адресату, не всем участникам"
    print("GCALL: call_signal адресован конкретному участнику (to), остальные не получают — OK")

    # нельзя послать сигнал тому, кого нет в звонке
    ws["alice"].o.clear(); ws["carol"].o.clear()
    await main.handle_call(A, "call_signal", {"call_id": "g-1", "to": C["id"], "data": {"n": "n", "c": "c"}}, err)
    assert ws["carol"].o == [], "carol отклонила приглашение — сигналы ей больше не должны идти"

    # ---------- предпоследний выходит — вдвоём (сумма участников и ещё звонящих) звонок пока держится ----------
    ws["bob"].o.clear(); ws["dave"].o.clear()
    await main.handle_call(B, "call_end", {"call_id": "g-1"}, err)
    assert "g-1" in main.calls, "звонок не должен завершиться, пока остались хотя бы двое"
    assert set(c.participants) == {A["id"], D["id"]}
    assert ws["dave"].o[-1] == {"type": "call_peer_left", "call_id": "g-1", "user_id": B["id"]}
    print("GCALL: один участник вышел — пока остались хотя бы двое, звонок продолжается (call_peer_left) — OK")

    # ---------- уже занят другим звонком — не приглашается повторно ----------
    await expect_err(main.handle_call(D, "call_invite", {"room_id": drid, "call_id": "x", "video": False}, err))
    print("GCALL: нельзя начать второй звонок, уже будучи в одном — OK")

    # ---------- с двоих до одного — звонок завершается сам (разговаривать больше не с кем) ----------
    n_msgs_before = len(await main.history(rid, 50, None, A))
    ws["alice"].o.clear()
    await main.handle_call(D, "call_end", {"call_id": "g-1"}, err)
    assert "g-1" not in main.calls, "когда остался только один (alice) — звонок должен завершиться сам"
    hist = await main.history(rid, 50, None, A)
    assert len(hist) == n_msgs_before + 1 and hist[-1]["kind"] == "call"
    print("GCALL: когда остаётся один — звонок завершается сам, итог пишется в чат — OK")

    # ---------- никто не ответил вовремя — звонок завершается сам (no_answer) ----------
    for w in ws.values():
        w.o.clear()
    await main.handle_call(A, "call_invite", {"room_id": rid, "call_id": "g-2", "video": False}, err)
    await asyncio.sleep(0.15)   # ждём срабатывания всех индивидуальных ring-таймеров (RING_TIMEOUT=0.02)
    assert "g-2" not in main.calls, "если никто не ответил — звонок должен завершиться сам"
    hist = await main.history(rid, 50, None, A)
    assert "Пропущенный" in hist[-1]["text"]
    print("GCALL: если никто не ответил вовремя — звонок завершается сам, «Пропущенный» — OK")

    # ---------- лимит участников ----------
    many = [f"user{i}" for i in range(main.MAX_CALL_PARTICIPANTS + 3)]
    for n in many:
        await reg(n)
    big_room = await main.create_room(main.RoomIn(name="Большая"), A)
    for n in many:
        await main.join_room(big_room["id"], users[n])
    await main.handle_call(A, "call_invite", {"room_id": big_room["id"], "call_id": "g-3", "video": False}, err)
    c3 = main.calls["g-3"]
    assert len(c3.invited) + len(c3.participants) <= main.MAX_CALL_PARTICIPANTS
    await main.handle_call(A, "call_end", {"call_id": "g-3"}, err)
    print(f"GCALL: число участников не превышает MAX_CALL_PARTICIPANTS={main.MAX_CALL_PARTICIPANTS} — OK")

asyncio.run(go())
