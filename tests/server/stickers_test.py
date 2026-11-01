"""Каталог стикеров: main.py, FastAPI подменён заглушкой (как в остальных server-тестах)."""
import asyncio
import os
import sys
import tempfile
import time
import types

_tmp = tempfile.mkdtemp()
os.environ["CHAT_DB"] = os.path.join(_tmp, "t.db")
os.environ["CHAT_UPLOADS"] = os.path.join(_tmp, "up")
os.environ["CHAT_STICKERS"] = os.path.join(_tmp, "stickers")


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
    import base64
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
WEBP = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 200


async def go():
    for n in ("alice", "bob"):
        await reg(n)
    A, B = users["alice"], users["bob"]

    # ---------- создание и поиск ----------
    p1 = await main.create_pack(main.PackIn(name="Грустные коты"), A)
    p2 = await main.create_pack(main.PackIn(name="Весёлые коты"), A)
    p3 = await main.create_pack(main.PackIn(name="Погода"), B)
    assert p1["sticker_count"] == 0 and p1["creator_username"] == "alice"

    found = await main.search_packs("коты", 30, None, A)
    assert sorted(f["name"] for f in found) == ["Весёлые коты", "Грустные коты"]
    found_all = await main.search_packs("", 30, None, A)
    assert len(found_all) == 3
    found_none = await main.search_packs("собаки", 30, None, A)
    assert found_none == []
    print("STICKERS: создание паков и поиск по подстроке названия — OK")

    # спецсимволы LIKE не ломают поиск и не находят лишнего
    await main.create_pack(main.PackIn(name="100% смеха"), A)
    found_pct = await main.search_packs("100%", 30, None, A)
    assert len(found_pct) == 1 and found_pct[0]["name"] == "100% смеха"
    # "_" в LIKE без экранирования значит «любой один символ» — без экранирования "a_b" нашёл бы и "axb"
    await main.create_pack(main.PackIn(name="a_b"), A)
    await main.create_pack(main.PackIn(name="axb"), A)
    found_underscore = await main.search_packs("a_b", 30, None, A)
    assert [f["name"] for f in found_underscore] == ["a_b"], "без экранирования '_' нашёл бы ещё и 'axb'"
    print("STICKERS: спецсимволы % и _ в поисковом запросе экранируются — OK")

    # ---------- добавление стикеров: только автор, только картинки ----------
    await expect(403, main.add_sticker(p1["id"], FakeUpload(JPEG), B))
    print("STICKERS: чужой пак — добавить стикер нельзя (403) — OK")

    bad = await expect(422, main.add_sticker(p1["id"], FakeUpload(b"not an image, just text" * 20), A))
    assert "картинк" in bad.lower()
    print("STICKERS: не-картинка отклоняется с понятным сообщением — OK")

    s1 = await main.add_sticker(p1["id"], FakeUpload(JPEG), A)
    s2 = await main.add_sticker(p1["id"], FakeUpload(PNG), A)
    s3 = await main.add_sticker(p1["id"], FakeUpload(WEBP), A)
    assert s1["url"].endswith(".jpg") and s2["url"].endswith(".png") and s3["url"].endswith(".webp")
    detail = await main.get_pack(p1["id"], A)
    assert detail["sticker_count"] == 3 and len(detail["stickers"]) == 3
    print("STICKERS: JPEG/PNG/WEBP принимаются и различаются по расширению файла — OK")

    # файлы реально лежат на диске
    for s in detail["stickers"]:
        fn = s["url"].rsplit("/", 1)[-1]
        assert (main.STICKER_DIR / fn).exists()
    print("STICKERS: файлы стикеров действительно сохранены на диске — OK")

    # ---------- установка/список установленных ----------
    assert await main.installed_packs(B) == []
    await main.install_pack(p1["id"], B)
    installed = await main.installed_packs(B)
    assert len(installed) == 1 and installed[0]["id"] == p1["id"] and len(installed[0]["stickers"]) == 3
    detail_for_b = await main.get_pack(p1["id"], B)
    assert detail_for_b["installed"] is True
    print("STICKERS: установка пака — появляется в /installed со своими стикерами — OK")

    await main.uninstall_pack(p1["id"], B)
    assert await main.installed_packs(B) == []
    print("STICKERS: отмена установки убирает пак из списка — OK")

    # ---------- удаление ----------
    await expect(403, main.delete_sticker(p1["id"], s1["id"], B))
    await expect(404, main.delete_sticker(p1["id"], 999999, A))
    await main.delete_sticker(p1["id"], s1["id"], A)
    assert (await main.get_pack(p1["id"], A))["sticker_count"] == 2
    print("STICKERS: удалить стикер может только автор пака — OK")

    await expect(403, main.delete_pack(p1["id"], B))
    fn_left = [s["url"].rsplit("/", 1)[-1] for s in (await main.get_pack(p1["id"], A))["stickers"]]
    await main.install_pack(p1["id"], B)
    await main.delete_pack(p1["id"], A)
    await expect(404, main.get_pack(p1["id"], A))
    assert await main.installed_packs(B) == [], "удалённый пак должен пропасть и из чужого списка установленных"
    for fn in fn_left:
        assert not (main.STICKER_DIR / fn).exists(), "файлы удалённого пака должны быть стёрты с диска"
    print("STICKERS: удаление пака целиком — подчищает файлы и чужие installed_packs — OK")

    # ---------- раздача файлов: только авторизованным, без привязки к комнате ----------
    remaining = await main.get_pack(p2["id"], A)
    fn2 = remaining["url"].rsplit("/", 1)[-1] if "url" in remaining else None
    s4 = await main.add_sticker(p2["id"], FakeUpload(JPEG), A)
    fn = s4["url"].rsplit("/", 1)[-1]
    served = await main.get_sticker_file(fn, B)   # bob — не автор и не устанавливал пак, но всё равно видит файл
    assert served[1] == str(main.STICKER_DIR / fn)
    await expect(404, main.get_sticker_file("../../etc/passwd", A))
    await expect(404, main.get_sticker_file("nonexistent-file-xxxxxxxxxxxxxxxx.jpg", A))
    # по-настоящему проверяем защиту от выхода за пределы каталога стикеров: кладём файл-маркер СНАРУЖИ
    # STICKER_DIR и убеждаемся, что путь с traversal его не достаёт — не потому что файла нет, а потому что
    # само имя не проходит регэксп (без regex File.exists() бы его нашёл и отдал)
    marker = main.STICKER_DIR.parent / "secret-marker.txt"
    marker.write_text("не должно быть отдано")
    await expect(404, main.get_sticker_file("../secret-marker.txt", A))
    marker.unlink()
    print("STICKERS: выход за пределы каталога стикеров (traversal) не проходит даже когда файл реально существует — OK")
    print("STICKERS: файл отдаётся любому авторизованному (каталог общий), путь наружу — 404 — OK")

    # ---------- лимиты ----------
    # до предела досыпаем напрямую в БД (не через API — иначе упрёмся в лимит частоты запросов, это другая
    # проверка), а саму границу — 120-й и 121-й стикеры — добавляем по-настоящему, через сам эндпоинт
    now = time.time()
    current = await await_count(p2["id"])
    for i in range(main.MAX_STICKERS_PER_PACK - current - 1):
        db.execute("INSERT INTO stickers(pack_id, filename, position, created) VALUES(?,?,?,?)",
                  (p2["id"], f"filler-{i}.jpg", current + i, now))
    db.commit()
    await main.add_sticker(p2["id"], FakeUpload(JPEG), A)   # последний перед пределом — ещё можно
    await expect(422, main.add_sticker(p2["id"], FakeUpload(JPEG), A))
    print(f"STICKERS: не больше {main.MAX_STICKERS_PER_PACK} стикеров в паке — OK")


async def await_count(pack_id):
    return db.execute("SELECT COUNT(*) c FROM stickers WHERE pack_id = ?", (pack_id,)).fetchone()["c"]


asyncio.run(go())
