#!/usr/bin/env bash
# Все проверки проекта. Нужны: node 20+, python3 с пакетом cryptography, JDK 17+.
set -euo pipefail
cd "$(dirname "$0")"
bash e2ee/run.sh
echo "== 3b. Запуск только по HTTPS (сертификаты, перенаправление, заголовки)"
timeout 300 python3 server/tls_test.py 2>&1 < /dev/null | grep -v firebase
echo "== 3c. Скрипт установки на Debian (без root/apt/сети — только то, что тестируется без них)"
python3 server/install_script_test.py
echo "== 4. Раздача установщиков (сервер)"
python3 server/downloads_test.py 2>&1 | grep -v firebase
echo "== 4b. Ответы, редактирование, удаление сообщений (сервер)"
python3 server/edit_delete_test.py 2>&1 | grep -v firebase
echo "== 4c. Публичный профиль (био) — приватность отдельно от ключей (сервер)"
python3 server/profile_test.py 2>&1 | grep -v firebase
echo "== 4d. Реакции, закреплённые сообщения, самоудаление (сервер)"
python3 server/social_test.py 2>&1 | grep -v firebase
echo "== 4e. Галочки «доставлено/прочитано» (сервер)"
python3 server/read_state_test.py 2>&1 | grep -v firebase
echo "== 4f. Отложенная отправка (сервер)"
python3 server/schedule_test.py 2>&1 | grep -v firebase
echo "== 4g. Опросы: голоса (сервер)"
python3 server/poll_test.py 2>&1 | grep -v firebase
echo "== 4h. Групповые звонки: сигналинг (сервер)"
python3 server/group_call_test.py 2>&1 | grep -v firebase
echo "== 4i. Каталог стикеров (сервер)"
python3 server/stickers_test.py 2>&1 | grep -v firebase
echo "== 4j. Роли участников и каналы (сервер)"
python3 server/channels_test.py 2>&1 | grep -v firebase
echo "== 4k. Миграция схемы БД со старых версий (без потери данных)"
python3 server/migration_test.py 2>&1 | grep -v firebase
echo "== 4l. Аватары (сервер)"
python3 server/avatar_test.py 2>&1 | grep -v firebase
echo "== 4m. Распознавание речи (сервер, без реального faster-whisper)"
python3 server/speech_test.py 2>&1 | grep -v firebase
echo "== 5. Блок скачивания на странице входа"
node --test web/downloads_test.mjs 2>&1 | grep -E "^# (tests|pass|fail)"
node web/browser_test.mjs   # настоящий Chromium; без него проверка пропускается
echo "== 5b. Интерфейс чата (аватары, группировка сообщений, поиск, иконки) — настоящий Chromium"
node web/chat_ui_test.mjs
echo "== 5c. Ответы, редактирование, удаление, файлы, черновики — настоящий Chromium"
node web/messaging_test.mjs
echo "== 5d. Собственный QR-энкодер (qr.js) — раундтрип-проверка"
node web/qr_test.mjs
echo "== 5g. Групповые звонки: mesh-сигналинг в браузере (RTCPeerConnection подменён)"
node web/group_call_test.mjs
echo "== 5h. Каталог стикеров: лоток, поиск, права автора — настоящий Chromium"
node web/stickers_test.mjs
echo "== 5f. Реакции, пересылка, закреплённые сообщения, самоудаление — настоящий Chromium"
node web/social_test.mjs
echo "== 5e. Публичный профиль, псевдонимы, QR «добавить контакт» — настоящий Chromium"
node web/profile_test.mjs
echo "== 6. Настольный клиент (Electron подменён заглушкой)"
if [ -d ../desktop ]; then
  (cd ../desktop && node --test 2>&1 | grep -E "^# (tests|pass|fail)")
else
  echo "  каталог desktop/ не найден рядом (это архив только сервера) — пропущено"
fi
echo "== Всё прошло"
