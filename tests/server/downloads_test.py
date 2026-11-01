"""Проверка раздачи установщиков (main.py: /api/ping, /api/downloads, /downloads/{name}); FastAPI подменён заглушкой."""
import asyncio, hashlib, os, sys, tempfile, time, types
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ.update(CHAT_DB=str(tmp / "t.db"), CHAT_UPLOADS=str(tmp / "up"), CHAT_DOWNLOADS=str(tmp / "dl"))

def mod(name, **kw):
    m = types.ModuleType(name); m.__dict__.update(kw); sys.modules[name] = m
class FastAPI:
    def __init__(self, **k): pass
    def _dec(self, *a, **k): return lambda f: f
    get = post = put = delete = websocket = middleware = _dec
    def mount(self, *a, **k): pass
class HTTPException(Exception):
    def __init__(self, code, detail=None): self.code, self.detail = code, detail
class Base:
    def __init__(self, **kw): self.__dict__.update(kw)
mod("fastapi", FastAPI=FastAPI, HTTPException=HTTPException, Depends=lambda f=None: None, File=lambda *a, **k: None,
    Form=lambda *a, **k: None, Header=lambda *a, **k: None, Query=lambda *a, **k: None, Request=object, UploadFile=object,
    WebSocket=object, WebSocketDisconnect=Exception)
