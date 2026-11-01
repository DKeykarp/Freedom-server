"""Запуск сервера Freedom только по HTTPS.

    python serve.py           # https на порту 443, а порт 80 перенаправляет на https
    python serve.py --check   # только проверить сертификаты и выйти

Сертификаты лежат в папке certs/ (certificate.crt, ca_bundle.crt, private.key). Переменные окружения:
CHAT_HOST (адрес сервера, по умолчанию contora.is-local.org), CHAT_CERT_DIR, PORT (443), HTTP_PORT (80, 0 = не
перенаправлять), BIND (0.0.0.0). Порты 80 и 443 требуют прав: root или `setcap 'cap_net_bind_service=+ep' python3`.
"""
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import tls

HOST = os.getenv("CHAT_HOST", "contora.is-local.org")
CERT_DIR = Path(os.getenv("CHAT_CERT_DIR", str(Path(__file__).parent / "certs")))
BIND = os.getenv("BIND", "0.0.0.0")
log = logging.getLogger("freedom.serve")


class Redirect(BaseHTTPRequestHandler):
    """http:// -> https://. Адрес назначения фиксирован (заголовок Host не используется), поэтому
    перенаправление нельзя направить на чужой сайт."""

    def _go(self):
        path = self.path if self.path.startswith("/") else "/"
        path = "".join(c for c in path if c.isprintable() and c not in "\r\n")
        self.send_response(308)                       # 308 сохраняет метод запроса
        self.send_header("Location", f"https://{HOST}{path}")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _go

    def log_message(self, *args):
        pass


def start_redirect(port: int, bind: str = BIND):
    try:
        srv = ThreadingHTTPServer((bind, port), Redirect)
    except OSError as e:
        log.warning("Порт %s недоступен (%s): перенаправление http -> https отключено", port, e)
        return None
    threading.Thread(target=srv.serve_forever, daemon=True, name="http-redirect").start()
    return srv


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        p = tls.prepare(CERT_DIR, HOST)
    except tls.CertError as e:
        log.error("%s", e)
        return 1
    days = int((p.not_after - time.time()) // 86400)
    log.info("Сертификат для %s действителен ещё %d дн.", ", ".join(p.names), days)
    for w in p.warnings:
        log.warning("%s", w)
    if "--check" in argv:
        return 0

    http_port = int(os.getenv("HTTP_PORT", "80"))
    if http_port:
        start_redirect(http_port)
    sys.path.insert(0, str(Path(__file__).parent))
    import uvicorn
    uvicorn.run("main:app", host=BIND, port=int(os.getenv("PORT", "443")),
                ssl_certfile=str(p.chain), ssl_keyfile=str(p.key), server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
