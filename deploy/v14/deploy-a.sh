#!/bin/bash
# Этап A: сборка stable_sync.py.new с конфигом (PARENT_URL, API_KEY, DB_CONFIG, PARENT_TLS_VERIFY) из
# ЖИВОГО файла (build_new.py), компиляция, бэкап (файлы + дамп таблицы user), dry-run. В работу ничего не
# включает и точку отката /root/sync-h14.last не трогает: пишет кандидата /root/sync-h14.next (бэкап
# того, что заменит этап B, с маркером new.sha256); .last переносит сам этап B при переключении.
# /root/sync-h14.baseline — версия до первой v4.5 (пишется один раз). Если вся сборка (синк, health,
# check_engines, rollback.sh) уже в работе — только dry-run. Запуск: sudo bash $SYNC_UPLOAD/deploy-a.sh.
# Секреты, имена и UUID не печатает.
set -euo pipefail
umask 077
T=${SYNC_TOOLS:-/opt/hiddify-custom/tools}; U=${SYNC_UPLOAD:-/tmp/sync14}; PY=/opt/hiddify-manager/.venv313/bin/python
NEWVER=$(sed -n "s/^VERSION = '\(.*-h14\)'$/\1/p" $U/stable_sync.py)
[ -n "$NEWVER" ] || { echo "в $U/stable_sync.py нет VERSION = '…-h14'"; exit 1; }
PREVVER=$(sed -n "s/^VERSION = '\(.*\)'$/\1/p" $T/stable_sync.py); PREVVER=${PREVVER:-4.3}
NX=$(readlink -f "$(cat /root/sync-h14.next 2>/dev/null)" 2>/dev/null || true)
if [ -n "$NX" ] && [ -f "$NX/new.sha256" ] && [ ! -f "$NX/deploy-b.done" ] \
   && [ "$(sha256sum < $T/stable_sync.py | cut -c1-64)" = "$(cat "$NX/new.sha256")" ]; then
  echo "этап B кандидата $NX начат и не закончен — запустите deploy-b.sh, он доделает (этап A отменён)"
  exit 1
fi

python3 -I $U/build_new.py $T/stable_sync.py $U/stable_sync.py $T/stable_sync.py.new "$NEWVER"
for f in $T/stable_sync.py.new $U/sync_health_api.py $U/check_engines.py; do
  $PY -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')" $f
done
echo "компиляция py3.13 ok: stable_sync.py.new, sync_health_api.py, check_engines.py"
# --dry-run понимает только v4.5+: старый код с этим флагом сделал бы настоящий прогон
grep -q "^VERSION = '$NEWVER'$" $T/stable_sync.py.new || { echo "stable_sync.py.new не v$NEWVER — отменено"; exit 1; }

dryrun() {   # $1 — файл синка, $2 — куда писать вывод
  local rc=0
  ( cd $T && SYNC_ANON=1 $PY "$1" --dry-run ) > "$2" 2>&1 || rc=$?
  chmod 600 "$2"
  echo "код выхода dry-run: $rc; строк: $(wc -l < "$2")"
  grep -vE "^\[[0-9 :-]+\] \[dry-run\] (✏️ PATCH|🛠 SQL|➕ POST)" "$2" | cut -c1-240
  echo "--- план: POST $(grep -c '➕ POST' "$2"), PATCH $(grep -c '✏️ PATCH' "$2"), SQL $(grep -c '🛠 SQL' "$2"), DELETE $(grep -c '🗑️ DELETE' "$2")"
  echo "--- POST (обезличенно):"; grep -o '➕ POST u:[0-9a-f]*' "$2" | sort || true
  echo "--- PATCH: какие поля у скольких:"
  grep -o '✏️ PATCH u:[0-9a-f]*: \[.*\].*' "$2" | sed -E 's/^✏️ PATCH u:[0-9a-f]*: //' | sort | uniq -c | sort -rn | head -20 || true
  echo "--- SQL: какие поля у скольких:"
  grep -o '🛠 SQL u:[0-9a-f]*: \[.*\]' "$2" | sed -E 's/^🛠 SQL u:[0-9a-f]*: //' | sort | uniq -c | sort -rn | head -20 || true
}

same_all() {
  cmp -s $T/stable_sync.py.new $T/stable_sync.py || return 1
  for f in sync_health_api.py check_engines.py rollback.sh; do cmp -s $U/$f $T/$f || return 1; done
}
if same_all; then
  rm -f $T/stable_sync.py.new
  echo "сборка v$NEWVER уже в работе целиком — бэкап, точки отката и маркер не меняются, этап B не нужен"
  echo "=== DRY-RUN работающей v$NEWVER (обезличенно) ==="
  OUTF=$(mktemp); dryrun $T/stable_sync.py "$OUTF"; rm -f "$OUTF"
  exit 0
fi

TS=$(date +%Y%m%d-%H%M%S); B=/root/sync-h14-$TS
install -d -m 0700 "$B"
cp -p $T/stable_sync.py "$B/stable_sync.py.prev"; cp -p $T/sync_health_api.py "$B/sync_health_api.py.prev"
[ ! -f $T/check_engines.py ] || cp -p $T/check_engines.py "$B/check_engines.py.prev"
echo "$PREVVER" > "$B/prev.version"
mysqldump --single-transaction hiddifypanel user > "$B/user-table.sql"
chmod 600 "$B"/*
echo "бэкап: $B (в работе v$PREVVER; $(ls "$B" | tr '\n' ' '); дамп user: $(wc -c < "$B/user-table.sql") Б)"
cmp -s $T/stable_sync.py.new $T/stable_sync.py && echo "код синка тот же — этап B обновит health, check_engines и rollback.sh"
if [ ! -f /root/sync-h14.baseline ]; then          # версия до первой v4.5 — один раз
  OLD=$(readlink -f "$(cat /root/sync-h14.last 2>/dev/null)" 2>/dev/null || true)
  if [ -n "$OLD" ] && [ -f "$OLD/stable_sync.py.v43" ]; then echo "$OLD" > /root/sync-h14.baseline
  elif [[ "$PREVVER" != *-h14 ]]; then echo "$B" > /root/sync-h14.baseline; fi
fi
[ ! -f /root/sync-h14.baseline ] || echo "точка отката к версии до v4.5: $(cat /root/sync-h14.baseline)"
stat -c '%a %U:%G %n' $T/stable_sync.py $T/stable_sync.py.new $T/sync_health_api.py

echo "=== DRY-RUN v$NEWVER (обезличенно) ==="
dryrun $T/stable_sync.py.new "$B/dryrun-a.txt"

sha256sum < $T/stable_sync.py.new | cut -c1-64 > "$B/new.sha256"
sha256sum < $U/stable_sync.py | cut -c1-64 > "$B/upload.sha256"      # какую загрузку собирали
L=$(readlink -f "$(cat /root/sync-h14.last 2>/dev/null)" 2>/dev/null || true)
[ -z "$L" ] || [ ! -d "$L" ] || echo "$L" > "$B/prev-last"          # цепочка точек отката
echo "$B" > /root/sync-h14.next
echo "этап A готов: кандидат для этапа B — $B (маркер new.sha256); точка отката /root/sync-h14.last сменится на него только в этапе B"
