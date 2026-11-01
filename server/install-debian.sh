#!/usr/bin/env bash
# Установка и обновление сервера Freedom на Debian/Ubuntu: системные пакеты, виртуальное окружение
# со всеми зависимостями из requirements.txt, системный пользователь, каталоги данных и служба systemd.
#
#   sudo bash install-debian.sh                    # установка/обновление (по умолчанию: contora.is-local.org)
#   sudo bash install-debian.sh --host chat.example.com
#   sudo bash install-debian.sh --no-turn           # без своего TURN-сервера (только STUN)
#   sudo bash install-debian.sh --firebase-credentials /путь/к/ключу.json   # включить push (или просто
#                                                    # положить firebase-adminsdk.json рядом, в server/)
#   sudo bash install-debian.sh --whisper-model small   # включить распознавание речи (faster-whisper и
#                                                    # ffmpeg ставятся отдельно — см. README §14, по
#                                                    # умолчанию выключено, это не часть обычной установки)
#   sudo bash install-debian.sh --uninstall         # остановить и убрать службу (данные и сертификаты остаются)
#   sudo bash install-debian.sh --uninstall --purge # + удалить данные, сертификаты и системного пользователя
#
# И Freedom, и TURN (coturn) для звонков ставятся и настраиваются автоматически, и оба работают как
# полноценные службы systemd (systemctl status freedom, systemctl status coturn).
# Запускать из каталога server/ распакованного проекта (или указать --source). Повторный запуск безопасен:
# обновляет код и зависимости, не трогает уже загруженные сертификаты, базу данных, файлы и секрет TURN.
set -euo pipefail
umask 022

# ---------- параметры по умолчанию ----------
HOST="contora.is-local.org"        # должен совпадать с адресом, зашитым в клиентах (Android SERVER_URL, десктоп SERVER_ORIGIN)
INSTALL_DIR="/opt/freedom"
SERVICE_USER="freedom"
SERVICE_NAME="freedom"
HTTPS_PORT="443"
HTTP_PORT="80"
SOURCE_DIR=""
NO_TURN=0
TURN_SECRET=""
FIREBASE_CREDENTIALS_SRC=""
WHISPER_MODEL=""
NO_START=0
UNINSTALL=0
PURGE=0
YES=0

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"

usage() {
  # Шапка файла до первой не-комментарийной строки — не завязано на номера строк, переживает правки скрипта.
  awk '/^#!/{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    --https-port) HTTPS_PORT="$2"; shift 2 ;;
    --http-port) HTTP_PORT="$2"; shift 2 ;;
    --source) SOURCE_DIR="$2"; shift 2 ;;
    --no-turn) NO_TURN=1; shift ;;
    --turn-secret) TURN_SECRET="$2"; shift 2 ;;
    --firebase-credentials) FIREBASE_CREDENTIALS_SRC="$2"; shift 2 ;;
    --whisper-model) WHISPER_MODEL="$2"; shift 2 ;;
    --no-start) NO_START=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    -y|--yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Неизвестный параметр: $1" >&2; usage; exit 2 ;;
  esac
done

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "Нужны права root: запустите через sudo."

# systemd бывает недоступен (например, внутри контейнера при сборке образа) — тогда только
# готовим файлы и предупреждаем, но не пытаемся дергать systemctl (упадёт по set -e).
HAS_SYSTEMD=0
[ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1 && HAS_SYSTEMD=1

# ---------- удаление ----------
if [ "$UNINSTALL" = 1 ]; then
  log "Останавливаю и убираю службу $SERVICE_NAME"
  if [ "$HAS_SYSTEMD" = 1 ]; then
    systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "/etc/systemd/system/$SERVICE_NAME.service"
    systemctl daemon-reload 2>/dev/null || true
  else
    rm -f "/etc/systemd/system/$SERVICE_NAME.service"
    warn "systemd сейчас недоступен — юнит удалён, но остановить активную службу не могу"
  fi
  if [ "$PURGE" = 1 ]; then
    if [ "$YES" != 1 ]; then
      read -r -p "Удалить $INSTALL_DIR целиком — базу, загруженные файлы, СЕРТИФИКАТЫ — безвозвратно? [y/N] " a
      case "$a" in y|Y|yes|YES) ;; *) die "Отменено." ;; esac
    fi
    rm -rf "$INSTALL_DIR"
    id -u "$SERVICE_USER" >/dev/null 2>&1 && userdel "$SERVICE_USER" 2>/dev/null || true
    log "Удалено: $INSTALL_DIR, пользователь $SERVICE_USER"
  else
    log "Код, база, файлы и сертификаты оставлены в $INSTALL_DIR (для полного удаления добавьте --purge)"
  fi
  warn "TURN (coturn), если он был настроен, не тронут: это отдельная системная служба. Отключить вручную:"
  warn "  systemctl disable --now coturn"
  exit 0
