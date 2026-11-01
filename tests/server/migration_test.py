"""Миграция схемы БД со старых версий на текущую — без потери данных.
Собирает настоящую базу v7 руками (по той схеме, что была на v7 — до scheduled_messages/poll_votes/
стикеров/ролей), кладёт в неё реальные данные и проверяет: после db.init() ничего не потеряно, все
новые таблицы/колонки на месте, снята резервная копия, а слишком старая/слишком новая база отклоняется."""
import importlib
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "server"))

_tmp = tempfile.mkdtemp()


def fresh_db_path(name):
    return os.path.join(_tmp, name)


# ---------- строим базу ровно по схеме v7 (до scheduled_messages/poll_votes/стикеров/ролей) ----------
V7_SCHEMA = """
CREATE TABLE users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL, pw_salt TEXT NOT NULL,
    dh_pub TEXT NOT NULL, sig_pub TEXT NOT NULL, key_blob TEXT NOT NULL,
    bio TEXT NOT NULL DEFAULT ''
);
CREATE TABLE tokens(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE rooms(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, is_direct INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL, key_epoch INTEGER NOT NULL DEFAULT 0, autodelete_seconds INTEGER
);
CREATE TABLE room_members(
    room_id INTEGER NOT NULL, user_id INTEGER NOT NULL, last_read_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(room_id, user_id)
);
CREATE TABLE room_keys(
    room_id INTEGER NOT NULL, epoch INTEGER NOT NULL, recipient_id INTEGER NOT NULL,
    eph TEXT NOT NULL, n TEXT NOT NULL, c TEXT NOT NULL, sig TEXT NOT NULL,
    PRIMARY KEY(room_id, epoch, recipient_id)
);
CREATE TABLE messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    username TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
    epoch INTEGER, ct TEXT, sig TEXT, media TEXT, ts REAL NOT NULL, edited_ts REAL
);
CREATE TABLE hidden_messages(user_id INTEGER NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY(user_id, message_id));
CREATE TABLE devices(fcm_token TEXT PRIMARY KEY, user_id INTEGER NOT NULL);
CREATE TABLE reactions(
    room_id INTEGER NOT NULL, message_id INTEGER NOT NULL, user_id INTEGER NOT NULL, username TEXT NOT NULL,
    epoch INTEGER NOT NULL, ct TEXT NOT NULL, sig TEXT NOT NULL, PRIMARY KEY(message_id, user_id)
);
CREATE TABLE pinned_messages(
    room_id INTEGER NOT NULL, message_id INTEGER NOT NULL, pinned_by TEXT NOT NULL, pinned_at REAL NOT NULL,
    PRIMARY KEY(room_id, message_id)
);
"""


def build_v7_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(V7_SCHEMA)
    # реальные данные: два пользователя, групповая комната с обоими, сообщение, реакция, закреп
    conn.execute("INSERT INTO users(id, username, pw_hash, pw_salt, dh_pub, sig_pub, key_blob) "
                "VALUES(1, 'alice', 'h', 's', 'd', 'g', '{}')")
    conn.execute("INSERT INTO users(id, username, pw_hash, pw_salt, dh_pub, sig_pub, key_blob) "
                "VALUES(2, 'bob', 'h', 's', 'd', 'g', '{}')")
    conn.execute("INSERT INTO rooms(id, name, is_direct, created, key_epoch) VALUES(5, 'Древняя группа', 0, 100.0, 1)")
    conn.execute("INSERT INTO room_members(room_id, user_id, last_read_id) VALUES(5, 1, 0)")
    conn.execute("INSERT INTO room_members(room_id, user_id, last_read_id) VALUES(5, 2, 0)")
    conn.execute("INSERT INTO messages(id, room_id, user_id, username, kind, epoch, ct, sig, ts) "
                "VALUES(42, 5, 1, 'alice', 'e2e', 1, 'CIPHERTEXT', 'SIG', 200.0)")
    conn.execute("INSERT INTO reactions(room_id, message_id, user_id, username, epoch, ct, sig) "
                "VALUES(5, 42, 2, 'bob', 1, 'RCT', 'RSIG')")
    conn.execute("INSERT INTO pinned_messages(room_id, message_id, pinned_by, pinned_at) VALUES(5, 42, 'alice', 210.0)")
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()


