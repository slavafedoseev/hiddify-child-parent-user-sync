#!/bin/bash
# Этап C (usage: deploy-c.sh <epoch после этапа B>): прогоны таймера с этого момента, status.json, health,
# движки; прибрать дубли бэкапов (точки отката /root/sync-h14.last, .baseline, кандидат .next и их цепочка
# prev-last остаются). Только если всё в порядке (код 0), C снимает runtime-drop-in SYNC_ANON=1 и удаляет
# каталог загрузки — если загруженная сборка в работе (иначе загрузку оставляет); при проблемах оставляет
# всё, и после разбора C повторяется той же командой. Если кандидат этапа A не выкачен (этап B не запускался, прерван или не доделан) или идёт
# другой этап (блокировка /root/sync-h14.lock) — ничего не делает.
# Код выхода: 0 — всё в порядке; 1 — этап не выполнен, ничего не изменено; 2 — проверка нашла проблемы
# (список в конце вывода). Секреты, имена и UUID не печатает: строки журнала, где могут быть имена (❌, ⏳), —
# только их число.
set -uo pipefail
T=${SYNC_TOOLS:-/opt/hiddify-custom/tools}; U=${SYNC_UPLOAD:-/tmp/sync14}; PY=/opt/hiddify-manager/.venv313/bin/python
S=hiddify-child-sync.service
SINCE=${1:-}
[ -n "$SINCE" ] || { echo "usage: deploy-c.sh <epoch после этапа B> — этап C отменён, ничего не изменено"; exit 1; }
{ exec 9>>/root/sync-h14.lock; } 2>/dev/null || { echo "не открыть /root/sync-h14.lock — запускайте от root (sudo); этап C отменён, ничего не изменено"; exit 1; }
flock -n 9 || { echo "идёт другой этап выкатки или откат (обрыв ssh его не прерывает) — дождитесь окончания; этап C отменён, ничего не изменено"; exit 1; }
ptr() { local d; d=$(readlink -f "$(cat "$1" 2>/dev/null)" 2>/dev/null) && [ -n "$d" ] && [ -d "$d" ] && echo "$d"; }
NX=$(ptr /root/sync-h14.next)
if [ -n "$NX" ] && [ ! -f "$NX/deploy-b.done" ]; then
  if [ -f "$NX/deploy-b.switched" ]; then
    RB="sudo bash $T/rollback.sh"; [[ "$(cat "$NX/prev.version" 2>/dev/null)" == *-h14 ]] || RB="$RB --baseline"
    echo "этап B кандидата $NX начат и не закончен — запустите deploy-b.sh, он доделает, или откатите: $RB;"
    echo "этап C отменён, ничего не изменено"
  else
    echo "кандидат этапа A $NX не выкачен: этап B не запускался или прерван до переключения (файлы в работе не менялись)"
    echo "— запустите deploy-b.sh; если кандидат не нужен — rm /root/sync-h14.next; этап C отменён, ничего не изменено"
  fi
  exit 1
