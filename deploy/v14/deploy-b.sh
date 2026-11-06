#!/bin/bash
# Этап B: включить сборку этапа A (кандидат /root/sync-h14.next) и новый health; drop-in
# TimeoutStartSec=900; реальный прогон, health, сверка движков, запуск таймера (Requires= даёт ещё прогон),
# повторный dry-run (ждём 0 изменений). Всё проверяется и раскладывается ДО остановки таймера; после
# остановки таймер запускается снова при любом выходе (trap), точка отката /root/sync-h14.last
# переносится на кандидата. Обрыв ssh этап не прерывает (HUP/PIPE игнорируются) — запускайте файлом:
# sudo bash $SYNC_UPLOAD/deploy-b.sh [u:… — id новых пользователей из плана этапа A]. Незаконченный
# этап B (сбой после переключения) повторный запуск доделывает; законченный помечается deploy-b.done.
# Журнал этапа — deploy-b.log в каталоге кандидата. На время проверки журнал синка обезличен
# runtime-drop-in-ом SYNC_ANON=1 (снимает deploy-c.sh или перезагрузка). Кладёт rollback.sh рядом с
# синком ($SYNC_TOOLS/rollback.sh). Секреты, имена и UUID не печатает.
set -uo pipefail
T=${SYNC_TOOLS:-/opt/hiddify-custom/tools}; U=${SYNC_UPLOAD:-/tmp/sync14}; PY=/opt/hiddify-manager/.venv313/bin/python
S=hiddify-child-sync.service
fail() { echo "этап B отменён, ничего не изменено: $*"; exit 1; }
compiles() { $PY -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')" "$1" 2>/dev/null; }
sha() { sha256sum < "$1" | cut -c1-64; }
ptr() { local d; d=$(readlink -f "$(cat "$1" 2>/dev/null)" 2>/dev/null) && [ -n "$d" ] && [ -d "$d" ] && echo "$d"; }
ver() { sed -n "s/^VERSION = '\(.*\)'$/\1/p" "$1"; }
same_aux() {   # health, check_engines и rollback.sh в работе совпадают с загрузкой (или её уже нет)
  [ -d $U ] || return 0
  for f in sync_health_api.py check_engines.py rollback.sh; do cmp -s $U/$f $T/$f || return 1; done
}
same_upload() {   # загружена та же сборка, что собирал этап A в каталоге $1 (или загрузки уже нет)
  [ -d $U ] || return 0
  [ -f $U/stable_sync.py ] && [ -f "$1/upload.sha256" ] && [ "$(sha $U/stable_sync.py)" = "$(cat "$1/upload.sha256")" ]
}
same_prev() {     # в работе то же, что сохранил этап A в $B
  cmp -s "$B/stable_sync.py.prev" $T/stable_sync.py && cmp -s "$B/sync_health_api.py.prev" $T/sync_health_api.py || return 1
  if [ -f "$B/check_engines.py.prev" ]; then cmp -s "$B/check_engines.py.prev" $T/check_engines.py; else [ ! -f $T/check_engines.py ]; fi
}

B=$(ptr /root/sync-h14.next)
if [ -z "$B" ]; then
  LAST=$(ptr /root/sync-h14.last)
  if [ -n "$LAST" ] && [ -f "$LAST/deploy-b.done" ] && [ -f "$LAST/new.sha256" ] \
     && [ "$(sha $T/stable_sync.py)" = "$(cat "$LAST/new.sha256")" ] && same_aux && same_upload "$LAST"; then
    echo "сборка v$(ver $T/stable_sync.py) уже выкачена целиком — повторять A и B не нужно"
    exit 0
  fi
  fail "нет кандидата этапа A (/root/sync-h14.next) — сначала этап A"
fi
[ -f "$B/new.sha256" ] || fail "нет маркера этапа A ($B/new.sha256) — повторите этап A"
MARK=$(cat "$B/new.sha256")
same_upload "$B" || fail "загружена не та сборка, что собирал этап A ($B) — повторите этап A"
if [ -f $T/stable_sync.py.new ]; then
  [ "$(sha $T/stable_sync.py.new)" = "$MARK" ] || fail "stable_sync.py.new не тот, что проверял последний этап A — повторите этап A"
  same_prev || fail "в работе не то, что сохранил этап A ($B: откат или правка после этапа A) — повторите этап A"
  NEWF=$T/stable_sync.py.new
elif [ "$(sha $T/stable_sync.py)" = "$MARK" ]; then
  echo "этап B для этой сборки начинался и не закончился (журнал: $B/deploy-b.log) — доделываю"
  NEWF=$T/stable_sync.py
else
  fail "нет $T/stable_sync.py.new — повторите этап A"
fi
NEWVER=$(sed -n "s/^VERSION = '\(.*-h14\)'$/\1/p" $NEWF)
[ -n "$NEWVER" ] || fail "в сборке нет VERSION …-h14"
compiles $NEWF || fail "сборка не компилируется"
for f in stable_sync.py sync_health_api.py check_engines.py rollback.sh; do
  [ -f $U/$f ] || fail "нет $U/$f (каталог загрузки очищен перезагрузкой? загрузите заново)"
  case $f in *.py) compiles $U/$f || fail "$U/$f не компилируется";; *) bash -n $U/$f || fail "$U/$f с ошибкой синтаксиса";; esac
