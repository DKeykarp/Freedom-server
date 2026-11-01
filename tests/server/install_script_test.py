"""Проверки install-debian.sh, не требующие root/apt/сети: синтаксис, справка, аргументы, содержимое юнита.

Полный прогон (создание пользователя, apt, systemd) здесь не тестируется — для этого нужен root на
настоящем Debian/Ubuntu с сетью; см. testing-заметку в конце README. Эти проверки ловят то, что чаще всего
реально ломается: синтаксические ошибки после правок, сдвинувшийся при редактировании usage(), опечатки
в путях systemd-юнита.
"""
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "server" / "install-debian.sh"


def run(*args, check=False):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


assert SCRIPT.is_file(), SCRIPT
assert SCRIPT.stat().st_mode & 0o111, "install-debian.sh должен быть исполняемым (chmod +x)"

r = sh(["bash", "-n", str(SCRIPT)])
assert r.returncode == 0, r.stderr
print("SH: синтаксис install-debian.sh — OK")

r = run("--help")
assert r.returncode == 0
assert "install-debian.sh" in r.stdout and "--uninstall" in r.stdout and "--no-turn" in r.stdout
assert "--with-turn" not in r.stdout, "флаг переименован в --no-turn (TURN теперь ставится по умолчанию)"
# usage() выводит шапку комментариев до первой не-комментарийной строки — регрессия на сдвиг номеров строк
assert "set -euo pipefail" not in r.stdout and "HOST=" not in r.stdout, \
    "usage() зацепил код скрипта — вероятно, кто-то вернул sed по номерам строк"
print("SH: --help выводит только шапку-описание, без утечки кода — OK")

r = run("--bogus-flag")
assert r.returncode == 2 and "Неизвестный параметр" in r.stderr
print("SH: неизвестный параметр — понятная ошибка, код возврата 2 — OK")

r = run("--source", "/nonexistent-dir-xyz")
assert r.returncode == 1 and "не похож на каталог server/" in r.stderr
print("SH: отсутствующий --source — понятная ошибка — OK")

# запускаем без root там, где это возможно (в контейнерах сборки часто есть 'nobody')
r = sh(["su", "-s", "/bin/bash", "nobody", "-c", f"bash {SCRIPT}"])
if "no such user" not in (r.stderr + r.stdout).lower():
    assert r.returncode == 1 and "root" in (r.stdout + r.stderr).lower()
    print("SH: без root — отказ с понятным сообщением — OK")

src = SCRIPT.read_text(encoding="utf-8")
# runuser, а не sudo: на минимальной установке Debian sudo может отсутствовать, runuser есть всегда (util-linux)
assert "sudo -u" not in src, "используйте runuser -u, а не sudo -u (sudo не гарантирован на чистом Debian)"
assert re.search(r"^runuser -u \"\$SERVICE_USER\" -- python3 -m venv", src, re.M)
print("SH: venv и pip создаются через runuser, не через sudo — OK")

# ключевые свойства юнита systemd (песочница, привязка портов без root, изоляция) должны остаться на месте
for needle in ("AmbientCapabilities=CAP_NET_BIND_SERVICE", "ProtectSystem=strict", "NoNewPrivileges=true",
              "ReadWritePaths=$INSTALL_DIR/data $INSTALL_DIR/certs", "Restart=on-failure",
              "ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/serve.py"):
    assert needle in src, f"пропало из unit-файла: {needle}"
print("SH: юнит systemd сохраняет непривилегированный запуск и песочницу — OK")

# HOST по умолчанию должен совпадать с адресом, зашитым в клиентах (Android/десктоп) — только если их исходники
# рядом: сервер теперь поставляется отдельным архивом от клиентов, и там их попросту не будет.
android_path = Path(__file__).resolve().parents[2] / "app/src/main/java/com/example/chat/ApiClient.kt"
desktop_path = Path(__file__).resolve().parents[2] / "desktop/src/app-config.js"
if android_path.is_file() and desktop_path.is_file():
    android = android_path.read_text(encoding="utf-8")
    desktop = desktop_path.read_text(encoding="utf-8")
    m = re.search(r'HOST="([^"]+)"', src)
    assert m and m.group(1) in android and m.group(1) in desktop, \
        "HOST по умолчанию в install-debian.sh разошёлся с адресом, зашитым в клиентах"
    print(f"SH: HOST по умолчанию ({m.group(1)}) совпадает с адресом в Android и десктоп-клиенте — OK")
