#!/bin/bash
# Откат синка. Без аргументов — к версии, которую заменила последняя выкатка (/root/sync-h14.last).
# --baseline — к версии до первой v4.5 (/root/sync-h14.baseline). На Hiddify 14 это v4.3: она не создаёт
# пользователей, не применяет выключения parent и при этом пишет success — только осознанно; обычно
# вместо неё — аварийный режим SYNC_SKIP_USERS=1 (RUNBOOK-v4.5-h14 §6). Созданных синком пользователей
# не трогает (дамп таблицы user до выкатки — в каталоге бэкапа). Всё проверяется до остановки таймера;
# после остановки таймер запускается снова при любом выходе (trap), обрыв ssh откат не прерывает.
set -uo pipefail
T=${SYNC_TOOLS:-/opt/hiddify-custom/tools}; S=hiddify-child-sync.service
fail() { echo "откат отменён, ничего не изменено: $*"; exit 1; }
PTR=/root/sync-h14.last; [ "${1:-}" = --baseline ] && PTR=/root/sync-h14.baseline
B=$(readlink -f "$(cat $PTR 2>/dev/null)") && [ -d "$B" ] || fail "нет $PTR или каталога, на который он указывает"
F=; for n in stable_sync.py.prev stable_sync.py.v43; do [ -f "$B/$n" ] && { F=$n; break; }; done
[ -n "$F" ] || fail "в $B нет сохранённого stable_sync.py"
SUF=${F##*.}
[ -f "$B/sync_health_api.py.$SUF" ] || fail "в $B нет sync_health_api.py.$SUF"
[ -d $T ] && [ -f $T/stable_sync.py ] || fail "нет $T/stable_sync.py (другой каталог — задайте SYNC_TOOLS)"
systemctl show -p ExecStart --value $S | grep -q "$T/stable_sync.py" || fail "юнит $S запускает не $T/stable_sync.py"
VER=$(sed -n "s/^VERSION = '\(.*\)'$/\1/p" "$B/$F"); VER=${VER:-4.3}
same=1
cmp -s "$B/$F" $T/stable_sync.py && cmp -s "$B/sync_health_api.py.$SUF" $T/sync_health_api.py || same=0
[ ! -f "$B/check_engines.py.$SUF" ] || cmp -s "$B/check_engines.py.$SUF" $T/check_engines.py || same=0
[ $same = 0 ] || fail "в работе уже то, что сохранено в $B (v$VER) — откатывать нечего"
if [[ "$VER" != *-h14 ]]; then
  [ "${1:-}" = --baseline ] || fail "в $B сохранена v$VER — не для Hiddify 14; откат на неё только явно: rollback.sh --baseline"
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
rm -f /run/systemd/system/$S.d/anon.conf
systemctl daemon-reload
systemctl reset-failed hiddify-sync-api.service 2>/dev/null
systemctl restart hiddify-sync-api.service || echo "⚠️ health API не поднялся — проверьте: systemctl status hiddify-sync-api"
echo "откат выполнен из $B: в работе v$(sed -n "s/^VERSION = '\(.*\)'$/\1/p" $T/stable_sync.py | grep . || echo 4.3)"
if [ -f "$B/prev-last" ]; then
  echo "предыдущая точка отката: $(cat "$B/prev-last") — откатиться дальше: echo $(cat "$B/prev-last") > /root/sync-h14.last; затем снова rollback.sh"
fi
