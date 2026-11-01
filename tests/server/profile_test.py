"""Публичный профиль (био): main.py, FastAPI подменён заглушкой (как в остальных server-тестах)."""
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
    users[name] = {"id": main.db.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()["id"],
                   "username": name}


async def go():
    for n in ("alice", "bob"):
        await reg(n)
    A, B = users["alice"], users["bob"]

    r = await main.public_profile("alice", A)
    assert r == {"username": "alice", "bio": "", "avatar": None}, r
    print("PROFILE: у нового пользователя био пустое — OK")

    r = await main.update_profile(main.ProfileIn(bio="  Люблю E2EE и велосипед  "), A)
    assert r == {"username": "alice", "bio": "Люблю E2EE и велосипед", "avatar": None}, r
    print("PROFILE: обновление обрезает пробелы по краям, возвращает новое значение — OK")

    r = await main.public_profile("alice", B)
    assert r == {"username": "alice", "bio": "Люблю E2EE и велосипед", "avatar": None}, r
    print("PROFILE: видно другому пользователю, БЕЗ общей комнаты — публичный профиль так и задуман — OK")

    await expect(404, main.public_profile("no-such-user", A))
    print("PROFILE: несуществующее имя — 404 — OK")

    r = await main.update_profile(main.ProfileIn(bio=""), A)
    assert r["bio"] == ""
    assert (await main.public_profile("alice", B))["bio"] == ""
    print("PROFILE: можно очистить био — OK")

    # ключи (в отличие от био) по-прежнему требуют общий контекст — заводим настоящую общую комнату явно
    # (раньше для этого хватало автоматического общего чата «Общий», которого теперь нет, см. §13 в README);
    # важно, что сам эндпоинт вообще не тронут этой правкой и продолжает возвращать ключи как раньше
    shared = await main.create_room(main.RoomIn(name="общая", kind="group"), A)
    await main.join_room(shared["id"], B)
    r = await main.user_keys("alice", B)
    assert r["username"] == "alice" and "dh_pub" in r
    print("PROFILE: /users/{u}/keys не задет этой правкой (ключи видны при общей комнате) — OK")

    # искусственно уберём bob из всех общих комнат с alice (напрямую в БД, в обход API) — так проверим
    # именно то различие в приватности, ради которого и заведён отдельный эндпоинт публичного профиля
    main.db.execute("DELETE FROM room_members WHERE user_id = ?", (B["id"],))
    main.db.commit()
    await expect(404, main.user_keys("alice", B))
    r = await main.public_profile("alice", B)
    assert r == {"username": "alice", "bio": "", "avatar": None}, r
    print("PROFILE: без единой общей комнаты ключи недоступны (404), а публичный профиль — всё равно виден — OK")

    b_bio_untouched = await main.public_profile("bob", A)
    assert b_bio_untouched == {"username": "bob", "bio": "", "avatar": None}
    print("PROFILE: изменение чужого био невозможно — у bob оно как было, пустое — OK")

asyncio.run(go())
