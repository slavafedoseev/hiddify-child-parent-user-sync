#!/bin/bash
# Этап A: бэкап (файлы + дамп таблицы user), сборка stable_sync.py.new с конфигом
# (PARENT_URL, API_KEY, DB_CONFIG) из ЖИВОГО файла (build_new.py), компиляция, dry-run.
# В работу ничего не включает. Секреты, имена и UUID не печатает.
set -euo pipefail
umask 077
T=${SYNC_TOOLS:-/opt/hiddify-custom/tools}; U=${SYNC_UPLOAD:-/tmp/sync14}; PY=/opt/hiddify-manager/.venv313/bin/python
TS=$(date +%Y%m%d-%H%M%S); B=/root/sync-h14-$TS
install -d -m 0700 "$B"
cp -p $T/stable_sync.py "$B/stable_sync.py.v43"; cp -p $T/sync_health_api.py "$B/sync_health_api.py.v43"
mysqldump --single-transaction hiddifypanel user > "$B/user-table.sql"
chmod 600 "$B"/*
echo "$B" > /root/sync-h14.last
echo "бэкап: $B ($(ls "$B" | tr '\n' ' '); дамп user: $(wc -c < "$B/user-table.sql") Б, INSERT-строк $(grep -c '^INSERT' "$B/user-table.sql"))"

python3 -I $U/build_new.py $T/stable_sync.py $U/stable_sync.py $T/stable_sync.py.new 4.5-h14
$PY -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec'); print('stable_sync.py.new: компиляция py3.13 ok')" $T/stable_sync.py.new
$PY -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec'); print('sync_health_api.py: компиляция ok')" $U/sync_health_api.py
$PY -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec'); print('check_engines.py: компиляция ok')" $U/check_engines.py
stat -c '%a %U:%G %n' $T/stable_sync.py $T/stable_sync.py.new $T/sync_health_api.py

echo "=== DRY-RUN новой версии (обезличенно) ==="
# --dry-run понимает только v4.5: старый код с этим флагом сделал бы настоящий прогон
grep -q "^VERSION = '4.5-h14'$" $T/stable_sync.py.new || { echo "stable_sync.py.new не v4.5-h14 — dry-run отменён"; exit 1; }
rc=0
( cd $T && SYNC_ANON=1 $PY $T/stable_sync.py.new --dry-run ) > "$B/dryrun-a.txt" 2>&1 || rc=$?
chmod 600 "$B/dryrun-a.txt"
echo "код выхода dry-run: $rc; строк: $(wc -l < "$B/dryrun-a.txt")"
grep -vE "^\[[0-9 :-]+\] \[dry-run\] (✏️ PATCH|🛠 SQL|➕ POST)" "$B/dryrun-a.txt" | cut -c1-240
echo "--- план: POST $(grep -c '➕ POST' "$B/dryrun-a.txt"), PATCH $(grep -c '✏️ PATCH' "$B/dryrun-a.txt"), SQL $(grep -c '🛠 SQL' "$B/dryrun-a.txt"), DELETE $(grep -c '🗑️ DELETE' "$B/dryrun-a.txt")"
echo "--- POST (обезличенно):"; grep -o '➕ POST u:[0-9a-f]*' "$B/dryrun-a.txt" | sort || true
echo "--- PATCH: какие поля у скольких:"
grep -o '✏️ PATCH u:[0-9a-f]*: \[.*\].*' "$B/dryrun-a.txt" | sed -E 's/^✏️ PATCH u:[0-9a-f]*: //' | sort | uniq -c | sort -rn | head -20 || true
echo "--- SQL: какие поля у скольких:"
grep -o '🛠 SQL u:[0-9a-f]*: \[.*\]' "$B/dryrun-a.txt" | sed -E 's/^🛠 SQL u:[0-9a-f]*: //' | sort | uniq -c | sort -rn | head -20 || true
