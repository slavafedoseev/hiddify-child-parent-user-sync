#!/bin/bash
# Откат синка. Без аргументов — к версии, которую заменила последняя выкатка (/root/sync-h14.last).
# --baseline — к версии до первой v4.5 (/root/sync-h14.baseline). На Hiddify 14 это v4.3: она не создаёт
# пользователей, не применяет выключения parent и при этом пишет success — только осознанно; обычно
# вместо неё — аварийный режим SYNC_SKIP_USERS=1 (RUNBOOK-v4.5-h14 §6). Созданных синком пользователей
# не трогает (дамп таблицы user до выкатки — в каталоге бэкапа). Всё проверяется до остановки таймера;
# после остановки таймер запускается снова при любом выходе (trap), обрыв ssh откат не прерывает; пока идёт
# этап выкатки (блокировка /root/sync-h14.lock), откат отказывает. Незаконченный этап B (кандидат
# /root/sync-h14.next с deploy-b.switched без deploy-b.done) откат снимает вместе с остатками *.new и
# SYNC_ANON — дальше новая выкатка этапами A и B. Если в работе уже то, что сохранено, файлы не трогает.
# И тогда, и после отката называет следующую точку цепочки prev-last, отличную от работающей.
# Код выхода: 0 — откат выполнен или снят незаконченный этап B; 1 — отказ, ничего не изменено.
set -uo pipefail
T=${SYNC_TOOLS:-/opt/hiddify-custom/tools}; S=hiddify-child-sync.service
fail() { echo "откат отменён, ничего не изменено: $*"; exit 1; }
{ exec 9>>/root/sync-h14.lock; } 2>/dev/null || fail "не открыть /root/sync-h14.lock — запускайте от root (sudo)"
flock -n 9 || fail "идёт этап выкатки или другой откат (обрыв ssh их не прерывает) — дождитесь окончания"
PTR=/root/sync-h14.last; [ "${1:-}" = --baseline ] && PTR=/root/sync-h14.baseline
B=$(readlink -f "$(cat $PTR 2>/dev/null)") && [ -d "$B" ] || fail "нет $PTR или каталога, на который он указывает"
BL=$(readlink -f "$(cat /root/sync-h14.baseline 2>/dev/null)" 2>/dev/null || true)
suf() { local n; for n in prev v43; do [ -f "$1/stable_sync.py.$n" ] && { echo $n; return 0; }; done; return 1; }
ver_of() { local v; v=$(sed -n "s/^VERSION = '\(.*\)'$/\1/p" "$1/stable_sync.py.$(suf "$1")"); echo "${v:-4.3}"; }
in_work() {   # всё, что сохранено в каталоге $1 (синк, health, check_engines), уже в работе
  local s; s=$(suf "$1") || return 1
  cmp -s "$1/stable_sync.py.$s" $T/stable_sync.py && cmp -s "$1/sync_health_api.py.$s" $T/sync_health_api.py || return 1
  [ ! -f "$1/check_engines.py.$s" ] || cmp -s "$1/check_engines.py.$s" $T/check_engines.py
}
to_baseline() {   # как откатиться на каталог $1 с версией до v4.5
  if [ -n "$BL" ] && [ "$1" -ef "$BL" ]; then echo "rollback.sh --baseline (/root/sync-h14.baseline → $BL)"
  else echo "echo $1 > /root/sync-h14.baseline; затем rollback.sh --baseline (сейчас .baseline → ${BL:-нет})"; fi
}
next_point() {   # следующая точка цепочки prev-last после $1, отличная от того, что в работе
  local k=$1 i v
  for i in $(seq 1 20); do
    [ -f "$k/prev-last" ] || break
    k=$(readlink -f "$(cat "$k/prev-last")" 2>/dev/null) && [ -d "$k" ] || break
    suf "$k" > /dev/null && ! in_work "$k" || continue
    v=$(ver_of "$k")
    if [[ "$v" == *-h14 ]]; then
      echo "предыдущая точка отката: $k (v$v) — откатиться дальше: echo $k > /root/sync-h14.last; затем снова rollback.sh"
    else
      echo "предыдущая точка отката: $k (v$v, до v4.5) — только осознанно: $(to_baseline "$k")"
    fi
    return 0
  done
  echo "дальше по цепочке точек отката нет"
}
SUF=$(suf "$B") || fail "в $B нет сохранённого stable_sync.py"
F=stable_sync.py.$SUF
[ -f "$B/sync_health_api.py.$SUF" ] || fail "в $B нет sync_health_api.py.$SUF"
[ -d $T ] && [ -f $T/stable_sync.py ] || fail "нет $T/stable_sync.py (другой каталог — задайте SYNC_TOOLS)"
systemctl show -p ExecStart --value $S | grep -q "$T/stable_sync.py" || fail "юнит $S запускает не $T/stable_sync.py"
VER=$(ver_of "$B")
NX=$(readlink -f "$(cat /root/sync-h14.next 2>/dev/null)" 2>/dev/null || true)
UNFIN=; [ -n "$NX" ] && [ -f "$NX/deploy-b.switched" ] && [ ! -f "$NX/deploy-b.done" ] && UNFIN=$NX
drop_unfinished() {   # файлы этого кандидата в работу больше не пойдут: снять его, остатки .new и SYNC_ANON
  rm -f /root/sync-h14.next $T/stable_sync.py.new $T/sync_health_api.py.new $T/check_engines.py.new $T/rollback.sh.new \
        /run/systemd/system/$S.d/anon.conf
  echo "незаконченный этап B кандидата $UNFIN снят — чтобы выкатить сборку снова, повторите этапы A и B"
}
if in_work "$B"; then
  if [ -n "$UNFIN" ]; then
    echo "в работе уже то, что сохранено в $B (v$VER) — файлы не трогаю"
    drop_unfinished; systemctl daemon-reload
    next_point "$B"; exit 0
  fi
  echo "откат отменён, ничего не изменено: в работе уже то, что сохранено в $B (v$VER) — откатывать нечего"
  next_point "$B"
  exit 1