def load_db_module(path):
    """Каждый раз — новый экземпляр модуля db (он выполняет init() при импорте, а singleton-соединение
    иначе расшарился бы между проверками)."""
    os.environ["CHAT_DB"] = path
    for name in [m for m in sys.modules if m == "db"]:
        del sys.modules[name]
    return importlib.import_module("db")


# ---------- миграция с v7 через весь путь до текущей версии, данные целы ----------
p7 = fresh_db_path("v7.db")
build_v7_db(p7)
db = load_db_module(p7)
assert db.db.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
print(f"MIGRATE: с v7 до v{db.SCHEMA_VERSION} — миграция прошла без ошибок — OK")

users = {r["username"]: r["id"] for r in db.db.execute("SELECT id, username FROM users").fetchall()}
assert users == {"alice": 1, "bob": 2}, "пользователи должны сохраниться как были"
room = db.db.execute("SELECT * FROM rooms WHERE id = 5").fetchone()
assert room["name"] == "Древняя группа" and room["kind"] == "group", "старая группа получает kind='group'"
msg = db.db.execute("SELECT * FROM messages WHERE id = 42").fetchone()
assert msg["ct"] == "CIPHERTEXT" and msg["username"] == "alice", "сообщения (шифртекст) не теряются"
reaction = db.db.execute("SELECT * FROM reactions WHERE message_id = 42").fetchone()
assert reaction["ct"] == "RCT", "реакции не теряются"
pin = db.db.execute("SELECT * FROM pinned_messages WHERE message_id = 42").fetchone()
assert pin["pinned_by"] == "alice", "закреплённые сообщения не теряются"
print("MIGRATE: данные (пользователи, сообщения, реакции, закреп) целы после миграции — OK")

roles = {r["user_id"]: r["role"] for r in db.db.execute("SELECT user_id, role FROM room_members WHERE room_id = 5").fetchall()}
assert roles == {1: "owner", 2: "member"}, f"первый вступивший (alice) должен стать владельцем задним числом: {roles}"
print("MIGRATE: у старой группы задним числом появился владелец (первый вступивший участник) — OK")

for table in ("scheduled_messages", "poll_votes", "sticker_packs", "stickers", "installed_packs"):
    db.db.execute(f"SELECT 1 FROM {table}")  # не бросает — таблица существует
print("MIGRATE: новые таблицы (отложенные, опросы, стикеры) созданы — OK")

assert os.path.exists(f"{p7}.backup-v7"), "перед миграцией должна остаться резервная копия исходного файла"
backup_conn = sqlite3.connect(f"{p7}.backup-v7")
assert backup_conn.execute("PRAGMA user_version").fetchone()[0] == 7, "в резервной копии должна остаться старая версия"
backup_conn.close()
print("MIGRATE: резервная копия базы снята перед миграцией, в ней — исходная (немигрированная) схема — OK")

# ---------- повторный запуск init() после уже состоявшейся миграции — идемпотентно, не падает и не дублирует ----------
db2 = load_db_module(p7)
assert db2.db.execute("PRAGMA user_version").fetchone()[0] == db2.SCHEMA_VERSION
roles2 = {r["user_id"]: r["role"] for r in db2.db.execute("SELECT user_id, role FROM room_members WHERE room_id = 5").fetchall()}
assert roles2 == roles, "повторный запуск после миграции не должен ничего менять"
print("MIGRATE: повторный запуск на уже мигрированной базе — идемпотентно, ничего не портит — OK")

# ---------- частая ситуация: миграция ровно с предыдущей версии (v10 -> v11), без прыжка через несколько ----------
p10 = fresh_db_path("v10.db")
build_v7_db(p10)
conn = sqlite3.connect(p10)
conn.execute("PRAGMA user_version = 10")
conn.executescript("""
CREATE TABLE scheduled_messages(id INTEGER PRIMARY KEY, room_id INTEGER, user_id INTEGER, username TEXT,
    epoch INTEGER, ct TEXT, sig TEXT, send_at REAL, created REAL);
CREATE TABLE poll_votes(room_id INTEGER, message_id INTEGER, user_id INTEGER, username TEXT,
    epoch INTEGER, ct TEXT, sig TEXT, ts REAL, PRIMARY KEY(message_id, user_id));
CREATE TABLE sticker_packs(id INTEGER PRIMARY KEY, name TEXT, creator_id INTEGER, creator_username TEXT, created REAL);
CREATE TABLE stickers(id INTEGER PRIMARY KEY, pack_id INTEGER, filename TEXT, position INTEGER, created REAL);
CREATE TABLE installed_packs(user_id INTEGER, pack_id INTEGER, installed_at REAL, PRIMARY KEY(user_id, pack_id));
""")
conn.commit(); conn.close()
db10 = load_db_module(p10)
assert db10.db.execute("PRAGMA user_version").fetchone()[0] == db10.SCHEMA_VERSION
assert db10.db.execute("SELECT username FROM users WHERE id = 1").fetchone()["username"] == "alice"
assert {r["user_id"]: r["role"] for r in db10.db.execute(
    "SELECT user_id, role FROM room_members WHERE room_id = 5").fetchall()} == {1: "owner", 2: "member"}
