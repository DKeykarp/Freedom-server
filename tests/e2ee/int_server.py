"""HTTP-обёртка над настоящими обработчиками main.py (FastAPI подменён заглушкой) — для интеграционного теста JS-клиента."""
import sys, types, os, asyncio, json, re
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
os.environ["CHAT_DB"] = "/tmp/int.db"; os.environ["CHAT_UPLOADS"] = "/tmp/int_up"
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
mod("fastapi.responses", FileResponse=lambda *a, **k: None); mod("fastapi.staticfiles", StaticFiles=lambda *a, **k: None)
mod("pydantic", BaseModel=Base, Field=lambda *a, **k: None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))
import main

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _go(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or "null") if n else None
        tok = (self.headers.get("Authorization") or "")[7:]
        user = main.user_by_token(tok) if tok else None
        P, m = u.path, self.command
        async def route():
            if P == "/register": return await main.register(main.RegisterIn(**body))
            if P == "/login": return await main.login(main.Credentials(**body), types.SimpleNamespace(client=types.SimpleNamespace(host="t")))
            if user is None: raise HTTPException(401, "auth")
            if P == "/rooms" and m == "GET": return await main.my_rooms(user)
            if P == "/dm": return await main.open_dm(main.DmIn(**body), user)
            if P == "/keys": return await main.my_keys(int(q["room_id"]) if "room_id" in q else None, user)
            mm = re.fullmatch(r"/users/(\w+)/keys", P)
            if mm: return await main.user_keys(mm[1], user)
            mm = re.fullmatch(r"/rooms/(\d+)/(members|keys|messages)", P)
            if mm:
                rid = int(mm[1])
                if mm[2] == "members": return await main.room_members(rid, user)
                if mm[2] == "messages": return await main.history(rid, 50, None, user)
                return await main.post_keys(rid, main.KeysIn(epoch=body["epoch"], envelopes=[main.EnvelopeIn(**e) for e in body["envelopes"]]), user)
            if P == "/rooms" and m == "POST": return await main.create_room(main.RoomIn(**body), user)
            mm = re.fullmatch(r"/rooms/(\d+)/join", P)
            if mm: return await main.join_room(int(mm[1]), user)
            if P == "/_send":   # вместо WebSocket: та же проверка check_e2e и сохранение
                bad = main.check_e2e(body["room_id"], body["epoch"], body["ct"], body["sig"])
                if bad: raise HTTPException(409, bad[0])
                return await main.post_message(user, body["room_id"], epoch=body["epoch"], ct=body["ct"], sig=body["sig"])
            raise HTTPException(404, "no route")
        try: code, out = 200, asyncio.run(route())
        except HTTPException as e: code, out = e.code, {"detail": e.detail}
        except Exception as e: code, out = 500, {"detail": repr(e)}
        data = json.dumps(out).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    do_GET = do_POST = _go
HTTPServer(("127.0.0.1", 8765), H).serve_forever()
