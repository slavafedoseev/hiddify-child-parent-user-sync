#!/bin/bash
# Откат на предыдущую версию синка: файлы из бэкапа этапа A, без drop-in-ов, health v4.3. Созданных синком
# пользователей не трогает (дамп таблицы user до выкатки — в каталоге бэкапа).
set -euo pipefail
T=${SYNC_TOOLS:-/opt/hiddify-custom/tools}; S=hiddify-child-sync.service
B=$(cat /root/sync-h14.last); [ -f "$B/stable_sync.py.v43" ] || { echo "нет бэкапа $B"; exit 1; }
systemctl stop hiddify-child-sync.timer
for i in $(seq 1 120); do [ "$(systemctl is-active $S)" = activating ] || break; sleep 5; done
cp -p "$B/stable_sync.py.v43" $T/stable_sync.py
cp -p "$B/sync_health_api.py.v43" $T/sync_health_api.py
rm -f /etc/systemd/system/$S.d/timeout.conf /run/systemd/system/$S.d/anon.conf
systemctl daemon-reload
systemctl restart hiddify-sync-api.service
systemctl start hiddify-child-sync.timer
echo "откат выполнен из $B: $(md5sum $T/stable_sync.py | cut -c1-8), таймер $(systemctl is-active hiddify-child-sync.timer)"
