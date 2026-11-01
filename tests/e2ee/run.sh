#!/usr/bin/env bash
# Проверки E2EE: нужны node 20+, python3 с пакетом cryptography и JDK 17+.
# Запуск:  bash tests/e2ee/run.sh
set -euo pipefail
cd "$(dirname "$0")"
echo "== 1. Совместимость JS (WebCrypto) <-> Python <-> Java (JCA)"
node interop_js1.mjs
python3 interop_py.py
node interop_js2.mjs
python3 make_java_input.py
java J.java
python3 interop_java_py.py
node interop_java_check.mjs
echo "== 2. Логика сервера (обработчики main.py, FastAPI подменён заглушкой)"
rm -f /tmp/srv.db /tmp/old.db; rm -rf /tmp/srv_up
python3 srv_test.py 2>&1 | grep -v firebase
echo "== 3. Интеграция: настоящий JS-клиент против настоящей логики сервера"
rm -f /tmp/int.db; rm -rf /tmp/int_up
python3 int_server.py >/tmp/int_server.log 2>&1 &
SRV=$!; trap 'kill $SRV 2>/dev/null || true' EXIT
sleep 1.5
node int_client.mjs
echo "== Всё прошло"
