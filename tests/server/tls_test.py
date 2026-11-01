"""Запуск только по HTTPS: сборка цепочки, проверка сертификатов, перенаправление http -> https, заголовки."""
import asyncio, datetime as dt, http.client, os, socket, ssl, stat, sys, tempfile, threading, types
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

HERE = Path(__file__).resolve().parent
os.environ.update(CHAT_DB=str(Path(tempfile.mkdtemp()) / "t.db"), CHAT_UPLOADS=tempfile.mkdtemp(), CHAT_DOWNLOADS=tempfile.mkdtemp())
sys.path.insert(0, str(HERE / ".." / ".." / "server"))
import tls, serve

HOST = "contora.is-local.org"
NOW = dt.datetime.now(dt.timezone.utc)

def name(cn): return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
def key(): return rsa.generate_private_key(public_exponent=65537, key_size=2048)
def issue(subject, subject_key, issuer, issuer_key, ca=False, days=90, start=0, san=None):
    b = (x509.CertificateBuilder().subject_name(name(subject)).issuer_name(name(issuer)).public_key(subject_key.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(NOW + dt.timedelta(days=start)).not_valid_after(NOW + dt.timedelta(days=start + days))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if san: b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in san]), critical=False)
    return b.sign(issuer_key, hashes.SHA256())
pem = lambda c: c.public_bytes(serialization.Encoding.PEM).decode()
def keypem(k, enc=None): return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.BestAvailableEncryption(enc) if enc else serialization.NoEncryption()).decode()

root_k, inter_k = key(), key()
root = issue("Test Root", root_k, "Test Root", root_k, ca=True, days=3650)
inter = issue("Test Intermediate CA", inter_k, "Test Root", root_k, ca=True, days=1800)

def make_dir(**kw):
    """Папка в формате ZeroSSL: certificate.crt, ca_bundle.crt, private.key."""
    d = Path(tempfile.mkdtemp()); lk = kw.pop("leaf_key", None) or key()
    leaf = issue(kw.pop("cn", HOST), lk, "Test Intermediate CA", inter_k, san=kw.pop("san", [HOST]), **kw)
    (d / "certificate.crt").write_text(pem(leaf)); (d / "ca_bundle.crt").write_text(pem(inter))
    (d / "private.key").write_text(keypem(lk)); (d / "private.key").chmod(0o600)
    return d, lk

def raises(msg_re, fn):
    import re
    try: fn()
    except tls.CertError as e:
        assert re.search(msg_re, str(e)), f"сообщение не то: {e}"; return str(e)
    raise AssertionError("ожидали CertError: " + msg_re)

# 1. сборка цепочки и настоящее TLS-рукопожатие: клиент знает только корень, значит сервер обязан отдать промежуточный
d, _ = make_dir()
p = tls.prepare(d, HOST)
chain = (d / "fullchain.pem").read_text()
assert chain.count("BEGIN CERTIFICATE") == 2 and p.names == [HOST] and p.warnings == []
sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); sctx.load_cert_chain(str(p.chain), str(p.key))
def handshake(server_ctx, server_hostname=HOST):
    ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(1); port = ls.getsockname()[1]
    def srv():
        try:
            c, _ = ls.accept()
            with server_ctx.wrap_socket(c, server_side=True) as t: t.recv(1)
        except Exception: pass
    th = threading.Thread(target=srv, daemon=True); th.start()
    cctx = ssl.create_default_context(cadata=pem(root))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s, cctx.wrap_socket(s, server_hostname=server_hostname) as t:
            t.send(b"x"); return t.version()
    finally: ls.close(); th.join(2)
assert handshake(sctx).startswith("TLS")
print("TLS: цепочка собрана (сертификат + промежуточный), клиент с одним корнем принимает сервер — OK")
leaf_only = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); (d / "leaf.pem").write_text(pem(x509.load_pem_x509_certificate((d / "certificate.crt").read_bytes()))); leaf_only.load_cert_chain(str(d / "leaf.pem"), str(p.key))
try: handshake(leaf_only); raise AssertionError("без промежуточного сертификата клиент должен отказать")
except ssl.SSLError: pass
try: handshake(sctx, "other.example.com"); raise AssertionError("чужое имя принято")
except ssl.SSLCertVerificationError: pass
print("TLS: без промежуточного сертификата и для чужого имени клиент отказывает (значит, проверка настоящая) — OK")

# 2. продление: заменили certificate.crt -> цепочка пересобрана; дубликаты не копятся
d2, k2 = make_dir(); old = (d2 / "certificate.crt").read_text(); tls.prepare(d2, HOST)
new_leaf = issue(HOST, k2, "Test Intermediate CA", inter_k, san=[HOST], days=89)
(d2 / "certificate.crt").write_text(pem(new_leaf) + pem(inter))     # в certificate.crt уже лежит цепочка
tls.prepare(d2, HOST); fc = (d2 / "fullchain.pem").read_text()
assert old.strip() not in fc and fc.count("BEGIN CERTIFICATE") == 2
d3 = Path(tempfile.mkdtemp()); (d3 / "fullchain.pem").write_text(fc); (d3 / "privkey.pem").write_text(keypem(k2)); (d3 / "privkey.pem").chmod(0o600)
assert tls.prepare(d3, HOST).names == [HOST]
print("TLS: продление подхватывается без дублей; формат fullchain.pem + privkey.pem тоже работает — OK")