else:
    print("SH: исходники клиентов рядом не найдены (это архив только сервера) — сверка HOST пропущена")

# Поведенческая проверка: этот sandbox гарантированно без systemd (нет /run/systemd/system) — значит
# HAS_SYSTEMD=0, и весь путь uninstall обязан пройти без падения `set -e` на недоступном systemctl.
assert not Path("/run/systemd/system").is_dir(), "тест ожидает песочницу без systemd для этой проверки"
r = run("--uninstall", "--user", "no-such-user-abcxyz", "--dir", "/tmp/no-such-dir-abcxyz")
assert r.returncode == 0, f"uninstall упал при недоступном systemd: {r.returncode}\n{r.stderr}"
assert "недоступен" in r.stderr and "не могу" in r.stderr
print("SH: --uninstall не падает, когда systemd недоступен (только предупреждает) — OK")

# --- TURN (coturn): по умолчанию включён, и запускается как отдельная служба systemd ---
assert 'NO_TURN=0' in src, "TURN должен быть включён по умолчанию (NO_TURN=0), опция отказа — --no-turn"
assert "apt-get install -y --no-install-recommends coturn" in src
assert "systemctl restart coturn" in src and 'systemctl is-active --quiet coturn' in src, \
    "coturn должен не только ставиться, но и реально запускаться со проверкой статуса — как freedom.service"
assert "journalctl -u coturn" in src
print("SH: coturn ставится по умолчанию и запускается/проверяется как служба systemd — OK")

# секрет TURN должен переживать повторные запуски (иначе каждый `install-debian.sh` менял бы его без нужды)
assert re.search(r'OLD_TURN_SECRET=.*grep.*TURN_SECRET=', src)
assert re.search(r'TURN_SECRET=\$\{?TURN_SECRET:-\$\{?OLD_TURN_SECRET|TURN_SECRET="\$\{TURN_SECRET:-\$OLD_TURN_SECRET', src) or \
       "OLD_TURN_SECRET:-$(openssl" in src, "секрет TURN должен переиспользоваться между запусками (OLD_TURN_SECRET)"
print("SH: секрет TURN переиспользуется между переустановками, а не генерируется заново — OK")

# environment: старые TURN_URLS/TURN_SECRET сначала вычищаются, а не бесконтрольно дописываются в конец файла
assert re.search(r"grep -vE .\^\(TURN_URLS\|TURN_SECRET\)=. .*ENV_FILE", src), \
    "повторные запуски не должны копить дублирующиеся строки TURN_* в файле окружения"
print("SH: файл окружения не копит дубли TURN_URLS/TURN_SECRET при переустановке — OK")

# --- Регрессия конкретно на баг, который здесь реально был найден и исправлен: определение внешнего IP
# (curl/ip) — необязательный шаг, и его сетевая/утилитная ошибка не должна прерывать всю установку через
# `set -e`. Прогоняем именно этот фрагмент скрипта с заведомо падающими curl/ip и проверяем код возврата 0.
block_start = src.index("# ---------- TURN (coturn)")
block_end = src.index('log "coturn настроен')
turn_block = src[block_start:block_end] + 'fi\n'
harness = (
    "set -euo pipefail\n"
    "INSTALL_DIR=$1; SERVICE_USER=$2; HOST=test.example.com; HAS_SYSTEMD=0; NO_TURN=0; TURN_SECRET=\n"
    "log()  { :; }\nwarn() { :; }\n" + turn_block
)
tmp = Path("/tmp/_frdm_turn_block_test.sh")
tmp.write_text(harness, encoding="utf-8")
fakebin = Path("/tmp/_frdm_fakebin")
fakebin.mkdir(exist_ok=True)
(fakebin / "curl").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
(fakebin / "ip").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
for f in ("curl", "ip"):
    (fakebin / f).chmod(0o755)
