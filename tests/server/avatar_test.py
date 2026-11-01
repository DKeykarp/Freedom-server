"""Аватары: main.py, FastAPI подменён заглушкой (как в остальных server-тестах)."""
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


class FakeUpload:
    def __init__(self, data: bytes): self._data = data
    async def read(self, n): return self._data[:n]


JPEG = b"\xff\xd8\xff" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


async def go():
    for n in ("alice", "bob"):
        await reg(n)
    A, B = users["alice"], users["bob"]

    # ---------- своего аватара ещё нет ----------
    p = await main.public_profile("alice", B)
    assert p["avatar"] is None
    print("AVATAR: у нового пользователя аватара нет (null) — OK")

    # ---------- не картинка — отказ, файла на диске не появляется ----------
    before = set(os.listdir(main.AVATAR_DIR)) if main.AVATAR_DIR.exists() else set()
    detail = await expect(422, main.upload_avatar(FakeUpload(b"not an image" * 20), A))
    assert "картинк" in detail.lower()
    after = set(os.listdir(main.AVATAR_DIR)) if main.AVATAR_DIR.exists() else set()
    assert after == before, "при отказе на диске не должно появляться ничего лишнего"
    print("AVATAR: не-картинка отклоняется (422), файл не создаётся — OK")

    # ---------- слишком большой файл — отказ ----------
    huge = FakeUpload(JPEG + b"\x00" * main.MAX_AVATAR_BYTES)
    await expect(413, main.upload_avatar(huge, A))
    print(f"AVATAR: файл больше {main.MAX_AVATAR_BYTES} байт отклоняется (413) — OK")

    # ---------- загрузка — появляется в профиле, файл реально на диске ----------
    r = await main.upload_avatar(FakeUpload(JPEG), A)
    assert r["avatar"].startswith("/avatars/") and r["avatar"].endswith(".jpg")
    fn1 = r["avatar"].rsplit("/", 1)[-1]
    assert (main.AVATAR_DIR / fn1).exists()
    p = await main.public_profile("alice", B)
    assert p["avatar"] == r["avatar"]
    print("AVATAR: загрузка — файл на диске, виден в публичном профиле — OK")

    # PUT /profile (смена био) не должна ни трогать, ни терять аватар в своём ответе
    upd = await main.update_profile(main.ProfileIn(bio="привет"), A)
    assert upd["avatar"] == r["avatar"], "смена био не должна влиять на аватар, и ответ должен его отдавать"
    print("AVATAR: обновление био отдаёт тот же avatar в ответе (согласованность формы ответа) — OK")

    # ---------- повторная загрузка — старый файл удаляется, остаётся только новый ----------
    r2 = await main.upload_avatar(FakeUpload(PNG), A)
    fn2 = r2["avatar"].rsplit("/", 1)[-1]
    assert fn2 != fn1 and fn2.endswith(".png")
    assert not (main.AVATAR_DIR / fn1).exists(), "старый файл аватара должен быть удалён при замене"
    assert (main.AVATAR_DIR / fn2).exists()
    print("AVATAR: повторная загрузка заменяет файл — старый удалён, новый на месте — OK")

    # ---------- раздача файла: любому авторизованному, path traversal не проходит ----------
    served = await main.get_avatar_file(fn2, B)
    assert served[1] == str(main.AVATAR_DIR / fn2)
    marker = main.AVATAR_DIR.parent / "secret.txt"
    marker.write_text("не отдавать")
    await expect(404, main.get_avatar_file("../secret.txt", B))
    marker.unlink()
    await expect(404, main.get_avatar_file("nonexistent-xxxxxxxxxxxxxxxxxxxx.jpg", B))
    print("AVATAR: файл отдаётся любому авторизованному; выход за пределы каталога и несуществующий — 404 — OK")

    # ---------- удаление аватара — очищается в БД и профиле, файл стирается с диска ----------
    d = await main.delete_avatar(A)
    assert d["avatar"] is None
    assert not (main.AVATAR_DIR / fn2).exists()
    p = await main.public_profile("alice", B)
    assert p["avatar"] is None
    print("AVATAR: удаление — файл стёрт, профиль снова без аватара — OK")

    # удаление, когда аватара и так нет — не падает
    await main.delete_avatar(A)
    print("AVATAR: повторное удаление без аватара не падает — OK")

    # ---------- аватар виден в списке участников комнаты и в списке комнат (для DM — аватар собеседника) ----------
    r3 = await main.upload_avatar(FakeUpload(JPEG), B)
    group = await main.create_room(main.RoomIn(name="группа", kind="group"), A)
    await main.join_room(group["id"], B)
    members = await main.room_members(group["id"], A)
    bob_row = next(m for m in members if m["username"] == "bob")
    assert bob_row["avatar"] == r3["avatar"]
    alice_row = next(m for m in members if m["username"] == "alice")
    assert alice_row["avatar"] is None
    print("AVATAR: GET .../members отдаёт аватар каждого участника — OK")

    dm = await main.open_dm(main.DmIn(username="bob"), A)
    rooms = main.rooms_for(A["id"], dm["id"])
    assert rooms[0]["avatar"] == r3["avatar"], "в личном чате avatar должен быть аватаром собеседника, не своим"
    print("AVATAR: в списке комнат у личного чата — аватар собеседника, а не свой — OK")

asyncio.run(go())
