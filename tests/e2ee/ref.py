"""Эталонная реализация FRDM-E2EE v1 на Python (для проверки совместимости клиентов)."""
import base64, json, os, hashlib
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

CH = 1 << 20
b64 = lambda b: base64.b64encode(b).decode()
unb64 = base64.b64decode
U = lambda s: s.encode()

def hkdf(ikm, info, n=32):
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=None, info=info if isinstance(info, bytes) else U(info)).derive(ikm)

def derive_secrets(username, password, iters):
    master = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=U("freedom-e2ee-v1:" + username), iterations=iters).derive(U(password))
    return b64(hkdf(master, "auth")), hkdf(master, "kek")

def spki(pub): return pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
def pkcs8(priv): return priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())

def sign(priv, data):
    r, s = decode_dss_signature(priv.sign(data, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")

def verify(pub, sig, data):
    try:
        pub.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")), data, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False

class Identity:
    def __init__(self, username, dh=None, sg=None):
        self.username = username
        self.dh = dh or ec.generate_private_key(ec.SECP256R1())
        self.sig = sg or ec.generate_private_key(ec.SECP256R1())
    @property
    def dh_spki(self): return spki(self.dh.public_key())
    @property
    def sig_spki(self): return spki(self.sig.public_key())

def seal_blob(ident, kek, username):
    plain = json.dumps({"dh": b64(pkcs8(ident.dh)), "dhPub": b64(ident.dh_spki), "sig": b64(pkcs8(ident.sig)), "sigPub": b64(ident.sig_spki)})
    n = os.urandom(12)
    return json.dumps({"v": 1, "n": b64(n), "c": b64(AESGCM(kek).encrypt(n, U(plain), U("FRDM-KEYBLOB-v1|" + username)))})

def open_blob(blob, kek, username):
    o = json.loads(blob)
    p = json.loads(AESGCM(kek).decrypt(unb64(o["n"]), unb64(o["c"]), U("FRDM-KEYBLOB-v1|" + username)))
    ld = lambda k: serialization.load_der_private_key(unb64(p[k]), None)
    return Identity(username, ld("dh"), ld("sig"))

def msg_key(K): return hkdf(K, "FRDM-MSG-KEY-v1")

def wrap(me, room, epoch, K, recipient, recipient_dh_spki):
    eph = ec.generate_private_key(ec.SECP256R1())
    info = U(f"FRDM-WRAP-v1|{room}|{epoch}|{me.username}|{recipient}")
    z = eph.exchange(ec.ECDH(), serialization.load_der_public_key(recipient_dh_spki))
    n = os.urandom(12)
    c = AESGCM(hkdf(z, info)).encrypt(n, K, info)
    e = spki(eph.public_key())
    sig = sign(me.sig, U(f"FRDM-ENV-v1|{room}|{epoch}|{me.username}|{recipient}") + b"\0" + e + n + c)
    return {"recipient": recipient, "eph": b64(e), "n": b64(n), "c": b64(c), "sig": b64(sig)}

def unwrap(me, env, room, epoch, sender, sender_sig_spki):
    e, n, c = unb64(env["eph"]), unb64(env["n"]), unb64(env["c"])
    data = U(f"FRDM-ENV-v1|{room}|{epoch}|{sender}|{me.username}") + b"\0" + e + n + c
    assert verify(serialization.load_der_public_key(sender_sig_spki), unb64(env["sig"]), data), "bad envelope signature"
    info = U(f"FRDM-WRAP-v1|{room}|{epoch}|{sender}|{me.username}")
    z = me.dh.exchange(ec.ECDH(), serialization.load_der_public_key(e))
    return AESGCM(hkdf(z, info)).decrypt(n, c, info)

def encrypt_message(me, room, epoch, K, payload):
    aad = U(f"FRDM-MSG-v1|{room}|{epoch}|{me.username}")
    n = os.urandom(12)
    ct = n + AESGCM(msg_key(K)).encrypt(n, U(json.dumps(payload)), aad)
    return {"epoch": epoch, "ct": b64(ct), "sig": b64(sign(me.sig, aad + b"\0" + ct))}

def decrypt_message(K, room, epoch, sender, sender_sig_spki, ct_b64, sig_b64):
    aad = U(f"FRDM-MSG-v1|{room}|{epoch}|{sender}")
    ct = unb64(ct_b64)
    assert verify(serialization.load_der_public_key(sender_sig_spki), unb64(sig_b64), aad + b"\0" + ct), "bad message signature"
    return json.loads(AESGCM(msg_key(K)).decrypt(ct[:12], ct[12:], aad))

def chunk_nonce(base, i):
    n = bytearray(base)
    for j, b in enumerate(i.to_bytes(4, "big")): n[8 + j] ^= b
    return bytes(n)
def chunk_aad(i, last): return b"FRDM-CHUNK-v1" + i.to_bytes(4, "big") + (b"\1" if last else b"\0")

def encrypt_file(data):
    k, base = os.urandom(32), os.urandom(12)
    n = max(1, -(-len(data) // CH))
    out = b"".join(AESGCM(k).encrypt(chunk_nonce(base, i), data[i*CH:(i+1)*CH], chunk_aad(i, i == n - 1)) for i in range(n))
    return out, b64(k), b64(base)

def decrypt_file(enc, k_b64, base_b64):
    k, base, step = unb64(k_b64), unb64(base_b64), CH + 16
    n = max(1, -(-len(enc) // step))
    return b"".join(AESGCM(k).decrypt(chunk_nonce(base, i), enc[i*step:(i+1)*step], chunk_aad(i, i == n - 1)) for i in range(n))

def seal_signal(me, peer_dh_spki, peer, call_id, obj):
    z = me.dh.exchange(ec.ECDH(), serialization.load_der_public_key(peer_dh_spki))
    n = os.urandom(12)
    c = AESGCM(hkdf(z, "FRDM-CALL-v1|" + call_id)).encrypt(n, U(json.dumps(obj)), U(f"FRDM-SIG-v1|{call_id}|{me.username}|{peer}"))
    return {"n": b64(n), "c": b64(c)}

def open_signal(me, peer_dh_spki, peer, call_id, data):
    z = me.dh.exchange(ec.ECDH(), serialization.load_der_public_key(peer_dh_spki))
    return json.loads(AESGCM(hkdf(z, "FRDM-CALL-v1|" + call_id)).decrypt(unb64(data["n"]), unb64(data["c"]), U(f"FRDM-SIG-v1|{call_id}|{peer}|{me.username}")))

def safety_number(a, b):
    fp = lambda u, d, s: hashlib.sha256(U(f"FRDM-FP-v1|{u}|") + d + s).digest()
    pair = sorted([(a[0], fp(*a)), (b[0], fp(*b))], key=lambda x: x[0])
    h = hashlib.sha256(pair[0][1] + pair[1][1]).digest()
    return " ".join(str(int.from_bytes(h[4*i:4*i+4], "big") % 100000).zfill(5) for i in range(8))