fi
if [[ "$VER" != *-h14 ]]; then
  [ "${1:-}" = --baseline ] || fail "в $B сохранена v$VER — не для Hiddify 14; откат на версию до v4.5 только явно: $(to_baseline "$B")"
  echo "⚠️ откат на v$VER: на Hiddify 14 она не создаёт пользователей и не применяет выключения parent, а пишет success и healthy"
fi

trap '' HUP PIPE
systemctl stop hiddify-child-sync.timer
trap 'systemctl start hiddify-child-sync.timer; echo "таймер: $(systemctl is-active hiddify-child-sync.timer)"' EXIT
echo "жду окончания текущего прогона синка (до 10 минут)…"
for i in $(seq 1 120); do [ "$(systemctl is-active $S)" = activating ] || break; sleep 5; done
[ "$(systemctl is-active $S)" != activating ] || { echo "прогон синка не закончился за 10 минут — откат прерван, файлы не тронуты"; exit 1; }
cp -p "$B/$F" $T/stable_sync.py || exit 1
cp -p "$B/sync_health_api.py.$SUF" $T/sync_health_api.py || exit 1
[ ! -f "$B/check_engines.py.$SUF" ] || cp -p "$B/check_engines.py.$SUF" $T/check_engines.py
[[ "$VER" == *-h14 ]] || rm -f /etc/systemd/system/$S.d/timeout.conf
[ -z "$UNFIN" ] || drop_unfinished
rm -f /run/systemd/system/$S.d/anon.conf
systemctl daemon-reload
systemctl reset-failed hiddify-sync-api.service 2>/dev/null
systemctl restart hiddify-sync-api.service || echo "⚠️ health API не поднялся — проверьте: systemctl status hiddify-sync-api"
echo "откат выполнен из $B: в работе v$(sed -n "s/^VERSION = '\(.*\)'$/\1/p" $T/stable_sync.py | grep . || echo 4.3)"
next_point "$B"
exit 0
