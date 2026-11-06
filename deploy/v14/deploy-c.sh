#!/bin/bash
# Этап C: проверка прогонов таймера после выкатки (usage: deploy-c.sh <epoch начала>),
# health, движки; затем снять runtime-drop-in SYNC_ANON=1 и прибрать временные файлы.
# Секреты, имена и UUID не печатает.
set -uo pipefail
T=${SYNC_TOOLS:-/opt/hiddify-custom/tools}; PY=/opt/hiddify-manager/.venv313/bin/python; S=hiddify-child-sync.service
SINCE=${1:?usage: deploy-c.sh <epoch>}
echo "последний прогон: Result=$(systemctl show -p Result --value $S) ExecMainStatus=$(systemctl show -p ExecMainStatus --value $S)"
echo "=== прогоны с $(date -d "@$SINCE" '+%H:%M:%S') ==="
journalctl -u $S --since "@$SINCE" --no-pager -o cat \
  | grep -E "Starting Stable|✅ Синхронизация:|Stable sync completed|завершён с ошибками|❌|⏸|⏳|Main process exited|Failed" | cut -c1-240
echo "прогонов: $(journalctl -u $S --since "@$SINCE" --no-pager -o cat | grep -c 'Starting Stable'), с изменениями через API: $(journalctl -u $S --since "@$SINCE" --no-pager -o cat | grep -cE '✏️ Обновлён|➕ Создан|🗑️  Удалён')"
python3 -c "import json; d=json.load(open('/var/lib/hiddify-child-sync/status.json')); print({k:d.get(k) for k in ('version','ok','parent_count','expected_count','local_count','created','patched','sql_fixed','deleted','held_blocks','backlog','stuck','errors','last_error','consecutive_failures','duration_s')})"
echo "=== health ==="
curl -s --max-time 10 http://127.0.0.1:8081/api/v2/hiddify-sync/health \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status'], d.get('reasons'), 'users=', d['database'].get('user_count'))"
echo "=== движки ==="
$PY $T/check_engines.py
echo "=== снять SYNC_ANON и прибрать ==="
rm -f /run/systemd/system/$S.d/anon.conf; rmdir /run/systemd/system/$S.d 2>/dev/null; systemctl daemon-reload
echo "SYNC_ANON в окружении юнита: $(systemctl show -p Environment --value $S | grep -c SYNC_ANON)"
KEEP=$(cat /root/sync-h14.last)
for d in /root/sync-h14-*; do
  [ "$d" = "$KEEP" ] && continue
  cmp -s "$d/stable_sync.py.v43" "$KEEP/stable_sync.py.v43" && rm -rf "$d" && echo "удалён дубль бэкапа: $d"
done
rm -rf "${SYNC_UPLOAD:-/tmp/sync14}"
echo "бэкап для отката: $KEEP ($(ls "$KEEP" | tr '\n' ' '))"