print("MIGRATE: миграция ровно на один шаг (v10 -> v11, самый частый в жизни случай) — тоже без потерь — OK")

# ---------- слишком старая (до v7) и слишком новая база отклоняются, а не молча ломаются ----------
p_ancient = fresh_db_path("ancient.db")
conn = sqlite3.connect(p_ancient)
conn.execute("CREATE TABLE users(id INTEGER, username TEXT)")
conn.execute("PRAGMA user_version = 3")
conn.commit(); conn.close()
try:
    load_db_module(p_ancient)
    raise AssertionError("ожидали RuntimeError для базы младше MIN_MIGRATABLE_VERSION")
except RuntimeError as e:
    assert "слишком старая версия" in str(e)
print("MIGRATE: база младше поддерживаемой версии — явный отказ, а не тихая порча данных — OK")

p_future = fresh_db_path("future.db")
conn = sqlite3.connect(p_future)
conn.execute("CREATE TABLE users(id INTEGER, username TEXT)")
conn.execute("PRAGMA user_version = 999")
conn.commit(); conn.close()
try:
    load_db_module(p_future)
    raise AssertionError("ожидали RuntimeError для базы новее текущей версии сервера")
except RuntimeError as e:
    assert "новее" in str(e)
print("MIGRATE: база новее текущей схемы (старый сервер поверх новой базы) — явный отказ — OK")

