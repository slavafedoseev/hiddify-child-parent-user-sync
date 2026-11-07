#!/bin/bash
# Этап B: включить сборку этапа A (кандидат /root/sync-h14.next) и новый health; drop-in
# TimeoutStartSec=900; реальный прогон, сверка движков, запуск таймера (Requires= даёт ещё прогон), health,
# повторный dry-run. Всё проверяется и раскладывается ДО остановки таймера и перепроверяется после ожидания
# текущего прогона; после остановки таймер запускается снова при любом выходе (trap). Перед первым
# изменением файлов в работе этап переносит точку отката /root/sync-h14.last на кандидата (и
# /root/sync-h14.baseline, если заменяет версию до v4.5) и ставит в кандидате маркер deploy-b.switched;
# законченный этап помечается deploy-b.done. Этап, упавший после переключения синка, повторный запуск
# доделывает из файлов, уже разложенных рядом с синком (саму сборку загружать заново не нужно, нужен только
# этот скрипт), если в работе — сборка этапа A (допускается правка конфига после сбоя: копия сборки лежит
# в кандидате); упавший до переноса синка — начинает заново. Обрыв ssh этап не прерывает (HUP/PIPE игнорируются); A, C и откат ждут его окончания
# (общая блокировка /root/sync-h14.lock). Запуск файлом:
# sudo bash $SYNC_UPLOAD/deploy-b.sh [u:… — id новых пользователей из плана этапа A].
# Без кандидата, если загруженная сборка уже в работе целиком, — только проверки.
# Код выхода: 0 — сборка в работе, проверки прошли; 1 — не выкачено или не доделано (сообщение говорит,
# менялись ли файлы в работе и что делать); 2 — сборка в работе (deploy-b.done), но проверки нашли
# проблемы (список в конце вывода). Журнал этапа — deploy-b.log в каталоге кандидата. На время проверки
# журнал синка обезличен runtime-drop-in-ом SYNC_ANON=1 (снимает deploy-c.sh при коде 0 или перезагрузка).
# Секреты, имена и UUID не печатает.
set -uo pipefail
T=${SYNC_TOOLS:-/opt/hiddify-custom/tools}; U=${SYNC_UPLOAD:-/tmp/sync14}; PY=/opt/hiddify-manager/.venv313/bin/python
S=hiddify-child-sync.service
fail() { echo "этап B отменён, ничего не изменено: $*"; exit 1; }
{ exec 9>>/root/sync-h14.lock; } 2>/dev/null || fail "не открыть /root/sync-h14.lock — запускайте от root (sudo)"
flock -n 9 || fail "идёт другой этап выкатки или откат (обрыв ssh его не прерывает) — дождитесь окончания"
compiles() { $PY -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')" "$1" 2>/dev/null; }
sha() { sha256sum < "$1" | cut -c1-64; }
ptr() { local d; d=$(readlink -f "$(cat "$1" 2>/dev/null)" 2>/dev/null) && [ -n "$d" ] && [ -d "$d" ] && echo "$d"; }
ver() { sed -n "s/^VERSION = '\(.*\)'$/\1/p" "$1"; }
h14() { sed -n "s/^VERSION = '\(.*-h14\)'$/\1/p" "$1"; }
same_aux() {   # health, check_engines и rollback.sh в работе совпадают с загрузкой
  local f; for f in sync_health_api.py check_engines.py rollback.sh; do [ -f $U/$f ] && cmp -s $U/$f $T/$f || return 1; done
}
same_upload() {   # загружена та же сборка, что собирал этап A в каталоге $1 (синк и остальные её файлы)
  [ -f $U/stable_sync.py ] && [ -f "$1/upload.sha256" ] && [ "$(sha $U/stable_sync.py)" = "$(cat "$1/upload.sha256")" ] || return 1
  [ ! -f "$1/upload-aux.sha256" ] || ( cd $U && sha256sum --status -c "$1/upload-aux.sha256" )
}
same_prev() {     # в работе то же, что сохранил этап A в $B
  cmp -s "$B/stable_sync.py.prev" $T/stable_sync.py && cmp -s "$B/sync_health_api.py.prev" $T/sync_health_api.py || return 1
  if [ -f "$B/check_engines.py.prev" ]; then cmp -s "$B/check_engines.py.prev" $T/check_engines.py; else [ ! -f $T/check_engines.py ]; fi
}
upload_in_work() {   # загруженная сборка уже в работе целиком — тот же признак, что у этапа A
  local tmp v rc=1
  [ -f $U/stable_sync.py ] && [ -f $U/build_new.py ] && same_aux || return 1
  v=$(h14 $U/stable_sync.py); [ -n "$v" ] || return 1
  tmp=$(mktemp) || return 1
  python3 -I $U/build_new.py $T/stable_sync.py $U/stable_sync.py "$tmp" "$v" > /dev/null 2>&1 && cmp -s "$tmp" $T/stable_sync.py && rc=0
  rm -f "$tmp"; return $rc
}
built_with_live_cfg() {   # живой синк — сборка этапа A (копия в кандидате), отличается от неё только конфигом
  local tmp rc=1
  [ -f "$B/stable_sync.py.built" ] && [ -f "$B/build_new.py" ] || return 1
  tmp=$(mktemp) || return 1
  python3 -I "$B/build_new.py" $T/stable_sync.py "$B/stable_sync.py.built" "$tmp" "$(h14 "$B/stable_sync.py.built")" > /dev/null 2>&1 \
    && cmp -s "$tmp" $T/stable_sync.py && rc=0
  rm -f "$tmp"; return $rc
}
PROBLEMS=()
health() {    # ответ health API; код 0 — healthy
  curl -s --max-time 10 http://127.0.0.1:8081/api/v2/hiddify-sync/health | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("health API не ответил"); sys.exit(1)
print(d.get("status"), d.get("reasons"), "users=", (d.get("database") or {}).get("user_count"))
sys.exit(0 if d.get("status") == "healthy" else 1)'
}
engines() {   # $1 — сколько раз повторять сверку
  SYNC_SCRIPT=$T/stable_sync.py $PY $T/check_engines.py --retry "$1" "${@:2}"; local rc=$?
  [ $rc = 0 ] || PROBLEMS+=("движки: код check_engines $rc (1 — расхождения, 2 — проверены не все)")
}
verdict() {   # итог по PROBLEMS: код 0 или 2
  if [ ${#PROBLEMS[@]} -eq 0 ]; then echo "ИТОГ этапа B: $1, проверки прошли"; exit 0; fi
  echo "ИТОГ этапа B: $1, но проверки нашли проблемы:"; printf '  - %s\n' "${PROBLEMS[@]}"
  echo "разберитесь по выводу выше; аварийный режим и откат — RUNBOOK §6"; exit 2
}

B=$(ptr /root/sync-h14.next)
if [ -z "$B" ]; then
  upload_in_work || fail "нет кандидата этапа A (/root/sync-h14.next) — сначала этап A"
  echo "загруженная сборка v$(ver $T/stable_sync.py) уже в работе целиком — повторять A и B не нужно; проверяю состояние"
  RES=$(systemctl show -p Result --value $S); echo "последний прогон: Result=$RES"
  [ "$RES" = success ] || PROBLEMS+=("последний прогон: Result=$RES")
  health || PROBLEMS+=("health не healthy")
  engines 3 "$@"
  verdict "сборка уже в работе"
fi
[ -f "$B/new.sha256" ] || fail "нет маркера этапа A ($B/new.sha256) — повторите этап A"
MARK=$(cat "$B/new.sha256")
RB="sudo bash $T/rollback.sh"; [[ "$(cat "$B/prev.version" 2>/dev/null)" == *-h14 ]] || RB="$RB --baseline"
if [ -f "$B/deploy-b.switched" ] && [ -f $T/stable_sync.py.new ]; then
  rm -f "$B/deploy-b.switched"   # прошлый запуск упал до переноса синка: синк, health и check_engines не менялись
  echo "прошлый запуск этапа B упал до переключения синка (синк, health и check_engines в работе не менялись) — этап начинается заново"
fi
OTHER=
if [ -f "$B/deploy-b.switched" ]; then        # синк переключён прошлым запуском, этап не закончен
  fail() { echo "этап B не доделан (файлы в работе частично новые), этим запуском ничего не изменено: $*"; exit 1; }
  NEWF=$T/stable_sync.py
  [ -n "$(h14 $NEWF)" ] && compiles $NEWF || fail "в работе синк не v4.5.x-h14 или не компилируется (откат или правка после сбоя) — откатите: $RB, затем этап A"
  if [ "$(sha $NEWF)" != "$MARK" ]; then
    built_with_live_cfg || fail "в работе не та сборка, что ставил этот этап B (откат или замена после сбоя) — откатите: $RB, затем этап A"
    echo "⚠️ синк в работе — сборка этапа A с изменённым конфигом (правка после сбоя) — доделываю с ним"
  fi
  echo "этап B для этой сборки начинался и не закончился (журнал: $B/deploy-b.log) — доделываю из файлов в $T"
  if [ ! -f $U/stable_sync.py ]; then echo "сборки в $U нет — она и не нужна, доделываю по файлам в $T"
  elif ! same_upload "$B"; then OTHER="в $U не та сборка, что собирал этап A: доделана сборка этапа A, загруженную выкатывайте этапами A и B"; echo "⚠️ $OTHER"; fi
else
  for f in stable_sync.py sync_health_api.py check_engines.py rollback.sh; do
    [ -f $U/$f ] || fail "нет $U/$f (каталог загрузки очищен перезагрузкой? загрузите заново)"
  done
  same_upload "$B" || fail "загружена не та сборка, что собирал этап A ($B) — повторите этап A"
  [ -f $T/stable_sync.py.new ] || fail "нет $T/stable_sync.py.new — повторите этап A"
  [ "$(sha $T/stable_sync.py.new)" = "$MARK" ] || fail "stable_sync.py.new не тот, что проверял последний этап A — повторите этап A"
  same_prev || fail "в работе не то, что сохранил этап A ($B: откат или правка после этапа A) — повторите этап A"
  for f in sync_health_api.py check_engines.py; do compiles $U/$f || fail "$U/$f не компилируется"; done
  bash -n $U/rollback.sh || fail "$U/rollback.sh с ошибкой синтаксиса"
  NEWF=$T/stable_sync.py.new
fi
NEWVER=$(h14 $NEWF)
[ -n "$NEWVER" ] || fail "в сборке нет VERSION …-h14"
compiles $NEWF || fail "сборка не компилируется"
systemctl show -p ExecStart --value $S | grep -q "$T/stable_sync.py" || fail "юнит $S запускает не $T/stable_sync.py"
if [ ! -f "$B/deploy-b.switched" ]; then
  install -m "$(stat -c %a $T/sync_health_api.py)" -o root -g root $U/sync_health_api.py $T/sync_health_api.py.new \
    && install -m 0700 -o root -g root $U/check_engines.py $T/check_engines.py.new \
    && install -m 0700 -o root -g root $U/rollback.sh $T/rollback.sh.new || fail "не разложены файлы в $T"
fi
OLDLAST=$(ptr /root/sync-h14.last); PV=$(cat "$B/prev.version" 2>/dev/null)

LOG="$B/deploy-b.log"
trap '' HUP PIPE
exec > >(exec 9>&-; tee -p -a "$LOG") 2>&1     # tee не держит блокировку после выхода этапа
echo "=== этап B: v$NEWVER, кандидат $B ==="
systemctl stop hiddify-child-sync.timer        # не stop сервиса: таймер его Requires=
trap 'set +e; trap - ERR; [ "$(systemctl is-active hiddify-child-sync.timer)" = active ] || systemctl start hiddify-child-sync.timer
      echo "таймер: $(systemctl is-active hiddify-child-sync.timer)"' EXIT
for i in $(seq 1 120); do [ "$(systemctl is-active $S)" = activating ] || break; sleep 5; done
if [ "$(systemctl is-active $S)" = activating ]; then
  if [ -f "$B/deploy-b.switched" ]; then
    echo "прогон синка не закончился за 10 минут — этап B прерван; файлы в работе частично новые (прошлый запуск) — повторите deploy-b.sh позже или откатите: $RB"
  else
    echo "прогон синка не закончился за 10 минут — этап B прерван, файлы в работе не менялись; повторите deploy-b.sh позже"
  fi
  exit 1
fi
if [ ! -f "$B/deploy-b.switched" ]; then       # пока ждали прогон, в работе могли что-то поменять руками
  [ "$(ptr /root/sync-h14.next)" = "$B" ] && [ -f $T/stable_sync.py.new ] && [ "$(sha $T/stable_sync.py.new)" = "$MARK" ] && same_prev \
    || { echo "этап B прерван, файлы в работе не менялись: пока ждали прогон, в работе или в кандидате что-то поменялось — повторите этап A"; exit 1; }
fi

set -e
trap 'echo "⚠️ этап B упал во время переключения — повторите deploy-b.sh (он доделает) или откатите: $RB"' ERR
install -d -m 0700 -o root -g root /var/lib/hiddify-child-sync; chmod 700 /var/lib/hiddify-child-sync
echo "$B" > /root/sync-h14.last                 # точка отката — то, что заменяет этот этап B
if [ -n "$PV" ] && [[ "$PV" != *-h14 ]]; then
  echo "$B" > /root/sync-h14.baseline           # заменяется версия до v4.5 — точка отката к ней
elif [ ! -f /root/sync-h14.baseline ] && [ -n "$OLDLAST" ] && [ -f "$OLDLAST/stable_sync.py.v43" ]; then
  echo "$OLDLAST" > /root/sync-h14.baseline
fi
touch "$B/deploy-b.switched"                    # дальше меняются файлы в работе; сбой доделывает повторный B
[ ! -f $T/rollback.sh.new ] || mv -f $T/rollback.sh.new $T/rollback.sh
if [ -f $T/stable_sync.py.new ]; then
  chmod --reference=$T/stable_sync.py $T/stable_sync.py.new
  mv -f $T/stable_sync.py.new $T/stable_sync.py
fi
[ ! -f $T/sync_health_api.py.new ] || mv -f $T/sync_health_api.py.new $T/sync_health_api.py
[ ! -f $T/check_engines.py.new ] || mv -f $T/check_engines.py.new $T/check_engines.py
install -d -m 0755 /etc/systemd/system/$S.d /run/systemd/system/$S.d
printf '[Service]\n# v4.5.x-h14: бюджет прогона 600 с, затем запас на текущий запрос\nTimeoutStartSec=900\n' \
  > /etc/systemd/system/$S.d/timeout.conf
printf '[Service]\nEnvironment=SYNC_ANON=1\n' > /run/systemd/system/$S.d/anon.conf
systemctl daemon-reload
set +e; trap - ERR
systemctl reset-failed hiddify-sync-api.service 2>/dev/null
systemctl restart hiddify-sync-api.service || echo "⚠️ health API не поднялся — проверьте: systemctl status hiddify-sync-api"
sleep 2
echo "файлы на месте: v$(ver $T/stable_sync.py); sync-api: $(systemctl is-active hiddify-sync-api.service); TimeoutStartUSec=$(systemctl show -p TimeoutStartUSec --value $S)"

[ -z "$OTHER" ] || PROBLEMS+=("$OTHER")
summary() {   # $1 — с какого момента журнал, $2 — какой прогон
  local res runs; res=$(systemctl show -p Result --value $S)
  echo "Result=$res ExecMainStatus=$(systemctl show -p ExecMainStatus --value $S)"
  journalctl -u $S --since "@$1" --no-pager -o cat \
    | grep -vE "Пользователь u:[0-9a-f]+: parent=|✅ Обновлён трафик u:" | cut -c1-240
  python3 -c "import json; d=json.load(open('/var/lib/hiddify-child-sync/status.json')); print({k:d.get(k) for k in ('version','ok','steps','parent_count','expected_count','local_count','created','patched','sql_fixed','deleted','blocked','unblocked','deferred_inactive','held_blocks','held_deletes','backlog','stuck','errors','last_error','consecutive_failures','duration_s')})" 2>/dev/null \
    || echo "status.json не прочитан"
  runs=$(journalctl -u $S --since "@$1" --no-pager -o cat | grep -c 'Starting Stable')
  [ "$runs" -gt 0 ] || PROBLEMS+=("$2: прогона не было (условие юнита не выполнено?)")
  [ "$res" = success ] || PROBLEMS+=("$2: Result=$res")
}

T0=$(date +%s)
echo "=== реальный прогон 1 (systemctl start) ==="
systemctl start $S
summary $T0 "прогон 1"
echo "=== движки (повтор до 6 раз — apply-users догоняет за секунды) ==="
engines 6 "$@"

echo "=== таймер (Requires= запускает прогон 2) ==="
T1=$(date +%s)
systemctl start hiddify-child-sync.timer
sleep 3
for i in $(seq 1 120); do [ "$(systemctl is-active $S)" = activating ] || break; sleep 5; done
[ "$(systemctl is-active $S)" != activating ] || PROBLEMS+=("прогон 2 не закончился за 10 минут")
summary $T1 "прогон 2"
n=$(journalctl -u $S --since "@$T1" --no-pager -o cat | grep -cE '✏️ Обновлён|➕ Создан|🗑️  Удалён|🛠 Без перезагрузок')
[ "$n" = 0 ] || PROBLEMS+=("прогон 2: изменений $n, ожидалось 0")
echo "=== health (таймер уже запущен) ==="
health || PROBLEMS+=("health не healthy")
systemctl list-timers hiddify-child-sync.timer --no-pager | sed -n 2p
touch "$B/deploy-b.done"; rm -f /root/sync-h14.next
echo "этап B завершён: в работе v$NEWVER, точка отката — $B"

echo "=== повторный dry-run (ожидается 0 изменений) ==="
DRY=$(cd $T && SYNC_ANON=1 $PY $T/stable_sync.py --dry-run 2>&1)
echo "$DRY" | grep -E "\[dry-run\]|Синхронизация:|dry-run завершён" | cut -c1-240
echo "изменений в плане dry-run: $(echo "$DRY" | grep -cE '\[dry-run\] (➕ POST|✏️ PATCH|🛠 SQL|🗑️ DELETE)')"
verdict "выкачено (deploy-b.done)"