fi

# ---------- где взять исходники сервера ----------
if [ -z "$SOURCE_DIR" ]; then
  if [ -f "$SCRIPT_DIR/main.py" ]; then
    SOURCE_DIR="$SCRIPT_DIR"                      # обычный случай: скрипт лежит в server/ рядом с main.py
  else
    die "main.py не найден рядом со скриптом. Запустите из каталога server/ проекта или укажите --source <путь>."
  fi
fi
[ -f "$SOURCE_DIR/main.py" ] && [ -f "$SOURCE_DIR/requirements.txt" ] || \
  die "$SOURCE_DIR не похож на каталог server/ проекта Freedom (нет main.py или requirements.txt)."

# ---------- система ----------
[ -r /etc/os-release ] && . /etc/os-release || true
case "${ID:-}:${ID_LIKE:-}" in
  debian:*|*:*debian*|ubuntu:*) : ;;
  *) warn "Похоже, это не Debian/Ubuntu (ID=${ID:-?}). Продолжаю, но пакеты может понадобиться поставить вручную." ;;
esac

ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
log "Система: ${PRETTY_NAME:-неизвестно}, архитектура: $ARCH"

log "Обновляю списки пакетов и ставлю системные зависимости"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# python3-venv/pip — виртуальное окружение; build-essential/python3-dev — на случай, если для какой-то
# зависимости нет готового wheel под эту архитектуру и pip придётся собирать из исходников.
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip python3-dev build-essential \
  ca-certificates openssl >/dev/null
if [ "$NO_TURN" != 1 ]; then
  apt-get install -y --no-install-recommends coturn >/dev/null
fi

# ---------- системный пользователь службы ----------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  log "Создаю системного пользователя $SERVICE_USER (без входа в систему)"
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin --user-group "$SERVICE_USER"
else
  log "Пользователь $SERVICE_USER уже существует"
fi