mod("fastapi.responses", FileResponse=lambda p, **k: ("file", str(p), k)); mod("fastapi.staticfiles", StaticFiles=lambda *a, **k: None)
mod("pydantic", BaseModel=Base, Field=lambda *a, **k: None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
import main

REQ = types.SimpleNamespace(client=types.SimpleNamespace(host="9.9.9.9"))
dl = main.DOWNLOADS_DIR
assert dl == tmp / "dl" and dl.is_dir(), "папка downloads создаётся при старте"

def put(name, data=b"x", age=0):
    p = dl / name; p.write_bytes(data)
    t = time.time() - age; os.utime(p, (t, t)); return p

async def expect(code, coro):
    try: await coro
    except HTTPException as e:
        assert e.code == code, f"ожидали {code}, получили {e.code}"; return
    raise AssertionError(f"ожидали HTTP {code}")

async def go():
    ping = await main.ping()
    assert ping["app"] == "freedom" and ping["protocol"].startswith("FRDM-E2EE")
    assert ping["version"] == main.APP_VERSION
    assert ping["min_client_version"] == {"windows": None, "linux": None, "android": None}, ping["min_client_version"]
    assert await main.downloads_list(REQ) == []
    print("DL: /api/ping опознаёт сервер, отдаёт версию и пустой (по умолчанию) минимум для клиентов — OK")

    exe = b"MZ" + os.urandom(3000)
    put("Freedom-Setup-1.0.0.exe", exe)
    put("Freedom-1.0.0-linux-x64.deb", b"!<arch>" + os.urandom(100))
    put("Freedom-1.0.0-linux-x64.rpm", b"\xed\xab\xee\xdb" + os.urandom(100))
    apk = b"PK" + os.urandom(500)
    put("Freedom-5.0-android.apk", apk)
    # посторонние и вредные файлы рядом
    put("evil.sh", b"rm -rf /"); put("Freedom-x.html", b"<script>"); put("freedom-1.0.0.deb", b"lower"); put("Freedom-1.0.0.deb.sig", b"s")
    (dl / "subdir").mkdir(); (dl / "Freedom-dir.exe").mkdir()
    (dl / "Freedom-link.exe").symlink_to("/etc/passwd")
    items = await main.downloads_list(REQ)
    assert [i["kind"] for i in items] == ["exe", "deb", "rpm", "apk"], items
    e = items[0]
    assert e["name"] == "Freedom-Setup-1.0.0.exe" and e["platform"] == "windows" and e["version"] == "1.0.0"
    assert e["size"] == len(exe) and e["sha256"] == hashlib.sha256(exe).hexdigest() and e["url"] == "/downloads/Freedom-Setup-1.0.0.exe"
    assert {i["platform"] for i in items[1:3]} == {"linux"}
    a = items[3]
    assert a["name"] == "Freedom-5.0-android.apk" and a["platform"] == "android" and a["version"] == "5.0", a
    assert a["size"] == len(apk) and a["sha256"] == hashlib.sha256(apk).hexdigest()
    assert not any(x in str(items) for x in ("evil", "html", "passwd", "link", "sig", "freedom-1"))
    print("DL: в списке Freedom-*.exe|deb|rpm|apk (в т.ч. Android, версия из двух чисел); чужие файлы, каталоги и симлинки отброшены; sha256 и размер верны — OK")

    put("Freedom-Setup-1.1.0.exe", b"MZ-new", age=-100)     # новее по времени
    items = await main.downloads_list(REQ)
    assert next(i for i in items if i["kind"] == "exe")["version"] == "1.1.0"
    # кэш sha256: содержимое изменилось -> хэш пересчитан
    p = put("Freedom-Setup-1.1.0.exe", b"MZ-changed!", age=-200)
    assert next(i for i in await main.downloads_list(REQ) if i["kind"] == "exe")["sha256"] == hashlib.sha256(b"MZ-changed!").hexdigest()
    print("DL: из нескольких версий берётся самая свежая; хэш обновляется при замене файла — OK")

    r = await main.download_file("Freedom-1.0.0-linux-x64.deb", REQ)
    assert r[0] == "file" and r[2]["media_type"] == "application/octet-stream" and r[2]["filename"] == "Freedom-1.0.0-linux-x64.deb"
    assert r[2]["headers"]["X-Content-Type-Options"] == "nosniff"
    for bad in ("evil.sh", "Freedom-x.html", "freedom-1.0.0.deb", "Freedom-1.0.0.deb.sig", "..%2f..%2fetc%2fpasswd", "Freedom-..\\x.exe",
                "Freedom-a/b.exe", "Freedom-.exe", "Freedom-link.exe", "Freedom-dir.exe", "Freedom-Ω.exe", "Freedom-nonexistent.exe"):
        await expect(404, main.download_file(bad, REQ))
    print("DL: скачивание только по строгому имени; обход каталога, симлинк, каталог, чужой тип — 404 — OK")

    limited = types.SimpleNamespace(client=types.SimpleNamespace(host="8.8.8.8"))
    for _ in range(20): await main.download_file("Freedom-1.0.0-linux-x64.rpm", limited)
    await expect(429, main.download_file("Freedom-1.0.0-linux-x64.rpm", limited))
    print("DL: ограничение частоты загрузок (429) — OK")

asyncio.run(go())

# MIN_CLIENT_VERSION читается из окружения один раз при импорте main.py, поэтому проверяем отдельным
# процессом с уже выставленными переменными (тот же приём, что и для проверки отказа на старой базе).
import subprocess
_stub_and_ping = (
    "import sys, os, types, asyncio\n"
    "def mod(name, **kw):\n"
    "    m = types.ModuleType(name); m.__dict__.update(kw); sys.modules[name] = m\n"
    "class FastAPI:\n"
    "    def __init__(s, **k): pass\n"
    "    def _d(s, *a, **k): return lambda f: f\n"
    "    get = post = put = delete = websocket = middleware = _d\n"
    "    def mount(s, *a, **k): pass\n"
    "class HTTPException(Exception):\n"
    "    def __init__(s, code, detail=None): s.code, s.detail = code, detail\n"
    "class Base:\n"
    "    def __init__(s, **kw): s.__dict__.update(kw)\n"
    "mod('fastapi', FastAPI=FastAPI, HTTPException=HTTPException, Depends=lambda f=None: None, File=lambda *a,**k: None,\n"
    "    Form=lambda *a,**k: None, Header=lambda *a,**k: None, Query=lambda *a,**k: None, Request=object, UploadFile=object,\n"
    "    WebSocket=object, WebSocketDisconnect=Exception)\n"
    "mod('fastapi.responses', FileResponse=lambda *a,**k: None); mod('fastapi.staticfiles', StaticFiles=lambda *a,**k: None)\n"
    "mod('pydantic', BaseModel=Base, Field=lambda *a,**k: None)\n"
    "sys.path.insert(0, %r)\n"
    "import main\n"
    "print(asyncio.run(main.ping())['min_client_version'])\n"
) % str(Path(main.__file__).parent)
r = subprocess.run([sys.executable, "-c", _stub_and_ping],
    env={**os.environ, "MIN_CLIENT_VERSION_ANDROID": "5.0", "MIN_CLIENT_VERSION_WINDOWS": "1.2.0"},
    capture_output=True, text=True)
assert r.returncode == 0, r.stderr
got = eval(r.stdout.strip())
assert got == {"windows": "1.2.0", "linux": None, "android": "5.0"}, got
print("DL: минимальная версия клиента читается из MIN_CLIENT_VERSION_* при старте сервера — OK")
