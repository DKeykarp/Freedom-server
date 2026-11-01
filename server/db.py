"""SQLite: подключение и схема (версия 13 — аватары)."""
import logging
import os
import shutil
import sqlite3
import time

DB_PATH = os.getenv("CHAT_DB", "freedom.db")
SCHEMA_VERSION = 13
MIN_MIGRATABLE_VERSION = 7  # раньше этой версии инкрементальные миграции не пишем — слишком старо и не запрошено
log = logging.getLogger("freedom.db")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

_TABLES = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    salt BLOB NOT NULL,
    pw_hash BLOB NOT NULL,
    dh_pub TEXT NOT NULL,        -- публичный ключ ECDH (SPKI, base64)
    sig_pub TEXT NOT NULL,       -- публичный ключ ECDSA (SPKI, base64)
    key_blob TEXT NOT NULL,      -- приватные ключи, зашифрованные ключом из пароля (сервер не может их прочитать)
    bio TEXT NOT NULL DEFAULT '', -- открытый профиль: виден всем, кто ищет пользователя по имени
    avatar TEXT                  -- имя файла аватара (открытый, не E2EE — как и био); NULL — аватара нет
);
CREATE TABLE IF NOT EXISTS tokens(
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    is_direct INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'group',     -- group | channel (для is_direct=1 роль/kind не имеют значения)
    created REAL NOT NULL,
    key_epoch INTEGER NOT NULL DEFAULT 0,  -- актуальная эпоха ключа комнаты
    autodelete_seconds INTEGER             -- самоудаление сообщений (NULL/0 = выключено)
);
CREATE TABLE IF NOT EXISTS room_members(
    room_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    last_read_id INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'member',   -- owner | admin | member — в канале писать могут только owner/admin
    PRIMARY KEY(room_id, user_id)
);
-- ключ комнаты, зашифрованный для каждого участника отдельно
CREATE TABLE IF NOT EXISTS room_keys(
    room_id INTEGER NOT NULL,
    epoch INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    eph TEXT NOT NULL, n TEXT NOT NULL, c TEXT NOT NULL, sig TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY(room_id, epoch, recipient_id)
);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    kind TEXT NOT NULL,          -- e2e | call
    text TEXT NOT NULL DEFAULT '',   -- только для kind=call (итог звонка, формирует сервер)
    epoch INTEGER,
    ct TEXT,                     -- шифртекст (base64): nonce || AES-GCM
    sig TEXT,                    -- подпись отправителя (base64, r||s)
    media TEXT,                  -- имя зашифрованного файла в uploads/
    ts REAL NOT NULL,
    edited_ts REAL               -- когда отредактировано (метаданные, как и ts, — открыты серверу)
);
-- «удалить у себя»: сообщение остаётся у всех остальных, просто пропускается в истории этого пользователя
CREATE TABLE IF NOT EXISTS hidden_messages(
    user_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY(user_id, message_id)
);
CREATE TABLE IF NOT EXISTS devices(
    fcm_token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL
);
-- реакции: как обычное сообщение — зашифрованы ключом комнаты, но одна запись на (сообщение, пользователь),
-- новая реакция того же человека на то же сообщение просто заменяет прежнюю (переключение/смена эмодзи)
CREATE TABLE IF NOT EXISTS reactions(
    room_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    ct TEXT NOT NULL,
    sig TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY(message_id, user_id)
);
CREATE TABLE IF NOT EXISTS pinned_messages(
    room_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    pinned_by TEXT NOT NULL,
    pinned_at REAL NOT NULL,
    PRIMARY KEY(room_id, message_id)
);
-- отложенная отправка: только текст (см. main.py) — видно и отменить может только тот, кто создал
CREATE TABLE IF NOT EXISTS scheduled_messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    ct TEXT NOT NULL,
    sig TEXT NOT NULL,
    send_at REAL NOT NULL,
    created REAL NOT NULL
);
-- голос за опрос: как реакция — зашифрован ключом комнаты, одна запись на (сообщение, пользователь), опрос
-- сам по себе не отдельная сущность, а обычное сообщение с t:"poll" внутри зашифрованного payload
CREATE TABLE IF NOT EXISTS poll_votes(
    room_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    ct TEXT NOT NULL,
    sig TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY(message_id, user_id)
);
-- каталог стикеров: содержимое открытое (не E2EE) — это общий, видимый всем пользователям каталог,
-- а не переписка. Пак создаёт один пользователь, добавлять стикеры в него может только он же.
CREATE TABLE IF NOT EXISTS sticker_packs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    creator_id INTEGER NOT NULL,
    creator_username TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stickers(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    position INTEGER NOT NULL,
    created REAL NOT NULL
);
-- какие паки пользователь установил себе (лоток быстрого доступа в composer'е)
CREATE TABLE IF NOT EXISTS installed_packs(
    user_id INTEGER NOT NULL,
    pack_id INTEGER NOT NULL,
    installed_at REAL NOT NULL,
    PRIMARY KEY(user_id, pack_id)
);
"""

_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_room_name
    ON rooms(name COLLATE NOCASE) WHERE is_direct = 0;
CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room_id, id);
CREATE INDEX IF NOT EXISTS idx_members_user ON room_members(user_id);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);
CREATE INDEX IF NOT EXISTS idx_keys_recipient ON room_keys(recipient_id);
CREATE INDEX IF NOT EXISTS idx_reactions_message ON reactions(message_id);
CREATE INDEX IF NOT EXISTS idx_pinned_room ON pinned_messages(room_id);
CREATE INDEX IF NOT EXISTS idx_scheduled_send_at ON scheduled_messages(send_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_room ON scheduled_messages(room_id, user_id);
CREATE INDEX IF NOT EXISTS idx_poll_votes_message ON poll_votes(message_id);
CREATE INDEX IF NOT EXISTS idx_stickers_pack ON stickers(pack_id);
CREATE INDEX IF NOT EXISTS idx_packs_name ON sticker_packs(name);
CREATE INDEX IF NOT EXISTS idx_installed_user ON installed_packs(user_id);
"""


def init() -> None:
    have_users = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if have_users and version != SCHEMA_VERSION:
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"База {DB_PATH} на схеме {version} — новее, чем понимает эта версия сервера "
                f"({SCHEMA_VERSION}). Это старая версия сервера поверх новой базы — обновите сервер.")
        if version < MIN_MIGRATABLE_VERSION:
            raise RuntimeError(
                f"База {DB_PATH} на схеме {version} — это слишком старая версия, инкрементальная миграция "
                f"с нею не написана (самая ранняя поддерживаемая — {MIN_MIGRATABLE_VERSION}). "
                "Удалите базу вместе с папкой uploads/ или укажите другой CHAT_DB.")
        _migrate(version)
    db.executescript(_TABLES)
    db.executescript(_INDEXES)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.commit()