fi
same_aux() { local f; for f in sync_health_api.py check_engines.py rollback.sh; do [ -f $U/$f ] && cmp -s $U/$f $T/$f || return 1; done; }
upload_in_work() {   # загруженная сборка уже в работе целиком — тот же признак, что у этапа A
  local tmp v rc=1
  [ -f $U/stable_sync.py ] && [ -f $U/build_new.py ] && same_aux || return 1
  v=$(sed -n "s/^VERSION = '\(.*-h14\)'$/\1/p" $U/stable_sync.py); [ -n "$v" ] || return 1
  tmp=$(mktemp) || return 1
  python3 -I $U/build_new.py $T/stable_sync.py $U/stable_sync.py "$tmp" "$v" > /dev/null 2>&1 && cmp -s "$tmp" $T/stable_sync.py && rc=0
  rm -f "$tmp"; return $rc
}
PROBLEMS=()
RES=$(systemctl show -p Result --value $S)
echo "последний прогон: Result=$RES ExecMainStatus=$(systemctl show -p ExecMainStatus --value $S)"
[ "$RES" = success ] || PROBLEMS+=("последний прогон: Result=$RES")
FROM=$(date -d "@$SINCE" '+%H:%M:%S')
echo "=== прогоны с $FROM ==="
J=$(journalctl -u $S --since "@$SINCE" --no-pager -o cat)
# строки ❌ и ⏳ могут содержать имена (прогоны без SYNC_ANON: сбой этапа B, перезагрузка) — только их число
echo "$J" | grep -E "Starting Stable|✅ Синхронизация:|Stable sync completed|завершён с ошибками|⏸|Main process exited|Failed" | cut -c1-240
echo "строк ❌: $(echo "$J" | grep -c '❌'), ⏳: $(echo "$J" | grep -c '⏳') — сами строки: journalctl -u $S на сервере"
RUNS=$(echo "$J" | grep -c 'Starting Stable')
echo "прогонов: $RUNS, с изменениями через API: $(echo "$J" | grep -cE '✏️ Обновлён|➕ Создан|🗑️  Удалён')"
[ "$RUNS" -gt 0 ] || PROBLEMS+=("с $FROM не было ни одного прогона — повторите C позже")
python3 -c "import json; d=json.load(open('/var/lib/hiddify-child-sync/status.json')); print({k:d.get(k) for k in ('version','ok','parent_count','expected_count','local_count','created','patched','sql_fixed','deleted','held_blocks','held_deletes','backlog','stuck','errors','last_error','consecutive_failures','duration_s')})" 2>/dev/null \
  || echo "status.json не прочитан"
echo "=== health ==="
curl -s --max-time 10 http://127.0.0.1:8081/api/v2/hiddify-sync/health | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("health API не ответил"); sys.exit(1)
print(d.get("status"), d.get("reasons"), "users=", (d.get("database") or {}).get("user_count"))
sys.exit(0 if d.get("status") == "healthy" else 1)' || PROBLEMS+=("health не healthy")
echo "=== движки ==="
RETRY=; grep -q -- '--retry' $T/check_engines.py 2>/dev/null && RETRY="--retry 3"   # check_engines 4.5-h14 (после отката) ключа не знает
SYNC_SCRIPT=$T/stable_sync.py $PY $T/check_engines.py $RETRY; rc=$?
[ $rc = 0 ] || PROBLEMS+=("движки: код check_engines $rc (1 — расхождения, 2 — проверены не все)")
KEEPU=
if [ -f $U/stable_sync.py ] && ! upload_in_work; then
  KEEPU="сборка в $U не та, что в работе (откат, доделка другой сборки или «посмотреть план») — загрузку не удаляю"
  echo "$KEEPU"
fi
if [ ${#PROBLEMS[@]} -eq 0 ]; then
  echo "=== снять SYNC_ANON ==="
  rm -f /run/systemd/system/$S.d/anon.conf; rmdir /run/systemd/system/$S.d 2>/dev/null; systemctl daemon-reload
  ANON=$(systemctl show -p Environment --value $S | grep -c SYNC_ANON)
  echo "SYNC_ANON в окружении юнита: $ANON"
  [ "$ANON" = 0 ] || PROBLEMS+=("SYNC_ANON не снят")
fi

echo "=== прибрать дубли бэкапов ==="
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
for p in /root/sync-h14.last /root/sync-h14.baseline /root/sync-h14.next; do
  if [ -f $p ]; then echo "$p → $(cat $p) (v$(cat "$(cat $p)/prev.version" 2>/dev/null || echo 4.3))"; fi
done
if [ ${#PROBLEMS[@]} -eq 0 ]; then
  if [ -z "$KEEPU" ]; then rm -rf "${U:?}"; echo "ИТОГ этапа C: всё в порядке, SYNC_ANON снят, каталог загрузки удалён"
  else echo "ИТОГ этапа C: всё в порядке, SYNC_ANON снят; $KEEPU"; fi
  exit 0
fi
echo "ИТОГ этапа C: проверка нашла проблемы:"
printf '  - %s\n' "${PROBLEMS[@]}"
echo "SYNC_ANON и каталог загрузки оставлены — после разбора C повторяется той же командой; аварийный режим и откат — RUNBOOK §6"
exit 2