# ---------- код ----------
FIRST_INSTALL=1
[ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/main.py" ] && FIRST_INSTALL=0

log "Копирую код сервера в $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
# только код и веб-клиент; данные (certs/, downloads/, база, загрузки) — отдельным деревом ниже, их не трогаем
for item in main.py db.py push.py tls.py serve.py requirements.txt web; do
  [ -e "$SOURCE_DIR/$item" ] || continue
  rm -rf "${INSTALL_DIR:?}/$item"
  cp -a "$SOURCE_DIR/$item" "$INSTALL_DIR/$item"
done
find "$INSTALL_DIR" -maxdepth 2 -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

log "Каталоги для данных и сертификатов"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 \
  "$INSTALL_DIR/data" "$INSTALL_DIR/data/uploads" "$INSTALL_DIR/data/downloads" "$INSTALL_DIR/data/stickers" "$INSTALL_DIR/data/avatars" "$INSTALL_DIR/certs"
# то, что уже лежало в исходном certs/ или downloads/ (например, сертификаты, подготовленные заранее) — переносим
for f in "$SOURCE_DIR"/certs/*; do
  case "$(basename "$f")" in .gitignore|README.txt) continue ;; esac
  [ -e "$f" ] && cp -a "$f" "$INSTALL_DIR/certs/" 2>/dev/null || true
done
for f in "$SOURCE_DIR"/downloads/*; do
  case "$(basename "$f")" in .gitignore|README.txt) continue ;; esac
  [ -e "$f" ] && cp -a "$f" "$INSTALL_DIR/data/downloads/" 2>/dev/null || true
done
[ -f "$INSTALL_DIR/certs/private.key" ] && chmod 600 "$INSTALL_DIR/certs/private.key"
# ключ сервисного аккаунта Firebase (push-уведомления, --firebase-credentials или FIREBASE_CREDENTIALS ниже):
# явно указанный путь важнее, но если рядом со скриптом уже лежит firebase-adminsdk.json — подхватываем и его,
# как и с сертификатами выше
[ -z "$FIREBASE_CREDENTIALS_SRC" ] && [ -f "$SOURCE_DIR/firebase-adminsdk.json" ] && \
  FIREBASE_CREDENTIALS_SRC="$SOURCE_DIR/firebase-adminsdk.json"
if [ -n "$FIREBASE_CREDENTIALS_SRC" ]; then
  cp -a "$FIREBASE_CREDENTIALS_SRC" "$INSTALL_DIR/firebase-adminsdk.json"
fi
[ -f "$INSTALL_DIR/firebase-adminsdk.json" ] && chmod 600 "$INSTALL_DIR/firebase-adminsdk.json"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ---------- виртуальное окружение и зависимости ----------
log "Виртуальное окружение и зависимости (requirements.txt)"
runuser -u "$SERVICE_USER" -- python3 -m venv "$INSTALL_DIR/.venv"
runuser -u "$SERVICE_USER" -- "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
runuser -u "$SERVICE_USER" -- "$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ---------- служба systemd ----------
log "Настраиваю службу systemd: $SERVICE_NAME"
cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Freedom — чат со сквозным шифрованием
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment=CHAT_HOST=$HOST
Environment=PORT=$HTTPS_PORT
Environment=HTTP_PORT=$HTTP_PORT
Environment=CHAT_CERT_DIR=$INSTALL_DIR/certs
Environment=CHAT_DB=$INSTALL_DIR/data/freedom.db
Environment=CHAT_UPLOADS=$INSTALL_DIR/data/uploads
Environment=CHAT_DOWNLOADS=$INSTALL_DIR/data/downloads
Environment=CHAT_STICKERS=$INSTALL_DIR/data/stickers
Environment=CHAT_AVATARS=$INSTALL_DIR/data/avatars
EnvironmentFile=-$INSTALL_DIR/environment
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/serve.py

# 80/443 без root: только эта возможность, и только на приём
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true

# песочница: всё дерево кроме data/ и certs/ — только для чтения
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$INSTALL_DIR/data $INSTALL_DIR/certs
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
[ -f "$INSTALL_DIR/environment" ] || runuser -u "$SERVICE_USER" -- touch "$INSTALL_DIR/environment"
chmod 640 "$INSTALL_DIR/environment"; chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/environment"

# ---------- TURN (coturn) — по умолчанию включён, звонки через мобильные сети и строгий NAT без него часто не проходят ----------
ENV_FILE="$INSTALL_DIR/environment"
OLD_TURN_SECRET="$(grep -oP '^TURN_SECRET=\K.*' "$ENV_FILE" 2>/dev/null || true)"
grep -vE '^(TURN_URLS|TURN_SECRET)=' "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || : > "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 640 "$ENV_FILE"; chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"

if [ "$NO_TURN" = 1 ]; then
  log "TURN отключён (--no-turn): звонки будут работать только через публичный STUN"
else
  # секрет переиспользуется между переустановками, если не задан явно — иначе он менялся бы при каждом sudo bash install-debian.sh
  [ -z "$TURN_SECRET" ] && TURN_SECRET="${OLD_TURN_SECRET:-$(openssl rand -hex 24)}"
  {
    echo "TURN_URLS=turn:$HOST:3478?transport=udp,turn:$HOST:3478?transport=tcp"
    echo "TURN_SECRET=$TURN_SECRET"
  } >> "$ENV_FILE"

  # Домашний сервер почти всегда за NAT роутера: coturn должен объявлять публичный IP в relay-кандидатах,
  # иначе для собеседников снаружи локальной сети звонки через TURN не поедут. Определяем оба адреса сами.
  # Определение адресов — необязательный шаг: ошибка сети или отсутствие утилиты не должны прерывать установку.
  set +e
  PRIVATE_IP=""
  if command -v ip >/dev/null 2>&1; then
    PRIVATE_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
  elif command -v hostname >/dev/null 2>&1; then
    PRIVATE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  PUBLIC_IP=""
  for url in "https://api.ipify.org" "https://ifconfig.me" "https://icanhazip.com"; do
    PUBLIC_IP="$(curl -fsS --max-time 4 "$url" 2>/dev/null | tr -d ' \t\n\r')"
    case "$PUBLIC_IP" in ""|*[!0-9.]*) PUBLIC_IP="" ;; *) break ;; esac
  done
  set -e
  EXTERNAL_LINE=""
  if [ -n "$PUBLIC_IP" ] && [ -n "$PRIVATE_IP" ] && [ "$PUBLIC_IP" != "$PRIVATE_IP" ]; then
    EXTERNAL_LINE="external-ip=$PUBLIC_IP/$PRIVATE_IP"
    log "Сервер за NAT: внешний $PUBLIC_IP, внутренний $PRIVATE_IP — coturn настроен объявлять внешний адрес"
  elif [ -n "$PUBLIC_IP" ]; then
    log "Публичный IP сервера: $PUBLIC_IP (NAT не обнаружен)"
  else
    warn "Не удалось определить публичный IP автоматически. Если сервер за NAT и звонки не пойдут,"
    warn "добавьте вручную в /etc/turnserver.conf: external-ip=<публичный-ip>/<локальный-ip> и перезапустите coturn"
  fi

  cat > /etc/turnserver.conf <<EOF
listening-port=3478
min-port=49152
max-port=65535
fingerprint
use-auth-secret
static-auth-secret=$TURN_SECRET
realm=$HOST
$EXTERNAL_LINE
no-multicast-peers
no-cli
EOF

  if [ "$HAS_SYSTEMD" = 1 ] && ! systemctl list-unit-files coturn.service >/dev/null 2>&1; then
    warn "Юнит coturn.service не найден — проверьте, что пакет coturn установился корректно"
  fi
  # На Debian/Ubuntu служба coturn дополнительно смотрит на этот флаг в /etc/default/coturn
  if [ -f /etc/default/coturn ]; then
    if grep -q '^#\?TURNSERVER_ENABLED=' /etc/default/coturn; then
      sed -i 's/^#\?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn
    else
      echo 'TURNSERVER_ENABLED=1' >> /etc/default/coturn
    fi
  fi
  log "coturn настроен (домен $HOST, порт 3478 UDP/TCP, релей 49152-65535 UDP)"
fi

# ---------- push-уведомления (Firebase) — по желанию, включаются наличием ключа сервисного аккаунта ----------
grep -vE '^FIREBASE_CREDENTIALS=' "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || : > "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 640 "$ENV_FILE"; chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"
if [ -f "$INSTALL_DIR/firebase-adminsdk.json" ]; then
  echo "FIREBASE_CREDENTIALS=$INSTALL_DIR/firebase-adminsdk.json" >> "$ENV_FILE"
  log "Push-уведомления включены (ключ Firebase найден)"
else
  log "Push-уведомления отключены: нет ключа сервисного аккаунта Firebase (--firebase-credentials или server/firebase-adminsdk.json)"
fi

# ---------- распознавание речи (faster-whisper) — по желанию, только имя модели; сами пакет и веса ставятся
# отдельно (см. README §14), install-debian.sh их не трогает — только запоминает, какую модель включить ----------
OLD_WHISPER_MODEL="$(grep -oP '^WHISPER_MODEL=\K.*' "$ENV_FILE" 2>/dev/null || true)"
grep -vE '^WHISPER_MODEL=' "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || : > "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 640 "$ENV_FILE"; chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"
[ -z "$WHISPER_MODEL" ] && WHISPER_MODEL="$OLD_WHISPER_MODEL"
if [ -n "$WHISPER_MODEL" ]; then
  echo "WHISPER_MODEL=$WHISPER_MODEL" >> "$ENV_FILE"
  log "Распознавание речи включено (модель: $WHISPER_MODEL) — faster-whisper и ffmpeg должны быть поставлены отдельно"
else
  log "Распознавание речи отключено (--whisper-model не задан)"
fi

[ "$HAS_SYSTEMD" = 1 ] && systemctl daemon-reload

# ---------- файрвол (если используется ufw) ----------
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  log "Открываю порты в ufw: $HTTP_PORT/tcp, $HTTPS_PORT/tcp"
  ufw allow "$HTTP_PORT"/tcp >/dev/null || true
  ufw allow "$HTTPS_PORT"/tcp >/dev/null || true
  [ "$NO_TURN" != 1 ] && { ufw allow 3478 >/dev/null || true; ufw allow 49152:65535/udp >/dev/null || true; }
fi

# ---------- сертификаты ----------
CERT_OK=0
if runuser -u "$SERVICE_USER" -- env CHAT_CERT_DIR="$INSTALL_DIR/certs" CHAT_HOST="$HOST" \
     "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/serve.py" --check; then
  CERT_OK=1
fi

# ---------- запуск ----------
if [ "$HAS_SYSTEMD" != 1 ]; then
  warn "systemd сейчас недоступен (это нормально, например, при сборке образа в контейнере)."
  warn "Юнит установлен в /etc/systemd/system/$SERVICE_NAME.service. На настоящем сервере выполните:"
  warn "  systemctl daemon-reload && systemctl enable --now $SERVICE_NAME"
elif [ "$NO_START" = 1 ]; then
  systemctl enable "$SERVICE_NAME" >/dev/null
  log "Служба включена, но не запущена (--no-start). Запуск: sudo systemctl start $SERVICE_NAME"
elif [ "$CERT_OK" = 1 ]; then
  systemctl enable "$SERVICE_NAME" >/dev/null
  systemctl restart "$SERVICE_NAME"
  sleep 1
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "Служба запущена: https://$HOST"
  else
    warn "Служба включена, но не запустилась. Подробности: journalctl -u $SERVICE_NAME -n 50"
  fi
else
  systemctl enable "$SERVICE_NAME" >/dev/null
  warn "Сертификат не найден или не подходит — служба включена (запустится при загрузке и по расписанию),"
  warn "но сейчас не стартовала. Положите certificate.crt, ca_bundle.crt и private.key в $INSTALL_DIR/certs/"
  warn "(chmod 600 private.key) и выполните: sudo systemctl start $SERVICE_NAME"
fi

# ---------- TURN: запуск как отдельной службы systemd ----------
TURN_STATUS="отключён (--no-turn)"
if [ "$NO_TURN" != 1 ]; then
  if [ "$HAS_SYSTEMD" != 1 ]; then
    TURN_STATUS="юнит установлен, но systemd сейчас недоступен — включите вручную: systemctl enable --now coturn"
  elif ! systemctl list-unit-files coturn.service >/dev/null 2>&1; then
    TURN_STATUS="ОШИБКА: пакет установлен, но юнит coturn.service не найден"
  else
    systemctl enable coturn >/dev/null 2>&1 || true
    if [ "$NO_START" = 1 ]; then
      TURN_STATUS="включён, но не запущен (--no-start)"
    else
      systemctl restart coturn
      sleep 1
      if systemctl is-active --quiet coturn; then
        TURN_STATUS="работает (порт 3478)"
        log "coturn запущен"
      else
        TURN_STATUS="включён, но не запустился — подробности: journalctl -u coturn -n 50"
        warn "coturn включён, но не запустился. Подробности: journalctl -u coturn -n 50"
      fi
    fi
  fi
fi

cat <<SUMMARY

$([ "$FIRST_INSTALL" = 1 ] && echo "Установлено" || echo "Обновлено") в $INSTALL_DIR (пользователь $SERVICE_USER).
  Адрес:         https://$HOST
  Сертификаты:   $INSTALL_DIR/certs/  (certificate.crt, ca_bundle.crt, private.key)
  База и файлы:  $INSTALL_DIR/data/
  TURN (coturn): $TURN_STATUS
  Доп. переменные (FIREBASE_CREDENTIALS и т.п.): $INSTALL_DIR/environment
  Логи:          journalctl -u $SERVICE_NAME -f   /   journalctl -u coturn -f
  Статус:        systemctl status $SERVICE_NAME   /   systemctl status coturn
  Обновить:      sudo bash install-debian.sh (повторно, из каталога server/)
  Удалить:       sudo bash install-debian.sh --uninstall [--purge]

Убедитесь, что домен $HOST указывает на этот сервер и порты $HTTP_PORT/$HTTPS_PORT проброшены с роутера$([ "$NO_TURN" != 1 ] && echo ", а также 3478 (UDP и TCP) и 49152-65535 (UDP) для TURN").
SUMMARY
