import json, os, hashlib
import ref
js = json.load(open("/tmp/js_out.json"))
iters = js["iters"]

# 1) пароль -> auth/kek совпадают с JS; блоб alice/bob открывается
users = {}
for name, u in js["users"].items():
    auth, kek = ref.derive_secrets(name, u["pw"], iters)
    assert auth == u["auth"], f"auth mismatch {name}"
    users[name] = ref.open_blob(u["blob"], kek, name)
    assert ref.b64(users[name].dh_spki) == u["dh"] and ref.b64(users[name].sig_spki) == u["sig"]
print("PY: KDF и блоб ключей (PKCS8 из WebCrypto) — OK")

alice, bob = users["alice"], users["bob"]
# 2) bob разворачивает конверт от alice, читает сообщение
env = next(e for e in js["envelopes"] if e["recipient"] == "bob")
K = ref.unwrap(bob, env, 7, 3, "alice", alice.sig_spki)
assert ref.b64(K) == js["K"]
m = js["msg"]
pl = ref.decrypt_message(K, 7, m["epoch"], "alice", alice.sig_spki, m["ct"], m["sig"])
assert pl["text"] == "Привет, мир! 🔒", pl
print("PY: конверт ключа комнаты и сообщение от JS — OK")

# 3) файл и пустой файл
enc = ref.unb64(js["file"]["enc"])
plain = ref.decrypt_file(enc, js["file"]["key"], js["file"]["nonce"])
assert len(plain) == js["file"]["size"] and ref.b64(hashlib.sha256(plain).digest()) == js["file"]["sha"]
assert ref.decrypt_file(ref.unb64(js["empty"]["enc"]), js["empty"]["key"], js["empty"]["nonce"]) == b""
# усечение файла должно ломаться
try:
    ref.decrypt_file(enc[:-(1<<20)-16], js["file"]["key"], js["file"]["nonce"]); raise SystemExit("truncation not detected")
except Exception as e:
    assert "not detected" not in str(e)
print("PY: файл (блоки 1 МиБ), пустой файл, защита от усечения — OK")

# 4) сигналинг и номер безопасности
sig = ref.open_signal(bob, alice.dh_spki, "alice", "call-123", js["signal"])
assert sig["sdp"]["type"] == "offer"
sn = ref.safety_number(("alice", alice.dh_spki, alice.sig_spki), ("bob", bob.dh_spki, bob.sig_spki))
assert sn == js["safety"], (sn, js["safety"])
print("PY: сигнал звонка и номер безопасности совпали:", sn)

# 5) Python -> JS: bob шлёт alice своё сообщение, файл и сигнал
K2 = os.urandom(32)
out = {"K2": ref.b64(K2), "env": ref.wrap(bob, 9, 1, K2, "alice", alice.dh_spki)}
out["msg"] = ref.encrypt_message(bob, 9, 1, K2, {"v": 1, "t": "audio", "dur": 3.5, "file": {"k": "x"}})
data = os.urandom(1048576 + 100)
e, k, n = ref.encrypt_file(data)
out["file"] = {"enc": ref.b64(e), "key": k, "nonce": n, "sha": ref.b64(hashlib.sha256(data).digest())}
out["signal"] = ref.seal_signal(bob, alice.dh_spki, "alice", "call-999", {"candidate": {"candidate": "candidate:1 1 UDP 1 1.2.3.4 5 typ host"}})
json.dump(out, open("/tmp/py_out.json", "w"))
print("PY: артефакты для JS созданы")