done
systemctl show -p ExecStart --value $S | grep -q "$T/stable_sync.py" || fail "юнит $S запускает не $T/stable_sync.py"
install -m "$(stat -c %a $T/sync_health_api.py)" -o root -g root $U/sync_health_api.py $T/sync_health_api.py.new \
  && install -m 0700 -o root -g root $U/check_engines.py $T/check_engines.py.new \
  && install -m 0700 -o root -g root $U/rollback.sh $T/rollback.sh || fail "не разложены файлы в $T"

LOG="$B/deploy-b.log"
trap '' HUP PIPE
exec > >(tee -p -a "$LOG") 2>&1
echo "=== этап B: v$NEWVER, кандидат $B ==="
systemctl stop hiddify-child-sync.timer        # не stop сервиса: таймер его Requires=
trap 'systemctl start hiddify-child-sync.timer; echo "таймер: $(systemctl is-active hiddify-child-sync.timer)"' EXIT
for i in $(seq 1 120); do [ "$(systemctl is-active $S)" = activating ] || break; sleep 5; done
[ "$(systemctl is-active $S)" != activating ] || { echo "прогон синка не закончился за 10 минут — этап B прерван"; exit 1; }
echo "$B" > /root/sync-h14.last                 # точка отката — то, что заменяет этот этап B

set -e
install -d -m 0700 -o root -g root /var/lib/hiddify-child-sync; chmod 700 /var/lib/hiddify-child-sync
if [ -f $T/stable_sync.py.new ]; then
  chmod --reference=$T/stable_sync.py $T/stable_sync.py.new
  mv -f $T/stable_sync.py.new $T/stable_sync.py
fi
mv -f $T/sync_health_api.py.new $T/sync_health_api.py
mv -f $T/check_engines.py.new $T/check_engines.py
install -d -m 0755 /etc/systemd/system/$S.d /run/systemd/system/$S.d
printf '[Service]\n# v4.5.x-h14: бюджет прогона 600 с, затем запас на текущий запрос\nTimeoutStartSec=900\n' \
  > /etc/systemd/system/$S.d/timeout.conf
printf '[Service]\nEnvironment=SYNC_ANON=1\n' > /run/systemd/system/$S.d/anon.conf
systemctl daemon-reload
set +e
systemctl reset-failed hiddify-sync-api.service 2>/dev/null
systemctl restart hiddify-sync-api.service || echo "⚠️ health API не поднялся — проверьте: systemctl status hiddify-sync-api"
sleep 2
echo "файлы на месте: v$(ver $T/stable_sync.py); sync-api: $(systemctl is-active hiddify-sync-api.service); TimeoutStartUSec=$(systemctl show -p TimeoutStartUSec --value $S)"

summary() {   # $1 — с какого момента журнал
  echo "Result=$(systemctl show -p Result --value $S) ExecMainStatus=$(systemctl show -p ExecMainStatus --value $S)"
  journalctl -u $S --since "@$1" --no-pager -o cat \
    | grep -vE "Пользователь u:[0-9a-f]+: parent=|✅ Обновлён трафик u:" | cut -c1-240
  python3 -c "import json; d=json.load(open('/var/lib/hiddify-child-sync/status.json')); print({k:d.get(k) for k in ('version','ok','steps','parent_count','expected_count','local_count','created','patched','sql_fixed','deleted','blocked','unblocked','deferred_inactive','held_blocks','held_deletes','backlog','stuck','errors','last_error','consecutive_failures','duration_s')})"
}

T0=$(date +%s)
echo "=== реальный прогон 1 (systemctl start) ==="
systemctl start $S
summary $T0
echo "=== health ==="
curl -s --max-time 10 http://127.0.0.1:8081/api/v2/hiddify-sync/health \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status'], d.get('reasons'), 'users=', d['database'].get('user_count'))"
echo "=== движки (повтор до 6 раз — apply-users догоняет за секунды) ==="
SYNC_SCRIPT=$T/stable_sync.py $PY $T/check_engines.py --retry 6 "$@"

echo "=== таймер (Requires= запускает прогон 2) ==="
T1=$(date +%s)
systemctl start hiddify-child-sync.timer
sleep 3
for i in $(seq 1 120); do [ "$(systemctl is-active $S)" = activating ] || break; sleep 5; done
summary $T1
systemctl list-timers hiddify-child-sync.timer --no-pager | sed -n 2p
touch "$B/deploy-b.done"; rm -f /root/sync-h14.next
echo "этап B завершён: в работе v$NEWVER, точка отката — $B"

echo "=== повторный dry-run (ожидается 0 изменений) ==="
( cd $T && SYNC_ANON=1 $PY $T/stable_sync.py --dry-run 2>&1 ) | grep -E "\[dry-run\]|Синхронизация:|dry-run завершён" | cut -c1-240