test_install = Path("/tmp/_frdm_turn_install")
test_install.mkdir(exist_ok=True)
(test_install / "environment").write_text("", encoding="utf-8")
import os
env = {**os.environ, "PATH": f"{fakebin}:{os.environ.get('PATH', '')}"}
r = subprocess.run(["bash", str(tmp), str(test_install), "root"], capture_output=True, text=True, env=env)
assert r.returncode == 0, (
    f"определение внешнего IP уронило установку при недоступных curl/ip (код {r.returncode}):\n{r.stderr}"
)
assert "unbound variable" not in r.stderr
print("SH: сбой определения внешнего IP (curl/ip недоступны) не прерывает установку — OK")
import shutil
shutil.rmtree(fakebin, ignore_errors=True)
shutil.rmtree(test_install, ignore_errors=True)
tmp.unlink(missing_ok=True)

# --- push-уведомления (Firebase): опция --firebase-credentials, ключ переживает переустановки ---
assert "--firebase-credentials" in src and "FIREBASE_CREDENTIALS_SRC" in src, \
    "должна быть опция --firebase-credentials, задающая путь к ключу сервисного аккаунта"
assert re.search(r"grep -vE .\^FIREBASE_CREDENTIALS=. .*ENV_FILE", src), \
    "повторные запуски не должны копить дублирующиеся строки FIREBASE_CREDENTIALS в файле окружения"
print("SH: --firebase-credentials задокументирован, файл окружения не копит дубли — OK")

fb_start = src.index("# ---------- push-уведомления")
fb_end = src.index("# ---------- распознавание речи")
fb_block = src[fb_start:fb_end]
harness2 = "set -euo pipefail\nINSTALL_DIR=$1; SERVICE_USER=$(id -un); ENV_FILE=$INSTALL_DIR/environment\nlog() { :; }\n" + fb_block
tmp2 = Path("/tmp/_frdm_fb_block_test.sh")
tmp2.write_text(harness2, encoding="utf-8")