def _table_has_column(table: str, column: str) -> bool:
    return any(r["name"] == column for r in db.execute(f"PRAGMA table_info({table})").fetchall())


def _add_column_if_missing(table: str, column: str, decl: str) -> None:
    if not _table_has_column(table, column):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate(from_version: int) -> None:
    """Пошагово доводит базу со старой схемы до текущей, не теряя данные. Каждый шаг идемпотентен —
    если процесс упадёт посередине, повторный запуск просто продолжит с той же точки (PRAGMA user_version
    поднимается после каждого шага отдельно, а не одним махом в конце). Перед первым шагом снимается
    резервная копия файла базы — на случай, если что-то в самой миграции пойдёт не так."""
    backup = f"{DB_PATH}.backup-v{from_version}"
    if not os.path.exists(backup):
        db.commit()  # чтобы в копии не было незакоммиченных изменений
        shutil.copy2(DB_PATH, backup)
        log.info("Резервная копия базы перед миграцией схемы: %s", backup)

    v = from_version

    if v < 8:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS scheduled_messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            ct TEXT NOT NULL,
            sig TEXT NOT NULL,
            send_at REAL NOT NULL,
            created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scheduled_send_at ON scheduled_messages(send_at);
        CREATE INDEX IF NOT EXISTS idx_scheduled_room ON scheduled_messages(room_id, user_id);
        """)
        db.execute("PRAGMA user_version = 8")
        db.commit()
        v = 8
        log.info("База обновлена до схемы 8 (отложенная отправка)")

    if v < 9:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS poll_votes(
            room_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            ct TEXT NOT NULL,
            sig TEXT NOT NULL,
            ts REAL NOT NULL,
            PRIMARY KEY(message_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_poll_votes_message ON poll_votes(message_id);
        """)
        db.execute("PRAGMA user_version = 9")
        db.commit()
        v = 9
        log.info("База обновлена до схемы 9 (опросы)")

    if v < 10:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS sticker_packs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            creator_id INTEGER NOT NULL,
            creator_username TEXT NOT NULL,
            created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stickers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pack_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            position INTEGER NOT NULL,
            created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS installed_packs(
            user_id INTEGER NOT NULL,
            pack_id INTEGER NOT NULL,
            installed_at REAL NOT NULL,
            PRIMARY KEY(user_id, pack_id)
        );
        CREATE INDEX IF NOT EXISTS idx_stickers_pack ON stickers(pack_id);
        CREATE INDEX IF NOT EXISTS idx_packs_name ON sticker_packs(name);
        CREATE INDEX IF NOT EXISTS idx_installed_user ON installed_packs(user_id);
        """)
        db.execute("PRAGMA user_version = 10")
        db.commit()
        v = 10
        log.info("База обновлена до схемы 10 (каталог стикеров)")

    if v < 11:
        _add_column_if_missing("rooms", "kind", "TEXT NOT NULL DEFAULT 'group'")
        _add_column_if_missing("room_members", "role", "TEXT NOT NULL DEFAULT 'member'")
        # точного создателя старая схема не хранила — лучшее восстановимое приближение: первый вступивший
        # (по rowid) в каждой группе/канале становится владельцем задним числом; в личных чатах роль ни на
        # что не влияет, поэтому их не трогаем
        db.execute("""
            UPDATE room_members SET role = 'owner'
            WHERE role = 'member' AND rowid IN (
                SELECT MIN(rm.rowid) FROM room_members rm
                JOIN rooms r ON r.id = rm.room_id
                WHERE r.is_direct = 0
                GROUP BY rm.room_id
            )
        """)
        db.execute("PRAGMA user_version = 11")
        db.commit()
        v = 11
        log.info("База обновлена до схемы 11 (роли участников, каналы) — "
                 "самый ранний участник каждой группы стал её владельцем")

    if v < 12:
        # раньше при первом запуске сервер сам заводил общий чат для всех — id=1, is_direct=0, имя 'Общий'.
        # Эта проверка по всем трём полям сразу — чтобы случайно не задеть комнату, которую кто-то создал
        # сам и просто назвал так же.
        row = db.execute(
            "SELECT id FROM rooms WHERE id = 1 AND is_direct = 0 AND name = 'Общий'").fetchone()
        if row is not None:
            for table in ("messages", "room_members", "room_keys", "reactions", "pinned_messages",
                         "scheduled_messages", "poll_votes"):
                db.execute(f"DELETE FROM {table} WHERE room_id = 1")
            db.execute("DELETE FROM rooms WHERE id = 1")
            log.info("Автосоздаваемый общий чат (id=1) удалён вместе со всей его историей")
        db.execute("PRAGMA user_version = 12")
        db.commit()
        v = 12
        log.info("База обновлена до схемы 12 (общий чат для всех больше не создаётся)")

    if v < 13:
        _add_column_if_missing("users", "avatar", "TEXT")
        db.execute("PRAGMA user_version = 13")
        db.commit()
        v = 13
        log.info("База обновлена до схемы 13 (аватары)")

    assert v == SCHEMA_VERSION, f"миграция довела базу только до версии {v}, а нужно {SCHEMA_VERSION} — забыли шаг?"
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.commit()


init()
