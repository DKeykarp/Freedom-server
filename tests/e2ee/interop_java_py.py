import json, hashlib, ref
js = json.load(open("/tmp/js_out.json"))
kv = dict(l.split("=", 1) for l in open("/tmp/java_out.txt", encoding="utf-8").read().splitlines())
u = js["users"]
_, kek = ref.derive_secrets("alice", u["alice"]["pw"], js["iters"])
alice = ref.open_blob(u["alice"]["blob"], kek, "alice")
bob_sig = ref.unb64(u["bob"]["sig"])
K = ref.unwrap(alice, {k[4:]: kv[k] for k in ("env_eph", "env_n", "env_c", "env_sig")}, 11, 1, "bob", bob_sig)
assert ref.b64(K) == kv["K"]
pl = ref.decrypt_message(K, 11, 1, "bob", bob_sig, kv["msg_ct"], kv["msg_sig"])
data = ref.decrypt_file(ref.unb64(kv["file_enc"]), kv["file_key"], kv["file_nonce"])
assert ref.b64(hashlib.sha256(data).digest()) == kv["file_sha"]
print("PY: конверт, сообщение и файл от Java (подписи DER->raw) — OK:", pl["text"])