# ключ есть — строка (пере)записывается, старое значение и прочие переменные не теряются
test_install2 = Path("/tmp/_frdm_fb_install")
test_install2.mkdir(exist_ok=True)
(test_install2 / "environment").write_text("SOME_OTHER=1\nFIREBASE_CREDENTIALS=/old/stale/path.json\n", encoding="utf-8")
(test_install2 / "firebase-adminsdk.json").write_text("{}", encoding="utf-8")
r = subprocess.run(["bash", str(tmp2), str(test_install2)], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
env_content = (test_install2 / "environment").read_text()
assert env_content.count("FIREBASE_CREDENTIALS=") == 1, f"не должно копиться несколько строк: {env_content!r}"
assert f"FIREBASE_CREDENTIALS={test_install2}/firebase-adminsdk.json" in env_content
assert "SOME_OTHER=1" in env_content, "другие переменные в файле окружения не должны затираться"
print("SH: FIREBASE_CREDENTIALS корректно (пере)записывается, без дублей и без потери других переменных — OK")

# ключа нет вовсе — старая (например, от предыдущей установки с другим --dir) запись вычищается, push выключен
test_install3 = Path("/tmp/_frdm_fb_install_none")
test_install3.mkdir(exist_ok=True)
(test_install3 / "environment").write_text("FIREBASE_CREDENTIALS=/old/path.json\n", encoding="utf-8")
r = subprocess.run(["bash", str(tmp2), str(test_install3)], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
assert "FIREBASE_CREDENTIALS=" not in (test_install3 / "environment").read_text(), \
    "без ключа старая запись должна вычищаться, а не оставаться висеть"
print("SH: без ключа Firebase FIREBASE_CREDENTIALS вычищается из файла окружения (push остаётся выключен) — OK")

import shutil
shutil.rmtree(test_install2, ignore_errors=True)
shutil.rmtree(test_install3, ignore_errors=True)
tmp2.unlink(missing_ok=True)

# --- регрессия: каждая CHAT_*-переменная пути, которую main.py читает через os.getenv, должна быть явно
# выставлена в systemd-юните — иначе сервис уходит в дефолт (часто относительный путь) и падает под
# ProtectSystem=strict с "Read-only file system", как реально было с CHAT_STICKERS (забыл прописать при
# добавлении каталога стикеров) ---
main_src = (Path(__file__).resolve().parents[2] / "server" / "main.py").read_text(encoding="utf-8")
chat_vars = set(re.findall(r'os\.getenv\("(CHAT_[A-Z_]+)"', main_src))
assert "CHAT_STICKERS" in chat_vars, "main.py должен знать про CHAT_STICKERS (тест устарел, если имя изменилось)"
missing = [v for v in sorted(chat_vars) if f"Environment={v}=" not in src]
assert not missing, f"переменные пути, которые main.py читает, но install-debian.sh не выставляет в юните: {missing}"
print("SH: все CHAT_*-переменные путей из main.py выставлены в systemd-юните (регрессия на баг с CHAT_STICKERS) — OK")

# сам каталог тоже должен создаваться заранее (с нужным владельцем/правами), а не только упоминаться в Environment=
assert '"$INSTALL_DIR/data/stickers"' in src, \
    "каталог для CHAT_STICKERS должен создаваться install -d вместе с uploads/downloads"
print("SH: каталог для стикеров создаётся install -d вместе с uploads/downloads — OK")

# --- распознавание речи (--whisper-model): задокументирован, не копит дубли, переживает переустановки без
# повторения флага (та же логика, что у TURN_SECRET — простое строковое значение, не файл) ---
assert "--whisper-model" in src and "WHISPER_MODEL" in src
assert re.search(r"grep -vE .\^WHISPER_MODEL=. .*ENV_FILE", src), \
    "повторные запуски не должны копить дублирующиеся строки WHISPER_MODEL в файле окружения"
print("SH: --whisper-model задокументирован, файл окружения не копит дубли — OK")

wm_start = src.index("# ---------- распознавание речи")
wm_end = src.index('[ "$HAS_SYSTEMD" = 1 ] && systemctl daemon-reload')
wm_block = src[wm_start:wm_end]
harness3 = "set -euo pipefail\nINSTALL_DIR=$1; SERVICE_USER=$(id -un); ENV_FILE=$INSTALL_DIR/environment\nWHISPER_MODEL=${2:-}\nlog() { :; }\n" + wm_block
tmp3 = Path("/tmp/_frdm_wm_block_test.sh")
tmp3.write_text(harness3, encoding="utf-8")

test_install4 = Path("/tmp/_frdm_wm_install")
test_install4.mkdir(exist_ok=True)
(test_install4 / "environment").write_text("SOME_OTHER=1\nWHISPER_MODEL=old-stale-model\n", encoding="utf-8")
r = subprocess.run(["bash", str(tmp3), str(test_install4), "small"], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
env_content = (test_install4 / "environment").read_text()
assert env_content.count("WHISPER_MODEL=") == 1, f"не должно копиться несколько строк: {env_content!r}"
assert "WHISPER_MODEL=small" in env_content
assert "SOME_OTHER=1" in env_content, "другие переменные в файле окружения не должны затираться"
print("SH: WHISPER_MODEL корректно (пере)записывается, без дублей и без потери других переменных — OK")

# без флага при повторном запуске — старое значение сохраняется (как у TURN_SECRET), не стирается
test_install5 = Path("/tmp/_frdm_wm_install_keep")
test_install5.mkdir(exist_ok=True)
(test_install5 / "environment").write_text("WHISPER_MODEL=small\n", encoding="utf-8")
r = subprocess.run(["bash", str(tmp3), str(test_install5), ""], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
assert "WHISPER_MODEL=small" in (test_install5 / "environment").read_text(), \
    "повторный запуск без --whisper-model не должен выключать уже настроенное распознавание речи"
print("SH: WHISPER_MODEL переживает переустановку без повторения флага (как TURN_SECRET) — OK")

import shutil as _shutil
_shutil.rmtree(test_install4, ignore_errors=True)
_shutil.rmtree(test_install5, ignore_errors=True)
tmp3.unlink(missing_ok=True)
