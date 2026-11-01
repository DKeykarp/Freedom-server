"""Распознавание речи: main.py, FastAPI подменён заглушкой (как в остальных server-тестах).
Сам faster-whisper здесь не ставится (тяжёлая зависимость, нужна сеть) — тестируется HTTP-слой
(выключено по умолчанию, лимиты, коды ошибок), а интеграция с моделью — подменена заглушкой."""
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


async def go():
    await reg("alice")
    A = users["alice"]

    # ---------- по умолчанию выключено — явный, понятный отказ, а не попытка загрузить несуществующую модель ----------
    assert main.WHISPER_MODEL_NAME is None, "по умолчанию функция должна быть выключена"
    detail = await expect(503, main.transcribe_speech(FakeUpload(b"\x00" * 100), A))
    assert "не настроено" in detail.lower()
    print("SPEECH: по умолчанию выключено — 503 с понятным сообщением, а не попытка загрузить модель — OK")

    # ---------- включаем (без реального faster-whisper — подменяем саму функцию распознавания) ----------
    main.WHISPER_MODEL_NAME = "tiny"
    calls = []

    async def fake_transcribe(data):
        calls.append(data)
        return "привет, это проверка"

    real_to_thread = asyncio.to_thread
    async def to_thread_stub(fn, *a, **k):
        if fn is main._transcribe_sync:
            return await fake_transcribe(*a, **k)
        return await real_to_thread(fn, *a, **k)
    asyncio.to_thread = to_thread_stub
    try:
        r = await main.transcribe_speech(FakeUpload(b"\x00" * 1000), A)
        assert r == {"text": "привет, это проверка"}
        assert len(calls) == 1 and calls[0] == b"\x00" * 1000, "на распознавание должны уйти именно присланные байты"
        print("SPEECH: включено — отдаёт текст, в модель уходят ровно присланные байты — OK")

        # ---------- лимиты и валидация ----------
        huge = FakeUpload(b"\x00" * (main.MAX_TRANSCRIBE_BYTES + 1))
        await expect(413, main.transcribe_speech(huge, A))
        print(f"SPEECH: файл больше {main.MAX_TRANSCRIBE_BYTES} байт отклоняется (413) — OK")

        await expect(422, main.transcribe_speech(FakeUpload(b"\x00\x01"), A))
        print("SPEECH: подозрительно маленький файл отклоняется (422) — OK")

        # ---------- сбой самого распознавания -> понятная 500, не падение процесса ----------
        async def failing_transcribe(data):
            raise RuntimeError("модель упала")
        async def to_thread_fail(fn, *a, **k):
            if fn is main._transcribe_sync:
                return await failing_transcribe(*a, **k)
            return await real_to_thread(fn, *a, **k)
        asyncio.to_thread = to_thread_fail
        await expect(500, main.transcribe_speech(FakeUpload(b"\x00" * 1000), A))
        print("SPEECH: сбой самого распознавания — понятная 500, не падение процесса — OK")
    finally:
        asyncio.to_thread = real_to_thread
        main.WHISPER_MODEL_NAME = None

    # ---------- частота запросов ограничена ----------
    main.WHISPER_MODEL_NAME = "tiny"
    async def to_thread_ok(fn, *a, **k):
        if fn is main._transcribe_sync:
            return "ok"
        return await real_to_thread(fn, *a, **k)
    asyncio.to_thread = to_thread_ok
    try:
        hit = False
        for _ in range(30):
            try:
                await main.transcribe_speech(FakeUpload(b"\x00" * 100), users["alice"])
            except main.HTTPException as e:
                assert e.code == 429
                hit = True
                break
        assert hit, "частые запросы должны когда-то упереться в лимит"
        print("SPEECH: частота запросов ограничена (429) — OK")
    finally:
        asyncio.to_thread = real_to_thread
        main.WHISPER_MODEL_NAME = None

asyncio.run(go())
