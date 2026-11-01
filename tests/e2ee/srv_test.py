import sys, types, os, asyncio, time, json, sqlite3, subprocess
os.environ["CHAT_DB"] = "/tmp/srv.db"; os.environ["CHAT_UPLOADS"] = "/tmp/srv_up"
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
import ref, main
from db import db

async def expect(code, coro):
    try: await coro
    except HTTPException as e:
        assert e.code == code, f"ожидали {code}, получили {e.code}: {e.detail}"; return
    raise AssertionError(f"ожидали HTTP {code}")

users = {}
async def reg(name):
    ident = ref.Identity(name)
    auth, kek = ref.derive_secrets(name, "pw-" + name, 1000)
    blob = ref.seal_blob(ident, kek, name)
    r = await main.register(main.RegisterIn(username=name, password=auth, dh_pub=ref.b64(ident.dh_spki), sig_pub=ref.b64(ident.sig_spki), key_blob=blob))
    users[name] = {"id": db.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()["id"], "username": name, "ident": ident, "blob": blob, "auth": auth}
    assert r["key_blob"] == blob

async def go():
    for n in ("alice", "bob", "carol"): await reg(n)
    A, B, C = users["alice"], users["bob"], users["carol"]
    await expect(422, main.register(main.RegisterIn(username="mallory", password="x"*44, dh_pub="AAAA", sig_pub="AAAA", key_blob="{" + "x"*30 + "}")))
    class Req: client = types.SimpleNamespace(host="1.2.3.4")
    lg = await main.login(main.Credentials(username="alice", password=A["auth"]), Req())
    assert lg["key_blob"] == A["blob"]; await expect(401, main.login(main.Credentials(username="alice", password="wrong"*10), Req()))
    print("SRV: регистрация с ключами, отказ по неверным ключам, login отдаёт блоб — OK")

    dm = await main.open_dm(main.DmIn(username="bob"), A); rid = dm["id"]
    assert dm["epoch"] == 0 and dm["peer"] == "bob"
    assert (await main.user_keys("bob", A))["dh_pub"] == ref.b64(B["ident"].dh_spki)   # общая комната уже есть — ключи видны
    K = os.urandom(32)
    envs = lambda me, room, ep, K, names: [main.EnvelopeIn(**ref.wrap(me["ident"], room, ep, K, n, users[n]["ident"].dh_spki)) for n in names]
    await main.post_keys(rid, main.KeysIn(epoch=1, envelopes=envs(A, rid, 1, K, ["alice", "bob"])), A)
    kb = await main.my_keys(rid, B)
    assert kb["latest"][rid] == 1 and len(kb["envelopes"]) == 1 and kb["envelopes"][0]["sender"] == "alice"
    assert ref.unwrap(B["ident"], kb["envelopes"][0], rid, 1, "alice", A["ident"].sig_spki) == K
    print("SRV: эпоха 1 выдана, bob разворачивает конверт alice — OK")

    await expect(409, main.post_keys(rid, main.KeysIn(epoch=1, envelopes=envs(B, rid, 1, K, ["alice", "bob"])), B))
    await expect(409, main.post_keys(rid, main.KeysIn(epoch=2, envelopes=envs(B, rid, 2, K, ["bob"])), B))
    await expect(422, main.post_keys(rid, main.KeysIn(epoch=2, envelopes=envs(B, rid, 2, K, ["alice", "bob", "carol"])), B))
    bad = envs(B, rid, 2, K, ["alice", "bob"]); bad[0].c = ref.b64(b"short")
    await expect(422, main.post_keys(rid, main.KeysIn(epoch=2, envelopes=bad), B))
    await expect(403, main.post_keys(rid, main.KeysIn(epoch=2, envelopes=envs(C, rid, 2, K, ["alice", "bob"])), C))
    print("SRV: 409 при гонке и неполном списке, 422 для чужого/кривого, 403 для не-участника — OK")

    m = ref.encrypt_message(A["ident"], rid, 1, K, {"v": 1, "t": "text", "text": "секрет"})
    assert main.check_e2e(rid, 1, m["ct"], m["sig"]) is None
    assert main.check_e2e(rid, 2, m["ct"], m["sig"])[0] == "stale_epoch"
    assert main.check_e2e(rid, 1, "!!!", m["sig"])[0] == "bad_message"
    assert main.check_e2e(rid, 1, m["ct"], ref.b64(b"x"*10))[0] == "bad_message"
    await main.post_message(A, rid, epoch=1, ct=m["ct"], sig=m["sig"])
    hist = await main.history(rid, 50, None, B)
    assert len(hist) == 1 and hist[0]["ct"] == m["ct"] and hist[0]["text"] == ""
    assert ref.decrypt_message(K, rid, 1, "alice", A["ident"].sig_spki, hist[0]["ct"], hist[0]["sig"])["text"] == "секрет"
    dump = "".join(str(tuple(r)) for r in db.execute("SELECT * FROM messages"))
    assert "секрет" not in dump
    rl = (await main.my_rooms(B))[0]; assert rl["unread"] == 1 and rl["last"]["ct"] == m["ct"]
    print("SRV: сообщения хранятся как шифртекст (открытого текста нет в БД), превью — шифртекст — OK")

    class UF:
        def __init__(s, d): s.d = d
        async def read(s, n=-1): return s.d[:n] if n >= 0 else s.d
    enc, fk, fn = ref.encrypt_file(os.urandom(5000))
    fm = ref.encrypt_message(A["ident"], rid, 1, K, {"v": 1, "t": "image", "file": {"k": fk, "n": fn}})
    up = await main.upload_media(rid, 1, fm["ct"], fm["sig"], UF(enc), A)
    assert up["media"].endswith(".bin") and up["kind"] == "e2e"
    await expect(409, main.upload_media(rid, 5, fm["ct"], fm["sig"], UF(enc), A))
    await expect(413, main.upload_media(rid, 1, fm["ct"], fm["sig"], UF(b"x" * (main.MAX_ENC_FILE + 1)), A))
    r = await main.get_file(up["media"].split("/")[-1], B)
    assert r[2]["media_type"] == "application/octet-stream" and open(r[1], "rb").read() == enc
    await expect(404, main.get_file(up["media"].split("/")[-1], C))
    print("SRV: файл хранится зашифрованным, отдаётся только участникам как octet-stream — OK")

    group = await main.create_room(main.RoomIn(name="Тестовая группа", kind="group"), A)
    gid = group["id"]
    await main.join_room(gid, B); await main.join_room(gid, C)
    Kg = os.urandom(32)
    await main.post_keys(gid, main.KeysIn(epoch=1, envelopes=envs(C, gid, 1, Kg, ["alice", "bob", "carol"])), C)
    await reg("dave"); D = users["dave"]
    await main.join_room(gid, D)   # дэйв уже вступил — список получателей ниже устарел (без него) — 409
    await expect(409, main.post_keys(gid, main.KeysIn(epoch=2, envelopes=envs(D, gid, 2, Kg, ["alice", "bob", "carol"])), D))
    await main.post_keys(gid, main.KeysIn(epoch=2, envelopes=envs(D, gid, 2, os.urandom(32), ["alice", "bob", "carol", "dave"])), D)
    assert [e["epoch"] for e in (await main.my_keys(gid, D))["envelopes"]] == [2]
    assert [e["epoch"] for e in (await main.my_keys(gid, C))["envelopes"]] == [1, 2]
    print("SRV: новый участник создаёт эпоху для всех и не получает ключи прежних эпох — OK")

    class W:
        def __init__(s): s.o = []
        async def send_json(s, p): s.o.append(p)
    wa, wb = W(), W(); main.manager.by_user = {A["id"]: {wa}, B["id"]: {wb}}
    async def err(t): raise AssertionError(t)
    await main.handle_call(A, "call_invite", {"room_id": rid, "call_id": "c1", "video": False}, err)
    await main.handle_call(B, "call_accept", {"call_id": "c1"}, err)
    wb.o.clear()
    await main.handle_call(A, "call_signal", {"call_id": "c1", "to": B["id"], "data": {"sdp": "PLAINTEXT SDP"}}, err)
    await main.handle_call(A, "call_signal", {"call_id": "c1", "to": B["id"], "data": {"n": "AAAA", "c": "BBBB", "extra": 1}}, err)
    sig = [p for p in wb.o if p["type"] == "call_signal"]
    assert len(sig) == 1 and sig[0]["data"] == {"n": "AAAA", "c": "BBBB"}
    await main.handle_call(A, "call_end", {"call_id": "c1"}, err)
    print("SRV: открытый сигнал отбрасывается, зашифрованный {n,c} пересылается — OK")

    await main.push_message([B["id"]], "alice", rid)

    o = sqlite3.connect("/tmp/old.db"); o.executescript("CREATE TABLE users(id INTEGER, username TEXT);"); o.commit(); o.close()
    r = subprocess.run([sys.executable, "-c", "import sys, os; sys.path.insert(0, %r); import db" % os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server")], env={**os.environ, "CHAT_DB": "/tmp/old.db"}, capture_output=True, text=True)
    assert "слишком старая версия" in r.stderr, r.stderr[-300:]
    print("SRV: база слишком старой (домиграционной) версии отклоняется с понятным сообщением — OK")

asyncio.run(go())
