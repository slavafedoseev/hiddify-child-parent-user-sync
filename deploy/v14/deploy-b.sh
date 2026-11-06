#!/bin/bash
# Этап B: включить v4.5-h14 и новый health; drop-in TimeoutStartSec=900; таймер на время
# выкатки остановлен; один реальный прогон, сверка движков, запуск таймера (Requires= даёт ещё
# прогон), повторный dry-run (ждём 0 изменений). На время проверки журнал синка обезличен
# runtime-drop-in-ом SYNC_ANON=1 (/run, снимается deploy-c.sh или перезагрузкой).
# Секреты, имена и UUID не печатает. Откат: rollback.sh.
set -uo pipefail
T=${SYNC_TOOLS:-/opt/hiddify-custom/tools}; U=${SYNC_UPLOAD:-/tmp/sync14}; PY=/opt/hiddify-manager/.venv313/bin/python
S=hiddify-child-sync.service
[ -f $T/stable_sync.py.new ] || { echo "нет stable_sync.py.new — сначала этап A"; exit 1; }
grep -q "^VERSION = '4.5-h14'$" $T/stable_sync.py.new || { echo "stable_sync.py.new не v4.5-h14"; exit 1; }
B=$(cat /root/sync-h14.last); [ -f "$B/stable_sync.py.v43" ] || { echo "нет бэкапа $B"; exit 1; }

systemctl stop hiddify-child-sync.timer        # не stop сервиса: таймер его Requires=
for i in $(seq 1 120); do [ "$(systemctl is-active $S)" = activating ] || break; sleep 5; done
echo "таймер: $(systemctl is-active hiddify-child-sync.timer), сервис: $(systemctl is-active $S)"

set -e
install -d -m 0700 -o root -g root /var/lib/hiddify-child-sync; chmod 700 /var/lib/hiddify-child-sync
chmod --reference=$T/stable_sync.py $T/stable_sync.py.new
mv -f $T/stable_sync.py.new $T/stable_sync.py
install -m "$(stat -c %a $T/sync_health_api.py)" -o root -g root $U/sync_health_api.py $T/sync_health_api.py.new
mv -f $T/sync_health_api.py.new $T/sync_health_api.py
install -m 0700 -o root -g root $U/check_engines.py $T/check_engines.py
install -d -m 0755 /etc/systemd/system/$S.d /run/systemd/system/$S.d
printf '[Service]\n# v4.5-h14: бюджет прогона 600 с, затем запас на текущий запрос\nTimeoutStartSec=900\n' \
  > /etc/systemd/system/$S.d/timeout.conf
printf '[Service]\nEnvironment=SYNC_ANON=1\n' > /run/systemd/system/$S.d/anon.conf
systemctl daemon-reload
set +e
systemctl restart hiddify-sync-api.service; sleep 2
echo "sync-api: $(systemctl is-active hiddify-sync-api.service); TimeoutStartUSec=$(systemctl show -p TimeoutStartUSec --value $S)"

summary() {   # $1 — с какого момента журнал
  echo "Result=$(systemctl show -p Result --value $S) ExecMainStatus=$(systemctl show -p ExecMainStatus --value $S)"
  journalctl -u $S --since "@$1" --no-pager -o cat \
    | grep -vE "Пользователь u:[0-9a-f]+: parent=|✅ Обновлён трафик u:" | cut -c1-240
  python3 -c "import json; d=json.load(open('/var/lib/hiddify-child-sync/status.json')); print({k:d.get(k) for k in ('version','ok','steps','parent_count','expected_count','local_count','created','patched','sql_fixed','deleted','blocked','unblocked','deferred_inactive','held_blocks','backlog','stuck','errors','last_error','consecutive_failures','duration_s')})"
}

T0=$(date +%s)
echo "=== реальный прогон 1 (systemctl start) ==="
systemctl start $S
summary $T0
echo "=== health ==="
curl -s --max-time 10 http://127.0.0.1:8081/api/v2/hiddify-sync/health \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status'], d.get('reasons'), 'users=', d['database'].get('user_count'))"
echo "=== движки ==="
$PY $T/check_engines.py "$@"

echo "=== таймер (Requires= запускает прогон 2) ==="
T1=$(date +%s)
systemctl start hiddify-child-sync.timer
sleep 3
for i in $(seq 1 120); do [ "$(systemctl is-active $S)" = activating ] || break; sleep 5; done
summary $T1
systemctl list-timers hiddify-child-sync.timer --no-pager | sed -n 2p

echo "=== повторный dry-run (ожидается 0 изменений) ==="
( cd $T && SYNC_ANON=1 $PY $T/stable_sync.py --dry-run 2>&1 ) | grep -E "\[dry-run\]|Синхронизация:|dry-run завершён" | cut -c1-240