# 3. негодные сертификаты: понятные причины отказа
dm, _ = make_dir(); (dm / "private.key").write_text(keypem(key()))
raises("не соответствует", lambda: tls.prepare(dm, HOST))
de, ek = make_dir(); (de / "private.key").write_text(keypem(ek, b"secret"))
raises("защищён паролем", lambda: tls.prepare(de, HOST))
raises("просрочен", lambda: tls.prepare(make_dir(days=10, start=-30)[0], HOST))
raises("выдан на", lambda: tls.prepare(make_dir(san=["other.example.com"], cn="other.example.com")[0], HOST))
raises("нет имён", lambda: tls.prepare(make_dir(san=None)[0], HOST))
raises("Нет сертификата", lambda: tls.prepare(Path(tempfile.mkdtemp()), HOST))
dk, _ = make_dir(); (dk / "private.key").unlink(); raises("Нет закрытого ключа", lambda: tls.prepare(dk, HOST))
dj, _ = make_dir(); (dj / "certificate.crt").write_text("мусор"); raises("нет сертификата в формате PEM", lambda: tls.prepare(dj, HOST))
print("TLS: чужой ключ, ключ с паролем, просрочка, чужое имя, нет SAN, нет файлов, мусор — понятные отказы — OK")

# 4. предупреждения: скорое истечение и права ключа; шаблонные wildcard
dw, _ = make_dir(days=5); (dw / "private.key").chmod(0o644)
w = tls.prepare(dw, HOST).warnings
assert any("истекает через" in x for x in w) and any("chmod 600" in x for x in w), w
assert tls.prepare(make_dir(san=["*.is-local.org"], cn="*.is-local.org")[0], HOST).names == ["*.is-local.org"]
raises("выдан на", lambda: tls.prepare(make_dir(san=["*.ddns.net"], cn="*.ddns.net")[0], "a.b.ddns.net"))   # wildcard не покрывает два уровня
print("TLS: предупреждения о сроке и правах ключа; wildcard только на один уровень — OK")

# 5. перенаправление http -> https: адрес назначения фиксирован
srv = serve.start_redirect(0, "127.0.0.1"); port = srv.server_address[1]
def raw(reqline, headers=""):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(f"{reqline}\r\nHost: evil.example.com\r\n{headers}\r\n".encode()); s.settimeout(3)
        data = b""
        try:
            while chunk := s.recv(4096): data += chunk
        except socket.timeout: pass
    return data.decode(errors="replace")
r = raw("GET /rooms?x=1 HTTP/1.1"); assert r.startswith("HTTP/1.0 308") or r.startswith("HTTP/1.1 308"), r
assert f"Location: https://{HOST}/rooms?x=1" in r and "evil" not in r
assert f"Location: https://{HOST}/" in raw("POST /register HTTP/1.1") and " 308 " in raw("POST /register HTTP/1.1")
assert f"Location: https://{HOST}/" in raw("GET http://evil.example.com/x HTTP/1.1"), "абсолютный URI не должен менять адрес назначения"
r = raw("GET /a%0d%0aSet-Cookie:x=1 HTTP/1.1"); assert "\r\nSet-Cookie" not in r, "инъекция заголовка"
assert " 308 " in raw("HEAD / HTTP/1.1"); srv.shutdown()
busy = socket.socket(); busy.bind(("127.0.0.1", 0)); busy.listen(1)
assert serve.start_redirect(busy.getsockname()[1], "127.0.0.1") is None, "занятый порт не должен ронять запуск"; busy.close()
print("HTTP: порт 80 перенаправляет на https://" + HOST + " с тем же путём (308), Host и абсолютный URI не влияют, инъекции нет — OK")

# 6. заголовки безопасности приложения
def stub(name, **kw):
    m = types.ModuleType(name); m.__dict__.update(kw); sys.modules[name] = m
class App:
    def __init__(s, **k): pass
    def _d(s, *a, **k): return lambda f: f
    get = post = put = delete = websocket = middleware = _d
    def mount(s, *a, **k): pass
class HE(Exception): pass
class Base:
    def __init__(s, **kw): s.__dict__.update(kw)
stub("fastapi", FastAPI=App, HTTPException=HE, Depends=lambda f=None: None, File=lambda *a, **k: None, Form=lambda *a, **k: None, Header=lambda *a, **k: None,
     Query=lambda *a, **k: None, Request=object, UploadFile=object, WebSocket=object, WebSocketDisconnect=Exception)
stub("fastapi.responses", FileResponse=lambda *a, **k: None); stub("fastapi.staticfiles", StaticFiles=lambda *a, **k: None); stub("pydantic", BaseModel=Base, Field=lambda *a, **k: None)
import main
class Resp: headers = types.SimpleNamespace(setdefault=lambda k, v: H.__setitem__(k, v))
H = {}
async def call_next(_): return Resp()
asyncio.run(main.security_headers(None, call_next))
assert H["Strict-Transport-Security"].startswith("max-age=31536000")
csp = H["Content-Security-Policy"]
assert "connect-src 'self' wss:" in csp and " ws:" not in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
print("HTTPS: HSTS включён, CSP разрешает только wss (без открытого ws) — OK")