# ---------- v11 -> v12: автосоздаваемый общий чат убирается вместе со своей историей ----------
p11 = fresh_db_path("v11.db")
build_v7_db(p11)
conn = sqlite3.connect(p11)
conn.execute("PRAGMA user_version = 11")
conn.executescript("""
CREATE TABLE scheduled_messages(id INTEGER PRIMARY KEY, room_id INTEGER, user_id INTEGER, username TEXT,
    epoch INTEGER, ct TEXT, sig TEXT, send_at REAL, created REAL);
CREATE TABLE poll_votes(room_id INTEGER, message_id INTEGER, user_id INTEGER, username TEXT,
    epoch INTEGER, ct TEXT, sig TEXT, ts REAL, PRIMARY KEY(message_id, user_id));
CREATE TABLE sticker_packs(id INTEGER PRIMARY KEY, name TEXT, creator_id INTEGER, creator_username TEXT, created REAL);
CREATE TABLE stickers(id INTEGER PRIMARY KEY, pack_id INTEGER, filename TEXT, position INTEGER, created REAL);
CREATE TABLE installed_packs(user_id INTEGER, pack_id INTEGER, installed_at REAL, PRIMARY KEY(user_id, pack_id));
""")
conn.execute("ALTER TABLE rooms ADD COLUMN kind TEXT NOT NULL DEFAULT 'group'")
conn.execute("ALTER TABLE room_members ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
# id=1 — тот самый автосоздаваемый общий чат, с историей внутри; id=5 из build_v7_db — обычная группа,
# её мы не трогаем вовсе (это как раз "Древняя группа", а не "Общий")
conn.execute("INSERT INTO rooms(id, name, is_direct, created, kind) VALUES(1, 'Общий', 0, 50.0, 'group')")
conn.execute("INSERT INTO room_members(room_id, user_id, role) VALUES(1, 1, 'member'), (1, 2, 'member')")
conn.execute("INSERT INTO messages(id, room_id, user_id, username, kind, epoch, ct, sig, ts) "
            "VALUES(99, 1, 2, 'bob', 'e2e', 1, 'HELLO', 'SIG2', 60.0)")
# а вот эту комнату кто-то создал и НАЗВАЛ САМ так же, как автосоздаваемая, но is_direct тут ни при чём —
# отличается id (не 1) — её удалять нельзя
conn.execute("INSERT INTO rooms(id, name, is_direct, created, kind) VALUES(6, 'Общий', 0, 70.0, 'group')")
conn.execute("INSERT INTO room_members(room_id, user_id, role) VALUES(6, 1, 'owner')")
conn.commit(); conn.close()

db11 = load_db_module(p11)
assert db11.db.execute("PRAGMA user_version").fetchone()[0] == db11.SCHEMA_VERSION
assert db11.db.execute("SELECT 1 FROM rooms WHERE id = 1").fetchone() is None, \
    "автосоздаваемый общий чат (id=1) должен быть удалён при миграции"
assert db11.db.execute("SELECT 1 FROM messages WHERE room_id = 1").fetchone() is None, \
    "история удалённого общего чата тоже должна быть удалена, а не повиснуть сиротами"
assert db11.db.execute("SELECT 1 FROM room_members WHERE room_id = 1").fetchone() is None
assert db11.db.execute("SELECT name FROM rooms WHERE id = 6").fetchone()["name"] == "Общий", \
    "а вот комнату, которую пользователь сам создал и назвал так же, трогать нельзя — она не id=1"
assert db11.db.execute("SELECT username FROM users WHERE id = 1").fetchone()["username"] == "alice", \
    "остальные данные (пользователи) миграция удаления общего чата не должна была затронуть"
print("MIGRATE: автосоздаваемый общий чат (id=1) удаляется вместе с историей; "
     "одноимённая комната, созданная пользователем, не трогается — OK")

# отдельно: если id=1 существует, но называется НЕ 'Общий' (в упорядоченной автоинкрементом базе такого
# по построению не бывает, но проверяем явно, а не полагаемся на одну лишь удачу) — трогать её нельзя
p11b = fresh_db_path("v11b.db")
build_v7_db(p11b)
conn = sqlite3.connect(p11b)
conn.execute("PRAGMA user_version = 11")
conn.executescript("""
CREATE TABLE scheduled_messages(id INTEGER PRIMARY KEY, room_id INTEGER, user_id INTEGER, username TEXT,
    epoch INTEGER, ct TEXT, sig TEXT, send_at REAL, created REAL);
CREATE TABLE poll_votes(room_id INTEGER, message_id INTEGER, user_id INTEGER, username TEXT,
    epoch INTEGER, ct TEXT, sig TEXT, ts REAL, PRIMARY KEY(message_id, user_id));
CREATE TABLE sticker_packs(id INTEGER PRIMARY KEY, name TEXT, creator_id INTEGER, creator_username TEXT, created REAL);
CREATE TABLE stickers(id INTEGER PRIMARY KEY, pack_id INTEGER, filename TEXT, position INTEGER, created REAL);
CREATE TABLE installed_packs(user_id INTEGER, pack_id INTEGER, installed_at REAL, PRIMARY KEY(user_id, pack_id));
""")
conn.execute("ALTER TABLE rooms ADD COLUMN kind TEXT NOT NULL DEFAULT 'group'")
conn.execute("ALTER TABLE room_members ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
conn.execute("INSERT INTO rooms(id, name, is_direct, created, kind) VALUES(1, 'Проект Х', 0, 50.0, 'group')")
conn.execute("INSERT INTO room_members(room_id, user_id, role) VALUES(1, 1, 'owner')")
conn.execute("INSERT INTO messages(id, room_id, user_id, username, kind, epoch, ct, sig, ts) "
            "VALUES(77, 1, 1, 'alice', 'e2e', 1, 'IMPORTANT', 'SIG3', 60.0)")
conn.commit(); conn.close()

db11b = load_db_module(p11b)
assert db11b.db.execute("SELECT name FROM rooms WHERE id = 1").fetchone()["name"] == "Проект Х", \
    "id=1, но не 'Общий' — это чья-то настоящая комната, трогать нельзя ни в коем случае"
assert db11b.db.execute("SELECT 1 FROM messages WHERE room_id = 1").fetchone() is not None, \
    "и её историю тоже нельзя было стереть"
print("MIGRATE: id=1 с другим названием (не автосоздаваемый общий чат) миграция не трогает — OK")

shutil.rmtree(_tmp, ignore_errors=True)
