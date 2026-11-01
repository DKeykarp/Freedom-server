"""Подготовка TLS-сертификатов: сборка цепочки и проверка перед запуском (сервер работает только по HTTPS)."""
import re
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path

_PEM = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)
_KEY_NAMES = ("private.key", "privkey.pem", "key.pem")
WARN_DAYS = 14


class CertError(Exception):
    """Сертификат непригоден: сообщение объясняет, что исправить."""


@dataclass
class Prepared:
    chain: Path
    key: Path
    not_after: float
    names: list
    warnings: list = field(default_factory=list)


def _pems(text: str) -> list:
    return [p.strip() for p in _PEM.findall(text)]


def _host_matches(host: str, pattern: str) -> bool:
    host, pattern = host.lower(), pattern.lower()
    if pattern.startswith("*."):
        return host.count(".") == pattern.count(".") and host.split(".", 1)[1] == pattern[2:]
    return host == pattern


def prepare(cert_dir, host: str) -> Prepared:
    """Файлы ZeroSSL/Sectigo: certificate.crt + ca_bundle.crt + private.key (цепочка fullchain.pem собирается
    заново при каждом запуске, поэтому после продления достаточно заменить файлы и перезапустить сервер).
    Либо готовые fullchain.pem + privkey.pem (Let’s Encrypt)."""
    d = Path(cert_dir)
    key = next((d / n for n in _KEY_NAMES if (d / n).is_file()), None)
    leaf, bundle, full = d / "certificate.crt", d / "ca_bundle.crt", d / "fullchain.pem"
    if leaf.is_file():
        certs = _pems(leaf.read_text())
        if not certs:
            raise CertError(f"В {leaf} нет сертификата в формате PEM")
        if bundle.is_file():
            certs += [c for c in _pems(bundle.read_text()) if c not in certs]
        tmp = d / "fullchain.pem.tmp"
        tmp.write_text("\n".join(certs) + "\n")
        tmp.replace(full)
    elif not full.is_file():
        raise CertError(
            f"Нет сертификата. Положите в {d} файлы certificate.crt, ca_bundle.crt и private.key "
            "(или fullchain.pem и privkey.pem).")
    if key is None:
        raise CertError(f"Нет закрытого ключа: положите private.key (или privkey.pem) в {d}")

    if "ENCRYPTED" in key.read_text(errors="ignore")[:400]:
        raise CertError("Закрытый ключ защищён паролем, а сервер запускается без ввода пароля. "
                        "Снимите пароль: openssl rsa -in private.key -out private.key")
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # password=...: без него OpenSSL для ключа с паролем ждёт ввода в терминале и запуск зависает
        ctx.load_cert_chain(str(full), str(key), password=lambda: b"")   # заодно проверяет пару сертификат/ключ
    except ssl.SSLError as e:
        msg = str(e)
        if "KEY_VALUES_MISMATCH" in msg or "key values mismatch" in msg:
            raise CertError("Закрытый ключ не соответствует сертификату (файлы от разных выпусков?)") from None
        if any(s in msg for s in ("bad decrypt", "PEM_R_BAD_PASSWORD", "password", "BAD_PASSWORD", "PROBLEMS_GETTING_PASSWORD")):
            raise CertError("Закрытый ключ защищён паролем, а сервер запускается без ввода пароля. "
                            "Снимите пароль: openssl rsa -in private.key -out private.key") from None
        raise CertError(f"Не удалось загрузить сертификат и ключ: {msg}") from None

    info = ssl._ssl._test_decode_cert(str(full))            # первый сертификат в файле — конечный
    not_after = ssl.cert_time_to_seconds(info["notAfter"])
    not_before = ssl.cert_time_to_seconds(info["notBefore"])
    names = [v for k, v in info.get("subjectAltName", ()) if k == "DNS"]
    if not names:
        raise CertError("В сертификате нет имён (subjectAltName): браузеры такой сертификат не примут")
    if not any(_host_matches(host, n) for n in names):
        raise CertError(f"Сертификат выдан на {', '.join(names)}, а сервер работает как {host}")
    now = time.time()
    if not_after < now:
        raise CertError(f"Сертификат просрочен ({info['notAfter']}). Продлите его и замените файлы в {d}")
    warnings = []
    if not_before > now + 300:
        warnings.append(f"Сертификат начнёт действовать только {info['notBefore']}")
    days = int((not_after - now) // 86400)
    if days < WARN_DAYS:
        warnings.append(f"Сертификат истекает через {days} дн. ({info['notAfter']}): продлите и перезапустите сервер")
    try:
        if key.stat().st_mode & 0o077:
            warnings.append(f"Закрытый ключ доступен другим пользователям системы: chmod 600 {key}")
    except OSError:
        pass
    return Prepared(full, key, not_after, names, warnings)
