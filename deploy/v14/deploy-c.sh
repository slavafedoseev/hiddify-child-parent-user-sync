#!/bin/bash
# Этап C: проверка прогонов таймера после выкатки (usage: deploy-c.sh <epoch начала>), health, движки;
# затем снять runtime-drop-in SYNC_ANON=1 и прибрать: каталог загрузки и дубли бэкапов (точки отката
# /root/sync-h14.last, .baseline, кандидат .next и их цепочка prev-last остаются). Если этап B начат и
# не закончен — ничего не делает (код 1). Код выхода — как у check_engines (0 совпадает, 1 расхождения,
# 2 не всё проверено). Секреты, имена и UUID не печатает.
set -uo pipefail
T=${SYNC_TOOLS:-/opt/hiddify-custom/tools}; PY=/opt/hiddify-manager/.venv313/bin/python; S=hiddify-child-sync.service
SINCE=${1:?usage: deploy-c.sh <epoch>}
ptr() { local d; d=$(readlink -f "$(cat "$1" 2>/dev/null)" 2>/dev/null) && [ -n "$d" ] && [ -d "$d" ] && echo "$d"; }
NX=$(ptr /root/sync-h14.next)
if [ -n "$NX" ] && [ -f "$NX/new.sha256" ] && [ ! -f "$NX/deploy-b.done" ] \
   && [ "$(sha256sum < $T/stable_sync.py | cut -c1-64)" = "$(cat "$NX/new.sha256")" ]; then
  echo "этап B кандидата $NX начат и не закончен — запустите deploy-b.sh, он доделает (этап C отменён)"
  exit 1
fi
echo "последний прогон: Result=$(systemctl show -p Result --value $S) ExecMainStatus=$(systemctl show -p ExecMainStatus --value $S)"
echo "=== прогоны с $(date -d "@$SINCE" '+%H:%M:%S') ==="
journalctl -u $S --since "@$SINCE" --no-pager -o cat \
  | grep -E "Starting Stable|✅ Синхронизация:|Stable sync completed|завершён с ошибками|❌|⏸|⏳|Main process exited|Failed" | cut -c1-240
echo "прогонов: $(journalctl -u $S --since "@$SINCE" --no-pager -o cat | grep -c 'Starting Stable'), с изменениями через API: $(journalctl -u $S --since "@$SINCE" --no-pager -o cat | grep -cE '✏️ Обновлён|➕ Создан|🗑️  Удалён')"
python3 -c "import json; d=json.load(open('/var/lib/hiddify-child-sync/status.json')); print({k:d.get(k) for k in ('version','ok','parent_count','expected_count','local_count','created','patched','sql_fixed','deleted','held_blocks','held_deletes','backlog','stuck','errors','last_error','consecutive_failures','duration_s')})"
echo "=== health ==="
curl -s --max-time 10 http://127.0.0.1:8081/api/v2/hiddify-sync/health \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status'], d.get('reasons'), 'users=', d['database'].get('user_count'))"
echo "=== движки ==="
SYNC_SCRIPT=$T/stable_sync.py $PY $T/check_engines.py --retry 3; RC=$?
echo "=== снять SYNC_ANON и прибрать ==="
rm -f /run/systemd/system/$S.d/anon.conf; rmdir /run/systemd/system/$S.d 2>/dev/null; systemctl daemon-reload
echo "SYNC_ANON в окружении юнита: $(systemctl show -p Environment --value $S | grep -c SYNC_ANON)"

KEEP=()
for p in /root/sync-h14.last /root/sync-h14.baseline /root/sync-h14.next; do
  k=$(ptr $p) || continue
  for i in $(seq 1 20); do                       # точка отката и её цепочка prev-last
    KEEP+=("$k")
    [ -f "$k/prev-last" ] && k=$(ptr "$k/prev-last") || break
  done
done
saved() { for n in "$2.prev" "$2.v43"; do [ -f "$1/$n" ] && { echo "$1/$n"; return; }; done; }
same_dir() {   # все сохранённые файлы одинаковы
  local f a b
  for f in stable_sync.py sync_health_api.py check_engines.py; do
    a=$(saved "$1" $f); b=$(saved "$2" $f)
    if [ -n "$a" ] || [ -n "$b" ]; then [ -n "$a" ] && [ -n "$b" ] && cmp -s "$a" "$b" || return 1; fi
  done
  [ -n "$(saved "$1" stable_sync.py)" ]
}
for d in /root/sync-h14-*; do
  [ -d "$d" ] || continue
  for k in "${KEEP[@]}"; do [ "$d" -ef "$k" ] && continue 2; done      # точки отката не трогаем никогда
  for k in "${KEEP[@]}"; do
    same_dir "$d" "$k" && { rm -rf "$d"; echo "удалён дубль бэкапа: $d"; break; }
  done
done
rm -rf "${SYNC_UPLOAD:-/tmp/sync14}"
for p in /root/sync-h14.last /root/sync-h14.baseline /root/sync-h14.next; do
  if [ -f $p ]; then echo "$p → $(cat $p) (v$(cat "$(cat $p)/prev.version" 2>/dev/null || echo 4.3))"; fi
done
exit $RC
