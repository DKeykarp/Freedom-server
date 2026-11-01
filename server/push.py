"""Push-уведомления через Firebase Cloud Messaging.

Включаются, если задана переменная FIREBASE_CREDENTIALS (путь к JSON
сервисного аккаунта) и установлен пакет firebase-admin. Иначе тихо отключены.
"""
import asyncio
import logging
import os

log = logging.getLogger("push")
_enabled = False

try:
    import firebase_admin
    from firebase_admin import credentials, messaging

    _cred_path = os.getenv("FIREBASE_CREDENTIALS")
    if _cred_path and os.path.exists(_cred_path):
        firebase_admin.initialize_app(credentials.Certificate(_cred_path))
        _enabled = True
        log.info("FCM включён")
    else:
        log.warning("FIREBASE_CREDENTIALS не задан: push-уведомления отключены")
except ImportError:
    log.warning("firebase-admin не установлен: push-уведомления отключены")


def _send(tokens: list[str], data: dict) -> list[str]:
    """Возвращает токены, которые больше не действительны."""
    invalid: list[str] = []
    for i in range(0, len(tokens), 500):  # лимит FCM: 500 токенов за раз
        chunk = tokens[i:i + 500]
        msg = messaging.MulticastMessage(
            tokens=chunk,
            data={k: str(v) for k, v in data.items()},  # data-only: уведомление строит приложение
            android=messaging.AndroidConfig(priority="high"),
        )
        resp = messaging.send_each_for_multicast(msg)
        for tok, r in zip(chunk, resp.responses):
            if not r.success and isinstance(
                    r.exception, (messaging.UnregisteredError,
                                  messaging.SenderIdMismatchError)):
                invalid.append(tok)
    return invalid


async def notify(tokens: list[str], title: str, body: str, data: dict) -> list[str]:
    if not _enabled or not tokens:
        return []
    try:
        return await asyncio.to_thread(_send, tokens, {**data, "title": title, "body": body})
    except Exception:
        log.exception("Ошибка отправки push")
        return []
