#!/bin/bash
# Песочница для deploy/v14 (deploy-a/b/c и rollback): заглушки systemctl/mysqldump/journalctl/curl/install/
# mv/sleep/date/tee и venv-python, пути /root, /var/lib, /etc/systemd, /run/systemd, venv заменены на каталоги
# песочницы. Модель: у таймера и сервиса раздельное состояние; start сервиса или таймера (Requires=, и
# при уже активном таймере — как systemd) — прогон: строки журнала с меткой часов песочницы (date +%s их двигает), Result прогона;
# journalctl понимает --since; health — как handle_health: при неактивном таймере unhealthy. Паузы мгновенные,
# метки каталогов бэкапа уникальны, compile(…) — настоящий.
# Флаги: $R/stuck — прогон «висит» (activating); $R/stuck.after.timer — зависает прогон от старта таймера;
# $R/norun — прогон не запускается (условие юнита); $R/result и $R/result.<n> — Result всех / n-го прогона;
# $R/journal.changes(.<n>) — в прогоне есть изменение; $R/journal.error — в прогоне ошибка по пользователю
# (с именем, если SYNC_ANON не действует); $R/during.wait — команда, которая выполнится при ожидании прогона;
# $R/hup.on.run — прогон шлёт HUP этапу; $R/health.json и $R/health.down — ответ health API; $R/ce.rc — код
# check_engines (вызовы — в $R/ce.args); $R/fail.start, fail.restart, fail.daemon-reload, fail.mv.health,
# fail.mv.sync — сбои команд.
# usage: bash tests/v14/deploy_sandbox.sh   (SANDBOX_DEP=<каталог> — прогнать другие скрипты deploy/v14)
set -u
CODE=$(cd "$(dirname "$0")/../../src/v14" && pwd); DEP=$(cd "${SANDBOX_DEP:-$(dirname "$0")/../../deploy/v14}" && pwd)
R=$(mktemp -d); trap 'chmod -R u+rwx "${R:?}" 2>/dev/null; rm -rf "${R:?}"' EXIT
T=$R/tools; U=$R/upload; mkdir -p $T $U $R/bin $R/root $R/etc $R/run $R/varlib
PASS=0; FAILN=0
ok() { if eval "$2"; then PASS=$((PASS+1)); echo "  ✓ $1"; else FAILN=$((FAILN+1)); echo "  ✗ $1"; fi; }
SLEEP=$(command -v sleep); DATE=$(command -v date)

# --- заглушки
cat > $R/bin/systemctl <<EOF
#!/bin/bash
echo "\$*" >> $R/systemctl.log
run() {   # прогон синка (oneshot)
  local n c
  n=\$(( \$(cat $R/run.n 2>/dev/null || echo 0) + 1 )); echo \$n > $R/run.n
  c=\$(cat $R/clock); echo \$((c + 10)) > $R/clock
  [ -f $R/hup.on.run ] && kill -HUP \$PPID
  [ -f $R/norun ] && return 0
  echo "\$c [x] === ⚙ Starting Stable Accumulative Sync v4.5.1-h14 ===" >> $R/journal
  if [ -f $R/journal.changes ] || [ -f $R/journal.changes.\$n ]; then echo "\$c [x] ✏️ Обновлён u:aaaaaaaa: ['enable']" >> $R/journal; fi
  if [ -f $R/journal.error ]; then
    if [ -f $R/run/hiddify-child-sync.service.d/anon.conf ]; then echo "\$c [x] ❌ Создание u:bbbbbbbb: HTTP 400" >> $R/journal
    else echo "\$c [x] ❌ Создание Ivan Petrov: HTTP 400" >> $R/journal; fi
  fi
  cat $R/result.\$n 2>/dev/null > $R/last.result || cat $R/result 2>/dev/null > $R/last.result || echo success > $R/last.result
}
u=\${2:-}
case "\$1" in
  is-active)
    if [ "\$u" = hiddify-child-sync.service ]; then
      if [ -f $R/during.wait ]; then bash $R/during.wait; rm -f $R/during.wait; fi
      [ -f $R/stuck ] && { echo activating; exit 0; }
    fi
    cat $R/state.\$u 2>/dev/null || echo inactive ;;
  stop) echo inactive > $R/state.\$u ;;
  start) [ -f $R/fail.start ] && exit 1
    case "\$u" in
      hiddify-child-sync.service) run ;;
      hiddify-child-sync.timer)          # как systemd: Requires= запускает прогон и при уже активном таймере
        echo active > $R/state.\$u; [ -f $R/stuck.after.timer ] && touch $R/stuck; run ;;
      *) echo active > $R/state.\$u ;;
    esac ;;
  restart) [ -f $R/fail.restart ] && exit 1; echo active > $R/state.\$u ;;
  daemon-reload) [ -f $R/fail.daemon-reload ] && exit 1 ;;
  show) case "\$*" in *ExecStart*) cat $R/execstart 2>/dev/null || echo "{ path=/x ; argv[]=/py $T/stable_sync.py ; }";; *Result*) cat $R/last.result 2>/dev/null || echo success;; *ExecMainStatus*) echo 0;; *Environment*) cat $R/run/hiddify-child-sync.service.d/anon.conf 2>/dev/null | grep -o 'SYNC_ANON=1';; *) echo 15min;; esac ;;
  list-timers) printf 'NEXT\nsoon\n' ;;
esac
exit 0
EOF
cat > $R/bin/mysqldump <<'EOF'
#!/bin/bash
echo "INSERT INTO user VALUES (1);"
EOF
cat > $R/bin/journalctl <<EOF
#!/bin/bash
since=0; prev=
for a in "\$@"; do [ "\$prev" = --since ] && since=\${a#@}; prev=\$a; done
awk -v s="\$since" '\$1 >= s { sub(/^[0-9]+ /, ""); print }' $R/journal 2>/dev/null
exit 0
EOF
cat > $R/bin/curl <<EOF
#!/bin/bash
[ -f $R/health.down ] && exit 7
[ -f $R/health.json ] && { cat $R/health.json; exit 0; }
if [ "\$(cat $R/state.hiddify-child-sync.timer 2>/dev/null)" = active ]; then
  echo '{"status": "healthy", "reasons": [], "database": {"user_count": 3}}'
else
  echo '{"status": "unhealthy", "reasons": ["таймер синка не активен"], "database": {"user_count": 3}}'
fi
EOF
cat > $R/bin/install <<'EOF'
#!/bin/bash
args=(); skip=0
for a in "$@"; do if [ $skip = 1 ]; then skip=0; continue; fi; case "$a" in -o|-g) skip=1;; *) args+=("$a");; esac; done
exec /usr/bin/install "${args[@]}"
EOF
cat > $R/bin/mv <<EOF
#!/bin/bash
[ -f $R/fail.mv.health ] && [[ "\$*" == *sync_health_api.py.new* ]] && { echo "mv: сбой (песочница)" >&2; exit 1; }
[ -f $R/fail.mv.sync ] && [[ "\$*" == *stable_sync.py.new* ]] && { echo "mv: сбой (песочница)" >&2; exit 1; }
exec /usr/bin/mv "\$@"
EOF
cat > $R/bin/sleep <<'EOF'
#!/bin/bash
exit 0
EOF
cat > $R/bin/tee <<EOF
#!/bin/bash
/usr/bin/tee "\$@"; rc=\$?; $SLEEP 0.3; exit \$rc        # хвост вывода этапа живёт чуть дольше самого этапа
EOF
cat > $R/bin/date <<EOF
#!/bin/bash
if [ "\$*" = "+%Y%m%d-%H%M%S" ]; then
  n=\$(( \$(cat $R/date.n 2>/dev/null || echo 0) + 1 )); echo \$n > $R/date.n; printf '20990101-%06d\n' \$n; exit 0
fi
if [ "\$*" = "+%s" ]; then c=\$(cat $R/clock); echo \$((c + 10)) > $R/clock; echo \$c; exit 0; fi
exec $DATE "\$@"
EOF
cat > $R/bin/pystub <<EOF
#!/bin/bash
[ "\$1" = -c ] && exec python3 "\$@"         # compile(…) этапов — настоящий
case "\$*" in
  *--dry-run*) echo "[t] === dry-run ==="; echo "[t] [dry-run] ➕ POST u:aaaaaaaa"; exit 0 ;;
  *check_engines.py*) echo "SYNC_SCRIPT=\${SYNC_SCRIPT:-} \$*" >> $R/ce.args; rc=\$(cat $R/ce.rc 2>/dev/null || echo 0)
    if [ "\$rc" = 0 ]; then echo "ИТОГ: движки совпадают с панелью"; else echo "ИТОГ: расхождения (заглушка)"; fi; exit \$rc ;;
  *) exec python3 "\$@" ;;
esac
EOF
chmod +x $R/bin/*
for f in deploy-a.sh deploy-b.sh deploy-c.sh rollback.sh; do
  sed -e "s#/root/#$R/root/#g" -e "s#/var/lib/hiddify-child-sync#$R/varlib#g" \
      -e "s#/etc/systemd/system#$R/etc#g" -e "s#/run/systemd/system#$R/run#g" \
      -e "s#/opt/hiddify-manager/.venv313/bin/python#$R/bin/pystub#g" "$DEP/$f" > $R/$f
done
export PATH=$R/bin:$PATH SYNC_TOOLS=$T SYNC_UPLOAD=$U
mkstatus() { echo '{"version": "x", "ok": true}' > $R/varlib/status.json; }

v43() { printf 'PARENT_URL = "https://p.example.net/AbCdEfPath"\nAPI_KEY = "k-live"\nDB_CONFIG = {\n    %s\n}\nprint("v4.3")\n' "'user': 'root'"; }
upload() {   # новый код в каталог загрузки (версия $1)
  mkdir -p $U; sed "s/^VERSION = .*/VERSION = '$1'/" $CODE/stable_sync.py > $U/stable_sync.py
  cp $CODE/sync_health_api.py $CODE/check_engines.py $DEP/build_new.py $DEP/rollback.sh $U/
}
hotfix() { upload "$1"; echo "# $2" >> $U/sync_health_api.py; }      # сборка, где изменился только health
reset() {
  rm -rf "${T:?}"/* "${R:?}"/root/* "${R:?}"/etc/* "${R:?}"/run/* "${R:?}"/varlib/*
  rm -f "${R:?}"/state.* "${R:?}"/fail.* "${R:?}"/result.* "${R:?}"/journal.* \
        "${R:?}"/{systemctl.log,execstart,stuck,stuck.after.timer,norun,during.wait,hup.on.run,result,last.result,journal,run.n,health.json,health.down,ce.rc,ce.args}
  mkdir -p $T $U; echo active > $R/state.hiddify-child-sync.timer; echo 1000000 > $R/clock; mkstatus
}
TIMER=$R/state.hiddify-child-sync.timer
st_de03() {   # как de03 до 4.5.1: в работе 4.5-h14 с её check_engines, .last — каталог с .v43, .baseline нет
  OLD=$R/root/sync-h14-old; mkdir -p $OLD; v43 > $OLD/stable_sync.py.v43; echo 'print("h43")' > $OLD/sync_health_api.py.v43
  echo $OLD > $R/root/sync-h14.last
  v43 | sed '1a VERSION = '"'"'4.5-h14'"'"'' > $T/stable_sync.py; echo 'print("health 4.5")' > $T/sync_health_api.py
  echo 'print("ce 4.5")' > $T/check_engines.py; chmod 700 $T/stable_sync.py
}
run_a() { bash $R/deploy-a.sh > $R/a.out 2>&1; }
run_b() { bash $R/deploy-b.sh "$@" > $R/b.out 2>&1; }
run_c() { bash $R/deploy-c.sh 0 > $R/c.out 2>&1; }
run_rb() { bash $R/rollback.sh "$@" > $R/r.out 2>&1; }
nonew() { ! ls $T/*.new > /dev/null 2>&1; }

echo "=== 1. первое обновление с v4.3: A, B, откат"
reset; v43 > $T/stable_sync.py; echo 'print("health v4.3")' > $T/sync_health_api.py; chmod 700 $T/stable_sync.py; upload 4.5.1-h14
run_a; rc=$?; ok "A прошёл" "[ $rc -eq 0 ]"
B1=$(cat $R/root/sync-h14.next)
ok "A: кандидат .next, .last не тронут" "[ -d \"$B1\" ] && [ ! -f $R/root/sync-h14.last ]"
ok "A: .baseline не пишет (его пишет B)" "[ ! -f $R/root/sync-h14.baseline ] && grep -qx 4.3 $B1/prev.version"
ok "маркеры A записаны" "[ -s $B1/new.sha256 ] && [ -s $B1/upload-aux.sha256 ]"
ok "конфиг перенесён" "grep -q 'AbCdEfPath' $T/stable_sync.py.new && grep -q \"^VERSION = '4.5.1-h14'\" $T/stable_sync.py.new"
run_b u:aaaaaaaa; rc=$?
ok "B: код 0, в работе 4.5.1" "[ $rc -eq 0 ] && grep -q \"^VERSION = '4.5.1-h14'\" $T/stable_sync.py"
ok "B: ровно два прогона (trap не запускает лишний)" "[ \"\$(cat $R/run.n)\" = 2 ]"
ok "B: таймер запущен" "grep -qx active $TIMER"
ok "B: журнал этапа" "grep -q 'реальный прогон 1' $B1/deploy-b.log"
ok "B: .last = кандидат, .next снят, done" "[ \"\$(cat $R/root/sync-h14.last)\" = \"$B1\" ] && [ ! -f $R/root/sync-h14.next ] && [ -f $B1/deploy-b.done ]"
ok "B: check_engines и rollback.sh рядом с синком, остатков .new нет" "[ -f $T/check_engines.py ] && [ -x $T/rollback.sh ] && nonew"
ok "B: baseline = кандидат (бэкап v4.3)" "[ \"\$(cat $R/root/sync-h14.baseline)\" = \"$B1\" ]"
: > $R/systemctl.log; run_rb; rc=$?
ok "откат без --baseline на v4.3 — отказ до остановки таймера" "[ $rc -ne 0 ] && grep -q 'только явно' $R/r.out && grep -q \"4.5.1-h14\" $T/stable_sync.py && ! grep -q '^stop' $R/systemctl.log"
run_rb --baseline
ok "откат --baseline → v4.3 с предупреждением" "grep -q 'v4.3' $T/stable_sync.py && grep -q '⚠️' $R/r.out && [ ! -f $R/etc/hiddify-child-sync.service.d/timeout.conf ]"

echo "=== 2. de03: 4.5-h14 → 4.5.1-h14, старый .last с .v43"
reset; st_de03; rm -f $T/check_engines.py; upload 4.5.1-h14
run_a; rc=$?; ok "A прошёл" "[ $rc -eq 0 ]"
B2=$(cat $R/root/sync-h14.next)
ok "baseline = старый .v43 (старая раскладка), .last пока прежний" "[ \"\$(cat $R/root/sync-h14.baseline)\" = \"$OLD\" ] && [ \"\$(cat $R/root/sync-h14.last)\" = \"$OLD\" ]"
ok "кандидат = бэкап 4.5-h14" "grep -qx 4.5-h14 $B2/prev.version"
ok "PARENT_TLS_VERIFY по умолчанию нового кода" "grep -q '^PARENT_TLS_VERIFY = True' $T/stable_sync.py.new"
run_b
ok "после B .last = кандидат, baseline прежний" "[ \"\$(cat $R/root/sync-h14.last)\" = \"$B2\" ] && [ \"\$(cat $R/root/sync-h14.baseline)\" = \"$OLD\" ]"
run_rb
ok "откат → 4.5-h14 (prev)" "grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py && grep -q 'health 4.5' $T/sync_health_api.py"
: > $R/systemctl.log; run_rb; rc=$?
ok "повторный откат — «нечего», таймер не трогали" "[ $rc -ne 0 ] && grep -q 'откатывать нечего' $R/r.out && ! grep -q '^stop' $R/systemctl.log"

echo "=== 3. B без каталога загрузки (перезагрузка между A и B) — таймер не трогаем"
reset; v43 > $T/stable_sync.py; echo 'print("h")' > $T/sync_health_api.py; upload 4.5.1-h14
run_a; rm -rf "${U:?}"/*; : > $R/systemctl.log
run_b; rc=$?
ok "B отказал до остановки таймера" "[ $rc -ne 0 ] && grep -q 'загрузите заново' $R/b.out && ! grep -q '^stop' $R/systemctl.log && grep -qx active $TIMER"
ok "живой файл не тронут" "grep -q 'v4.3' $T/stable_sync.py"
upload 4.5.1-h14; echo '# изменено' >> $T/stable_sync.py.new; : > $R/systemctl.log
run_b; rc=$?
ok "B: .new не тот, что проверял A — отказ до остановки таймера" "[ $rc -ne 0 ] && grep -q 'не тот' $R/b.out && ! grep -q '^stop' $R/systemctl.log"

echo "=== 4. сбой после переключения: trap запускает таймер, повторный B доделывает"
reset; v43 > $T/stable_sync.py; echo 'print("h")' > $T/sync_health_api.py; upload 4.5.1-h14
run_a; B4=$(cat $R/root/sync-h14.next)
chmod 000 $R/etc   # mkdir drop-in упадёт под set -e — уже после переключения синка
run_b; rc=$?; chmod 755 $R/etc
ok "B упал и называет оба выхода: повтор B и откат" "[ $rc -ne 0 ] && grep -q 'повторите deploy-b.sh' $R/b.out && grep -q 'rollback.sh' $R/b.out"
ok "таймер снова запущен" "grep -qx active $TIMER && awk '/^stop hiddify-child-sync.timer/{s=NR} /^start hiddify-child-sync.timer/{t=NR} END{exit !(t>s && s>0)}' $R/systemctl.log"
ok "не помечен как завершённый" "[ ! -f $B4/deploy-b.done ] && [ -f $R/root/sync-h14.next ]"
run_a; rc=$?
ok "A после упавшего B: «не закончен», код 1, кандидат прежний" "[ $rc -eq 1 ] && grep -q 'начат и не закончен' $R/a.out && [ \"\$(cat $R/root/sync-h14.next)\" = \"$B4\" ]"
run_c; rc=$?
ok "C после упавшего B: отказ, код 1, загрузка цела" "[ $rc -eq 1 ] && grep -q 'начат и не закончен' $R/c.out && [ -f $U/stable_sync.py ]"
: > $R/systemctl.log
run_b; rc=$?
ok "повторный B: «доделываю», код 0" "[ $rc -eq 0 ] && grep -q 'доделываю' $R/b.out && ! grep -q 'уже в работе' $R/b.out"
ok "повторный B: health перезапущен, drop-in на месте, done" "grep -q '^restart hiddify-sync-api' $R/systemctl.log && [ -f $R/etc/hiddify-child-sync.service.d/timeout.conf ] && [ -f $B4/deploy-b.done ] && [ ! -f $R/root/sync-h14.next ]"

echo "=== 5. обрыв канала (читатель вывода закрылся) — этап доходит до конца"
reset; v43 > $T/stable_sync.py; echo 'print("h")' > $T/sync_health_api.py; upload 4.5.1-h14
run_a; B5=$(cat $R/root/sync-h14.next)
bash $R/deploy-b.sh 2>&1 | head -1 > /dev/null; $SLEEP 1
ok "этап B доработал (dry-run в журнале)" "grep -q 'повторный dry-run' $B5/deploy-b.log && [ -f $B5/deploy-b.done ]"
ok "таймер запущен" "grep -qx active $TIMER"

echo "=== 6. повтор A и B после выкатки; сборка, где изменился только health"
BL=$(cat $R/root/sync-h14.baseline); LAST=$(cat $R/root/sync-h14.last); MARK=$(cat $LAST/new.sha256)
run_a; rc=$?
ok "A: «уже в работе целиком», код 0" "[ $rc -eq 0 ] && grep -q 'уже в работе целиком' $R/a.out && [ ! -f $T/stable_sync.py.new ] && [ ! -f $R/root/sync-h14.next ]"
ok "A: .last, baseline и маркер прежние" "[ \"\$(cat $R/root/sync-h14.last)\" = \"$LAST\" ] && [ \"\$(cat $R/root/sync-h14.baseline)\" = \"$BL\" ] && [ \"\$(cat $LAST/new.sha256)\" = \"$MARK\" ]"
: > $R/systemctl.log; run_b; rc=$?
ok "повторный B: «уже в работе целиком», проверки, код 0, таймер не трогали" "[ $rc -eq 0 ] && grep -q 'уже в работе целиком' $R/b.out && grep -q 'проверки прошли' $R/b.out && ! grep -q '^stop' $R/systemctl.log"
: > $R/systemctl.log; run_rb; rc=$?
ok "откат на v4.3 без --baseline — отказ без остановки таймера" "[ $rc -ne 0 ] && grep -q 'только явно' $R/r.out && ! grep -q '^stop' $R/systemctl.log"
echo '# health fix' >> $U/sync_health_api.py
run_a; rc=$?
ok "только health изменился: A готовит кандидата" "[ $rc -eq 0 ] && grep -q 'код синка тот же' $R/a.out && [ -f $R/root/sync-h14.next ]"
run_b
ok "B ставит новый health" "grep -q '# health fix' $T/sync_health_api.py && [ ! -f $R/root/sync-h14.next ]"
HL=$(cat $R/root/sync-h14.last)
run_rb; rc=$?
ok "откат хотфикса health: прежний health, не «нечего»" "[ $rc -eq 0 ] && ! grep -q '# health fix' $T/sync_health_api.py && grep -q 'предыдущая точка отката' $R/r.out"
echo "$HL" > $R/root/sync-h14.last; cp $U/sync_health_api.py $T/sync_health_api.py   # вернуть хотфикс
run_c; rc=$?
ok "C: код 0 после успешной выкатки" "[ $rc -eq 0 ]"
ok "C: точки отката и цепочка живы" "[ -d \"\$(cat $R/root/sync-h14.last)\" ] && [ -d \"\$(cat $R/root/sync-h14.baseline)\" ] && [ -d \"\$(cat \$(cat $R/root/sync-h14.last)/prev-last)\" ]"
ok "C: каталог загрузки удалён, rollback.sh на месте" "[ ! -d $U ] && [ -x $T/rollback.sh ]"

echo "=== 7. откат: health не поднялся — таймер всё равно запущен; чужой ExecStart — отказ до stop"
reset; st_de03; rm -f $T/check_engines.py; upload 4.5.1-h14
run_a; run_b
echo '{ path=/x ; argv[]=/py /opt/other/stable_sync.py ; }' > $R/execstart; : > $R/systemctl.log
run_rb; rc=$?
ok "чужой ExecStart — отказ, таймер не останавливали" "[ $rc -ne 0 ] && grep -q 'запускает не' $R/r.out && ! grep -q '^stop' $R/systemctl.log"
rm -f $R/execstart; touch $R/fail.restart; : > $R/systemctl.log
run_rb; rc=$?
ok "health не поднялся — откат доделан, таймер запущен" "grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py && grep -q 'health API не поднялся' $R/r.out && grep -qx active $TIMER && grep -q '^start hiddify-child-sync.timer' $R/systemctl.log"

echo "=== 8. указатель со «/» в конце — C не удаляет точку отката"
reset; v43 > $T/stable_sync.py; echo 'print("h")' > $T/sync_health_api.py; upload 4.5.1-h14
run_a; run_b
B8=$(cat $R/root/sync-h14.last); echo "$B8/" > $R/root/sync-h14.baseline; echo "$B8/" > $R/root/sync-h14.last
cp -a $B8 $R/root/sync-h14-dup                                       # настоящий дубль
run_c
ok "точка отката цела, дубль удалён" "[ -d $B8 ] && [ ! -d $R/root/sync-h14-dup ]"
run_rb --baseline
ok "откат --baseline по указателю со «/» работает" "grep -q 'v4.3' $T/stable_sync.py"

echo "=== 9. A новой сборки без B не сдвигает точку отката"
reset; st_de03; rm -f $T/check_engines.py; upload 4.5.1-h14
run_a; run_b
L9=$(cat $R/root/sync-h14.last); upload 4.5.2-h14
run_a
ok "A 4.5.2: кандидат есть, .last прежний" "[ -f $R/root/sync-h14.next ] && [ \"\$(cat $R/root/sync-h14.last)\" = \"$L9\" ]"
run_rb
ok "откат по §6 → 4.5-h14 (то, что заменил последний B)" "grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py"
run_c; rc=$?
ok "C при невыкачанном кандидате: код 1, каталог кандидата цел" "[ $rc -eq 1 ] && grep -q 'не выкачен' $R/c.out && [ -d \"\$(cat $R/root/sync-h14.next)\" ]"

echo "=== 10. откат между A и B; правка живого файла после A — B отказывает до остановки таймера"
reset; st_de03; rm -f $T/check_engines.py; upload 4.5.1-h14
run_a; run_b
upload 4.5.2-h14; run_a                                               # кандидат 4.5.2 (бэкап 4.5.1)
run_rb                                                                # инцидент: откат на 4.5-h14
: > $R/systemctl.log
run_b; rc=$?
ok "B после отката: «в работе не то», таймер не останавливали" "[ $rc -ne 0 ] && grep -q 'в работе не то' $R/b.out && ! grep -q '^stop' $R/systemctl.log && grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py"
run_a; sed -i 's/k-live/k-rotated/' $T/stable_sync.py; : > $R/systemctl.log
run_b; rc=$?
ok "B после правки живого конфига: отказ до stop" "[ $rc -ne 0 ] && grep -q 'в работе не то' $R/b.out && ! grep -q '^stop' $R/systemctl.log && grep -q k-rotated $T/stable_sync.py"

echo "=== 11. загружена другая сборка после A — B отказывает; «уже в работе» не врёт"
reset; v43 > $T/stable_sync.py; echo 'print("h")' > $T/sync_health_api.py; upload 4.5.1-h14
run_a
echo '# правка синка' >> $U/stable_sync.py; : > $R/systemctl.log
run_b; rc=$?
ok "B: «загружена не та сборка», таймер не останавливали" "[ $rc -ne 0 ] && grep -q 'загружена не та сборка' $R/b.out && ! grep -q '^stop' $R/systemctl.log"
upload 4.5.1-h14; run_b                                               # штатно выкатили
echo '# правка синка' >> $U/stable_sync.py
run_b; rc=$?
ok "B без A после новой загрузки: не «уже в работе», а «сначала этап A»" "[ $rc -ne 0 ] && ! grep -q 'уже в работе' $R/b.out && grep -q 'сначала этап A' $R/b.out"

echo "=== 12. сборка с тем же синком (только health): повтор A до B, C после A, новая загрузка, удалённый .new"
reset; st_de03; upload 4.5.1-h14; run_a; run_b; run_c                 # de03 на 4.5.1 после A, B, C
hotfix 4.5.1-h14 H2; run_a; rc=$?; N1=$(cat $R/root/sync-h14.next)
ok "A хотфикса health: кандидат, код 0" "[ $rc -eq 0 ] && grep -q 'код синка тот же' $R/a.out"
run_a; rc=$?
ok "повтор A той же сборки до B: код 0, новый кандидат (не «начат и не закончен»)" "[ $rc -eq 0 ] && ! grep -q 'начат и не закончен' $R/a.out && [ \"\$(cat $R/root/sync-h14.next)\" != \"$N1\" ]"
run_c; rc=$?
ok "C после A без B: код 1, «не выкачен», загрузка цела" "[ $rc -eq 1 ] && grep -q 'не выкачен' $R/c.out && ! grep -q 'начат и не закончен' $R/c.out && [ -f $U/stable_sync.py ]"
upload 4.5.2-h14; run_a; rc=$?
ok "после A хотфикса загружена другая сборка: A проходит" "[ $rc -eq 0 ] && [ -f $T/stable_sync.py.new ]"
run_b; rc=$?
ok "… и B её выкатывает" "[ $rc -eq 0 ] && grep -q \"^VERSION = '4.5.2-h14'\" $T/stable_sync.py"
hotfix 4.5.2-h14 H3; run_a; rm -f $T/stable_sync.py.new; : > $R/systemctl.log
run_b; rc=$?
ok "удалён .new сборки с тем же синком: B просит повторить A, а не «доделываю»" "[ $rc -eq 1 ] && grep -q 'повторите этап A' $R/b.out && ! grep -q 'доделываю' $R/b.out && ! grep -q '^stop' $R/systemctl.log"
run_a; run_b; rc=$?
ok "повтор A и B: хотфикс в работе" "[ $rc -eq 0 ] && grep -q '# H3' $T/sync_health_api.py"

echo "=== 13. в работе хотфикс H2, этап A хотфикса H3, откат по §6 — выкатка не упирается в тупик"
reset; st_de03; upload 4.5.1-h14; run_a; run_b
hotfix 4.5.1-h14 H2; run_a; run_b                                      # в работе H2
hotfix 4.5.1-h14 H3; run_a                                             # кандидат H3 (бэкап H2)
run_rb; rc=$?
ok "откат H2 → прежний health" "[ $rc -eq 0 ] && ! grep -q '# H2' $T/sync_health_api.py"
: > $R/systemctl.log; run_b; rc=$?
ok "B кандидата H3 после отката: «в работе не то», таймер не трогали" "[ $rc -eq 1 ] && grep -q 'в работе не то' $R/b.out && ! grep -q '^stop' $R/systemctl.log"
run_a; rc=$?
ok "A после отката проходит (не «начат и не закончен»)" "[ $rc -eq 0 ] && ! grep -q 'начат и не закончен' $R/a.out"
run_b; rc=$?
ok "B выкатывает H3" "[ $rc -eq 0 ] && grep -q '# H3' $T/sync_health_api.py"

echo "=== 14. B хотфикса упал после переключения, откат — откатанное не ставится снова"
reset; st_de03; upload 4.5.1-h14; run_a; run_b
hotfix 4.5.1-h14 H2; run_a; N2=$(cat $R/root/sync-h14.next)
chmod 000 $R/etc; run_b; rc=$?; chmod 755 $R/etc
ok "B упал после переключения: deploy-b.switched есть, done нет" "[ $rc -ne 0 ] && [ -f $N2/deploy-b.switched ] && [ ! -f $N2/deploy-b.done ] && grep -q '# H2' $T/sync_health_api.py"
run_rb; rc=$?
ok "откат: прежний health, незаконченный кандидат снят" "[ $rc -eq 0 ] && ! grep -q '# H2' $T/sync_health_api.py && grep -q 'снят' $R/r.out && [ ! -f $R/root/sync-h14.next ]"
run_b; rc=$?
ok "B после отката: «сначала этап A», откатанное не ставит" "[ $rc -eq 1 ] && grep -q 'сначала этап A' $R/b.out && ! grep -q 'доделываю' $R/b.out && ! grep -q '# H2' $T/sync_health_api.py"
run_a; rc=$?; run_b; rc2=$?
ok "A и B выкатывают H2 заново" "[ $rc -eq 0 ] && [ $rc2 -eq 0 ] && grep -q '# H2' $T/sync_health_api.py"

echo "=== 15. B упал после переключения, загрузка изменилась — B доделывает сборку этапа A, без круга"
reset; v43 > $T/stable_sync.py; echo 'print("h")' > $T/sync_health_api.py; upload 4.5.1-h14
run_a; C1=$(cat $R/root/sync-h14.next)
chmod 000 $R/etc; run_b; chmod 755 $R/etc
echo '# правка синка' >> $U/stable_sync.py
run_a; rc=$?
ok "A: «начат и не закончен», называет и B, и откат (с --baseline: заменена v4.3)" "[ $rc -eq 1 ] && grep -q 'начат и не закончен' $R/a.out && grep -q 'rollback.sh --baseline' $R/a.out"
run_b; rc=$?
ok "B доделывает сборку этапа A, о другой загрузке — в итоге, код 2" "[ $rc -eq 2 ] && grep -q 'доделываю' $R/b.out && grep -q 'не та сборка' $R/b.out && [ -f $C1/deploy-b.done ] && [ ! -f $R/root/sync-h14.next ] && ! grep -q '# правка синка' $T/stable_sync.py"
run_b; rc=$?
ok "повторный B: не «уже в работе», а «сначала этап A»" "[ $rc -eq 1 ] && grep -q 'сначала этап A' $R/b.out"
run_a; run_b; rc=$?
ok "A и B выкатывают изменённую загрузку" "[ $rc -eq 0 ] && grep -q '# правка синка' $T/stable_sync.py"

echo "=== 16. откат после выкатки одного rollback.sh и по цепочке — подсказка на точку, отличную от работающей"
reset; st_de03; upload 4.5.1-h14; run_a; run_b; B1=$(cat $R/root/sync-h14.last)
upload 4.5.1-h14; echo '# rb fix' >> $U/rollback.sh; run_a; run_b; rc=$?
ok "выкатка одного rollback.sh прошла" "[ $rc -eq 0 ] && grep -q '# rb fix' $T/rollback.sh"
run_rb; rc=$?
ok "откат: «нечего» и следующая точка — бэкап 4.5-h14" "[ $rc -eq 1 ] && grep -q 'откатывать нечего' $R/r.out && grep -q \"предыдущая точка отката: $B1 \" $R/r.out"
echo $B1 > $R/root/sync-h14.last; run_rb; rc=$?
ok "по подсказке — откат на 4.5-h14" "[ $rc -eq 0 ] && grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py"
reset; st_de03; upload 4.5.1-h14; run_a; run_b; B1=$(cat $R/root/sync-h14.last)
hotfix 4.5.1-h14 H2; run_a; run_b; run_rb                              # H2, откат на H1
hotfix 4.5.1-h14 H3; run_a; run_b; run_rb; rc=$?                       # H3, откат на H1
ok "цепочка: звено, равное работающему, пропущено — подсказка на бэкап 4.5-h14" "[ $rc -eq 0 ] && ! grep -q '# H3' $T/sync_health_api.py && grep -q \"предыдущая точка отката: $B1 \" $R/r.out"

echo "=== 17. первая выкатка с v4.3, правка ключа между A и B — .baseline на то, что заменил B"
reset; v43 > $T/stable_sync.py; echo 'print("h43")' > $T/sync_health_api.py; chmod 700 $T/stable_sync.py; upload 4.5.1-h14
run_a
ok "A .baseline не пишет" "[ ! -f $R/root/sync-h14.baseline ]"
sed -i 's/k-live/k-rotated/' $T/stable_sync.py
run_b; rc=$?
ok "B после правки ключа: «в работе не то»" "[ $rc -eq 1 ] && grep -q 'в работе не то' $R/b.out"
run_a; Z=$(cat $R/root/sync-h14.next); run_b; rc=$?
ok "после B .baseline и .last — бэкап второго A (с новым ключом)" "[ $rc -eq 0 ] && [ \"\$(cat $R/root/sync-h14.baseline)\" = \"$Z\" ] && [ \"\$(cat $R/root/sync-h14.last)\" = \"$Z\" ] && grep -q k-rotated $Z/stable_sync.py.prev"
run_rb; rc=$?
ok "откат без --baseline: отказ, сказано, куда указывает .baseline" "[ $rc -eq 1 ] && grep -q 'только явно' $R/r.out && grep -q \"baseline → $Z\" $R/r.out"
run_rb --baseline; rc=$?
ok "откат --baseline: v4.3 с новым ключом" "[ $rc -eq 0 ] && grep -q k-rotated $T/stable_sync.py && ! grep -q '^VERSION' $T/stable_sync.py"

echo "=== 18. откат хотфикса health после C (загрузки нет) — B не отвечает «уже в работе»"
reset; st_de03; upload 4.5.1-h14; run_a; run_b
hotfix 4.5.1-h14 H2; run_a; run_b; run_c; run_rb
run_b; rc=$?
ok "B без загрузки после отката: «сначала этап A»" "[ ! -d $U ] && [ $rc -eq 1 ] && ! grep -q 'уже в работе' $R/b.out && grep -q 'сначала этап A' $R/b.out"

echo "=== 19. B прерван до переключения (прогон синка висит) — C отказывает и не удаляет загрузку"
reset; st_de03; upload 4.5.1-h14; run_a; N=$(cat $R/root/sync-h14.next)
touch $R/stuck; run_b; rc=$?; rm -f $R/stuck
ok "B прерван: код 1, файлы не менялись, deploy-b.switched нет, таймер запущен" "[ $rc -eq 1 ] && grep -q 'прерван, файлы в работе не менялись' $R/b.out && grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py && [ ! -f $N/deploy-b.switched ] && grep -qx active $TIMER"
run_c; rc=$?
ok "C: код 1, «не выкачен», загрузка цела" "[ $rc -eq 1 ] && grep -q 'не выкачен' $R/c.out && [ -f $U/stable_sync.py ]"
run_b; rc=$?; run_c; rc2=$?
ok "затем B и C — штатно" "[ $rc -eq 0 ] && [ $rc2 -eq 0 ] && [ ! -d $U ]"

echo "=== 20. код C: health, Result, движки, SYNC_ANON; при проблемах загрузка и SYNC_ANON остаются, C повторяется той же командой"
reset; st_de03; upload 4.5.1-h14; run_a; run_b; cp $R/deploy-c.sh $U/deploy-c.sh
touch $R/health.down; echo exit-code > $R/last.result
bash $U/deploy-c.sh 0 > $R/c.out 2>&1; rc=$?
ok "health не отвечает, Result=exit-code: код 2, загрузка и SYNC_ANON остались" "[ $rc -eq 2 ] && grep -q 'health не healthy' $R/c.out && grep -q 'Result=exit-code' $R/c.out && [ -f $U/deploy-c.sh ] && [ -f $R/run/hiddify-child-sync.service.d/anon.conf ]"
rm -f $R/health.down; echo success > $R/last.result; echo '{"status": "unhealthy", "reasons": ["stuck"], "database": {"user_count": 3}}' > $R/health.json
bash $U/deploy-c.sh 0 > $R/c.out 2>&1; rc=$?
ok "повтор C той же командой: health unhealthy — код 2" "[ $rc -eq 2 ] && grep -q unhealthy $R/c.out"
rm -f $R/health.json; echo exit-code > $R/last.result
bash $U/deploy-c.sh 0 > $R/c.out 2>&1; rc=$?; echo success > $R/last.result
ok "только Result=exit-code — код 2" "[ $rc -eq 2 ] && grep -q 'последний прогон: Result=exit-code' $R/c.out"
echo 1 > $R/ce.rc
bash $U/deploy-c.sh 0 > $R/c.out 2>&1; rc=$?; rm -f $R/ce.rc
ok "движки расходятся — код 2" "[ $rc -eq 2 ] && grep -q 'движки: код check_engines 1' $R/c.out"
chmod 555 $R/run/hiddify-child-sync.service.d
bash $U/deploy-c.sh 0 > $R/c.out 2>&1; rc=$?; chmod 755 $R/run/hiddify-child-sync.service.d
ok "SYNC_ANON не снялся — код 2" "[ $rc -eq 2 ] && grep -q 'SYNC_ANON не снят' $R/c.out"
bash $U/deploy-c.sh 0 > $R/c.out 2>&1; rc=$?
ok "всё в порядке — код 0, SYNC_ANON снят, загрузка удалена" "[ $rc -eq 0 ] && [ ! -d $U ] && [ ! -f $R/run/hiddify-child-sync.service.d/anon.conf ]"
bash $R/deploy-c.sh > $R/c.out 2>&1; rc=$?
ok "C без аргумента — код 1" "[ $rc -eq 1 ] && grep -q usage $R/c.out"

echo "=== 21. битый check_engines.py в загрузке — A отказывает; изменённый после A — B отказывает"
reset; st_de03; upload 4.5.1-h14; echo 'def (' >> $U/check_engines.py
run_a; rc=$?
ok "A: битый check_engines.py — отказ, кандидата нет" "[ $rc -ne 0 ] && [ ! -f $R/root/sync-h14.next ]"
upload 4.5.1-h14; run_a; echo 'def (' >> $U/check_engines.py; : > $R/systemctl.log
run_b; rc=$?
ok "B: check_engines.py изменён после A — «загружена не та сборка», до остановки таймера" "[ $rc -eq 1 ] && grep -q 'загружена не та сборка' $R/b.out && ! grep -q '^stop' $R/systemctl.log"

echo "=== 22. код B: проверки после выкатки не прошли — код 2, этап помечен завершённым"
reset; st_de03; upload 4.5.1-h14; run_a; N=$(cat $R/root/sync-h14.next)
echo '{"status": "unhealthy", "reasons": ["x"], "database": {"user_count": 3}}' > $R/health.json; echo 1 > $R/ce.rc
run_b; rc=$?; rm -f $R/health.json $R/ce.rc
ok "health unhealthy и расхождения движков: код 2, deploy-b.done, список проблем" "[ $rc -eq 2 ] && [ -f $N/deploy-b.done ] && [ ! -f $R/root/sync-h14.next ] && grep -q 'проверки нашли проблемы' $R/b.out && grep -q 'health не healthy' $R/b.out && grep -q 'движки: код' $R/b.out"
upload 4.5.2-h14; run_a; echo exit-code > $R/result; run_b; rc=$?; rm -f $R/result
ok "прогон с ошибкой (Result=exit-code): код 2" "[ $rc -eq 2 ] && grep -q 'прогон 1: Result=exit-code' $R/b.out"
hotfix 4.5.2-h14 H2; run_a; touch $R/journal.changes; run_b; rc=$?; rm -f $R/journal.changes
ok "второй прогон с изменениями (не сошлось): код 2" "[ $rc -eq 2 ] && grep -q 'прогон 2: изменений 1' $R/b.out"

echo "=== 23. C после отката на 4.5-h14: её check_engines вызывается без --retry"
reset; st_de03; upload 4.5.1-h14; run_a; run_b; run_rb; rm -f $R/ce.args
run_c
ok "C вызвал check_engines 4.5-h14 без --retry" "grep -q check_engines $R/ce.args && ! grep -q -- '--retry' $R/ce.args"

echo "=== 24. A сборки, которая уже в работе, снимает невыкачанного кандидата"
reset; st_de03; upload 4.5.1-h14; run_a; run_b                        # в работе 4.5.1
upload 4.5.2-h14; run_a                                                # кандидат 4.5.2 — передумали
upload 4.5.1-h14; run_a; rc=$?
ok "A сборки в работе: код 0, «уже в работе целиком», кандидат и остатки .new сняты" "[ $rc -eq 0 ] && grep -q 'уже в работе целиком' $R/a.out && [ ! -f $R/root/sync-h14.next ] && nonew"
run_b; rc=$?
ok "B: «уже в работе целиком», код 0" "[ $rc -eq 0 ] && grep -q 'уже в работе целиком' $R/b.out"
run_c; rc=$?
ok "C: код 0" "[ $rc -eq 0 ]"

echo "=== 25. B хотфикса упал на mv health (файлы в работе ещё прежние) — откат снимает кандидата, не трогая файлы"
reset; st_de03; upload 4.5.1-h14; run_a; run_b; B1=$(cat $R/root/sync-h14.last)
hotfix 4.5.1-h14 H2; run_a; N=$(cat $R/root/sync-h14.next)
touch $R/fail.mv.health; run_b; rc=$?; rm -f $R/fail.mv.health
ok "B упал: deploy-b.switched есть, health прежний" "[ $rc -ne 0 ] && [ -f $N/deploy-b.switched ] && ! grep -q '# H2' $T/sync_health_api.py"
: > $R/systemctl.log; run_rb; rc=$?
ok "откат: «файлы не трогаю», кандидат и остатки .new сняты, подсказка на 4.5-h14, таймер не трогали" "[ $rc -eq 0 ] && grep -q 'файлы не трогаю' $R/r.out && [ ! -f $R/root/sync-h14.next ] && nonew && grep -q \"предыдущая точка отката: $B1 \" $R/r.out && ! grep -q '^stop' $R/systemctl.log"
run_a; rc=$?; run_b; rc2=$?
ok "затем A и B выкатывают H2" "[ $rc -eq 0 ] && [ $rc2 -eq 0 ] && grep -q '# H2' $T/sync_health_api.py"

echo "=== 26. B упал после переключения, каталог загрузки очищен (перезагрузка) — B доделывает без сборки"
reset; st_de03; upload 4.5.1-h14; run_a; N=$(cat $R/root/sync-h14.next)
chmod 000 $R/etc; run_b; chmod 755 $R/etc
touch $R/stuck; run_b; rc=$?; rm -f $R/stuck
ok "доделка B при висящем прогоне: «прерван», файлы частично новые, код 1" "[ $rc -eq 1 ] && grep -q 'частично новые' $R/b.out && ! grep -q 'файлы в работе не менялись' $R/b.out"
rm -rf "${U:?}"; run_b; rc=$?
ok "B без загрузки: «доделываю», код 0, drop-in на месте, done" "[ $rc -eq 0 ] && grep -q 'доделываю' $R/b.out && [ -f $N/deploy-b.done ] && [ -f $R/etc/hiddify-child-sync.service.d/timeout.conf ] && grep -q \"^VERSION = '4.5.1-h14'\" $T/stable_sync.py"

echo "=== 27. коды B по прогонам: health при запущенном таймере, Result прогона 2, изменения только в прогоне 1, прогон 2 висит, прогона нет"
reset; st_de03; upload 4.5.1-h14; run_a; run_b; rc=$?
ok "штатный B: health проверен при запущенном таймере — healthy, код 0" "[ $rc -eq 0 ] && grep -A1 'health (таймер уже запущен)' $R/b.out | grep -q '^healthy'"
hotfix 4.5.1-h14 H2; run_a; rm -f $R/run.n; echo exit-code > $R/result.2; run_b; rc=$?; rm -f $R/result.2
ok "Result прогона 2 = exit-code — код 2" "[ $rc -eq 2 ] && grep -q 'прогон 2: Result=exit-code' $R/b.out && ! grep -q 'прогон 1: Result' $R/b.out"
hotfix 4.5.1-h14 H3; run_a; rm -f $R/run.n; touch $R/journal.changes.1; run_b; rc=$?; rm -f $R/journal.changes.1
ok "изменения только в прогоне 1 — код 0" "[ $rc -eq 0 ]"
hotfix 4.5.1-h14 H4; run_a; touch $R/stuck.after.timer; run_b; rc=$?; rm -f $R/stuck.after.timer $R/stuck
ok "прогон 2 не закончился за 10 минут — код 2" "[ $rc -eq 2 ] && grep -q 'прогон 2 не закончился' $R/b.out"
hotfix 4.5.1-h14 H5; run_a; touch $R/norun; run_b; rc=$?; rm -f $R/norun
ok "прогона не было (условие юнита) — код 2" "[ $rc -eq 2 ] && grep -q 'прогон 1: прогона не было' $R/b.out"

echo "=== 28. C: прогонов с epoch нет — код 2; без SYNC_ANON строки с именами не печатаются"
reset; st_de03; upload 4.5.1-h14; run_a; run_b
bash $R/deploy-c.sh $(( $(cat $R/clock) + 1000 )) > $R/c.out 2>&1; rc=$?
ok "C: прогонов нет — код 2, SYNC_ANON и загрузка остались" "[ $rc -eq 2 ] && grep -q 'не было ни одного прогона' $R/c.out && [ -f $R/run/hiddify-child-sync.service.d/anon.conf ] && [ -f $U/stable_sync.py ]"
rm -f $R/run/hiddify-child-sync.service.d/anon.conf                    # «перезагрузка» сняла SYNC_ANON
touch $R/journal.error; systemctl start hiddify-child-sync.service; rm -f $R/journal.error
run_c
ok "C без SYNC_ANON: имён в выводе нет, вместо строк ❌ — счётчик" "! grep -q 'Ivan Petrov' $R/c.out && grep -q 'строк ❌: 1' $R/c.out"
reset; v43 > $T/stable_sync.py; echo 'print("h")' > $T/sync_health_api.py; upload 4.5.1-h14
run_a; chmod 000 $R/etc; touch $R/journal.error; run_b; chmod 755 $R/etc   # сбой до записи anon.conf: прогон trap-а — с именем
run_b; rm -f $R/journal.error; run_c
ok "C после доделки B (SYNC_ANON уже действует): имя из прогона до доделки не печатается" "[ -f $R/root/sync-h14.last ] && grep -q 'Ivan Petrov' $R/journal && ! grep -q 'Ivan Petrov' $R/c.out"

echo "=== 29. общая блокировка: пока идёт этап или откат, A, B, C и откат отказывают, ничего не меняя"
reset; st_de03; upload 4.5.1-h14; run_a
exec 8>>$R/root/sync-h14.lock; flock -n 8
: > $R/systemctl.log
run_a; ra=$?; run_b; rb=$?; run_c; rcc=$?; run_rb; rr=$?
ok "все четыре отказывают с кодом 1 и просят дождаться" "[ $ra -eq 1 ] && [ $rb -eq 1 ] && [ $rcc -eq 1 ] && [ $rr -eq 1 ] && grep -q 'дождитесь' $R/a.out && grep -q 'дождитесь' $R/b.out && grep -q 'дождитесь' $R/c.out && grep -q 'дождитесь' $R/r.out"
ok "… и ничего не тронули" "! grep -q '^stop' $R/systemctl.log && [ -f $R/root/sync-h14.next ] && grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py"
exec 8>&-
run_b; rc=$?
ok "блокировка снята — B проходит" "[ $rc -eq 0 ]"
run_c; rc=$?
ok "C сразу после B: блокировку не держит хвост вывода этапа B" "[ $rc -eq 0 ] && ! grep -q 'дождитесь' $R/c.out"

echo "=== 30. пока B ждал прогон, в живом файле поправили ключ — B отказывает, правка цела"
reset; st_de03; upload 4.5.1-h14; run_a
echo "sed -i 's/k-live/k-rotated/' $T/stable_sync.py" > $R/during.wait
run_b; rc=$?
ok "B: «пока ждали прогон … повторите этап A», код 1, синк прежний, правка цела, таймер запущен" "[ $rc -eq 1 ] && grep -q 'пока ждали прогон' $R/b.out && grep -q k-rotated $T/stable_sync.py && grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py && grep -qx active $TIMER"
run_a; run_b; rc=$?
ok "A и B после этого: в работе 4.5.1 с новым ключом" "[ $rc -eq 0 ] && grep -q k-rotated $T/stable_sync.py && grep -q \"^VERSION = '4.5.1-h14'\" $T/stable_sync.py"

echo "=== 31. обрыв ssh: B получает HUP во время прогонов, откат пишет в закрытую трубу — оба доходят до конца"
reset; st_de03; upload 4.5.1-h14; run_a; N=$(cat $R/root/sync-h14.next)
touch $R/hup.on.run; run_b; rm -f $R/hup.on.run
ok "B после HUP доработал: done, в работе 4.5.1" "[ -f $N/deploy-b.done ] && grep -q \"^VERSION = '4.5.1-h14'\" $T/stable_sync.py"
python3 - $R/rollback.sh > $R/r.out 2>&1 <<'PY'
import os, subprocess, sys
r, w = os.pipe(); os.close(r)
print(subprocess.run(['bash', sys.argv[1]], stdout=w, stderr=w).returncode)
PY
ok "откат с закрытой трубой: выполнен, код 0, таймер запущен" "grep -qx 0 $R/r.out && grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py && grep -qx active $TIMER"

echo "=== 32. доделка B: перенос оставшихся .new, сбой до переключения синка, правка ключа, чужой синк"
reset; st_de03; upload 4.5.1-h14; run_a; run_b
hotfix 4.5.1-h14 H2; run_a; N=$(cat $R/root/sync-h14.next)
touch $R/fail.mv.health; run_b; rm -f $R/fail.mv.health
run_b; rc=$?
ok "после сбоя на mv health: «доделываю», H2 в работе, остатков .new нет, код 0" "[ $rc -eq 0 ] && grep -q 'доделываю' $R/b.out && grep -q '# H2' $T/sync_health_api.py && nonew && [ -f $N/deploy-b.done ]"
upload 4.5.2-h14; run_a; N=$(cat $R/root/sync-h14.next)
touch $R/fail.mv.sync; run_b; rc=$?; rm -f $R/fail.mv.sync
ok "сбой на mv синка: switched есть, синк прежний" "[ $rc -ne 0 ] && [ -f $N/deploy-b.switched ] && grep -q \"^VERSION = '4.5.1-h14'\" $T/stable_sync.py"
run_b; rc=$?
ok "повторный B: этап заново, код 0, в работе 4.5.2" "[ $rc -eq 0 ] && grep -q 'начинается заново' $R/b.out && grep -q \"^VERSION = '4.5.2-h14'\" $T/stable_sync.py"
upload 4.5.3-h14; run_a; touch $R/fail.mv.sync; run_b; rm -f $R/fail.mv.sync
sed -i 's/k-live/k-rotated/' $T/stable_sync.py
run_b; rc=$?
ok "сбой до переключения синка, потом правка ключа: B «повторите этап A», маркер снят" "[ $rc -eq 1 ] && grep -q 'повторите этап A' $R/b.out && [ ! -f \"\$(cat $R/root/sync-h14.next)/deploy-b.switched\" ]"
run_a; rc=$?; run_b; rc2=$?
ok "A не блокирован, A и B ставят 4.5.3 с новым ключом" "[ $rc -eq 0 ] && [ $rc2 -eq 0 ] && grep -q k-rotated $T/stable_sync.py && grep -q \"^VERSION = '4.5.3-h14'\" $T/stable_sync.py"
upload 4.5.4-h14; run_a; chmod 000 $R/etc; run_b; chmod 755 $R/etc
sed -i 's/k-rotated/k-third/' $T/stable_sync.py
run_b; rc=$?
ok "сбой после переключения, потом правка ключа: B доделывает с правкой, предупреждает, код 0" "[ $rc -eq 0 ] && grep -q 'изменённым конфигом' $R/b.out && grep -q k-third $T/stable_sync.py && grep -q \"^VERSION = '4.5.4-h14'\" $T/stable_sync.py"
upload 4.5.5-h14; run_a; chmod 000 $R/etc; run_b; chmod 755 $R/etc
v43 > $T/stable_sync.py; : > $R/systemctl.log
run_b; rc=$?
ok "после переключения в работу руками вернули v4.3: B отказывает до остановки таймера, называет откат" "[ $rc -eq 1 ] && grep -q 'не v4.5.x-h14' $R/b.out && grep -q 'rollback.sh' $R/b.out && ! grep -q '^stop' $R/systemctl.log"

echo "=== 33. первая выкатка с v4.3, B упал после переключения — откат только --baseline, и сообщения это говорят"
reset; v43 > $T/stable_sync.py; echo 'print("h43")' > $T/sync_health_api.py; chmod 700 $T/stable_sync.py; upload 4.5.1-h14
run_a; chmod 000 $R/etc; run_b; chmod 755 $R/etc
ok "B называет откат с --baseline" "grep -q 'rollback.sh --baseline' $R/b.out"
run_c; rc=$?
ok "C: отказ, называет откат с --baseline" "[ $rc -eq 1 ] && grep -q 'rollback.sh --baseline' $R/c.out"
run_rb --baseline; rc=$?
ok "откат --baseline: v4.3, кандидат и SYNC_ANON сняты" "[ $rc -eq 0 ] && grep -q 'v4.3' $T/stable_sync.py && [ ! -f $R/root/sync-h14.next ] && [ ! -f $R/run/hiddify-child-sync.service.d/anon.conf ]"

echo "=== 34. A и B согласованы: после правки ключа в работе повтор той же сборки не нужен обоим"
reset; st_de03; upload 4.5.1-h14; run_a; run_b
sed -i 's/k-live/k-rotated/' $T/stable_sync.py
upload 4.5.1-h14; run_a; rc=$?
ok "A: «уже в работе целиком», код 0" "[ $rc -eq 0 ] && grep -q 'уже в работе целиком' $R/a.out"
run_b; rc=$?
ok "B: тоже «уже в работе целиком», код 0" "[ $rc -eq 0 ] && grep -q 'уже в работе целиком' $R/b.out"
hotfix 4.5.1-h14 H2; run_b; rc=$?
ok "загружен хотфикс health при том же синке, B без A: «сначала этап A»" "[ $rc -eq 1 ] && grep -q 'сначала этап A' $R/b.out && ! grep -q '# H2' $T/sync_health_api.py"

echo "=== 35. повтор B после кода 2 повторяет проверки, а не отвечает 0"
reset; st_de03; upload 4.5.1-h14; run_a
echo '{"status": "unhealthy", "reasons": ["x"], "database": {"user_count": 3}}' > $R/health.json
run_b; rc1=$?; run_b; rc2=$?; rm -f $R/health.json; run_b; rc3=$?
ok "B: 2, повтор — снова 2, после исправления — 0" "[ $rc1 -eq 2 ] && [ $rc2 -eq 2 ] && [ $rc3 -eq 0 ] && grep -q 'уже в работе целиком' $R/b.out"

echo "=== 36. C не удаляет загрузку, если её сборки нет в работе (но это не проблема выкатки)"
reset; st_de03; upload 4.5.1-h14; run_a; run_b; run_rb
run_c; rc=$?
ok "после отката C: код 0, загрузка не той сборки оставлена с пояснением" "[ $rc -eq 0 ] && grep -q 'загрузку не удаляю' $R/c.out && [ -f $U/stable_sync.py ]"

echo "=== 36a. C, когда в загрузке остались только скрипты (сборки нет), — код 0"
reset; st_de03; upload 4.5.1-h14; run_a; run_b; rm -f $U/stable_sync.py
run_c; rc=$?
ok "C: код 0, загрузка удалена" "[ $rc -eq 0 ] && [ ! -d $U ]"

echo "=== 37. B прерван до переключения — rollback.sh в работе не меняется; A сборки в работе снимает кандидата"
reset; st_de03; upload 4.5.1-h14; run_a; run_b; run_c                 # 4.5.1 в работе, загрузки нет
upload 4.5.1-h14; echo '# rb fix' >> $U/rollback.sh; run_a
touch $R/stuck; run_b; rc=$?; rm -f $R/stuck
ok "B прерван: rollback.sh в работе прежний" "[ $rc -eq 1 ] && ! grep -q '# rb fix' $T/rollback.sh"
upload 4.5.1-h14; run_a; rc=$?
ok "A сборки в работе: код 0, кандидат и остатки .new сняты" "[ $rc -eq 0 ] && grep -q 'уже в работе целиком' $R/a.out && [ ! -f $R/root/sync-h14.next ] && nonew"
run_c; rc=$?
ok "C: код 0" "[ $rc -eq 0 ]"

echo "=== 38. предпроверки B: чужой ExecStart, изменённый после A health, битые файлы у кандидата старого этапа A"
reset; st_de03; upload 4.5.1-h14; run_a; N=$(cat $R/root/sync-h14.next)
echo '{ path=/x ; argv[]=/py /opt/other/stable_sync.py ; }' > $R/execstart; : > $R/systemctl.log
run_b; rc=$?; rm -f $R/execstart
ok "чужой ExecStart: отказ до остановки таймера" "[ $rc -eq 1 ] && grep -q 'запускает не' $R/b.out && ! grep -q '^stop' $R/systemctl.log"
echo '# H3' >> $U/sync_health_api.py; : > $R/systemctl.log
run_b; rc=$?
ok "health изменён после A: «загружена не та сборка», до остановки таймера" "[ $rc -eq 1 ] && grep -q 'загружена не та сборка' $R/b.out && ! grep -q '^stop' $R/systemctl.log"
rm -f $N/upload-aux.sha256                                             # кандидат старого этапа A: sha остальных файлов нет
upload 4.5.1-h14; echo 'def (' >> $U/sync_health_api.py; run_b; rc=$?
ok "битый health: «не компилируется»" "[ $rc -eq 1 ] && grep -q 'sync_health_api.py не компилируется' $R/b.out"
upload 4.5.1-h14; echo 'def (' >> $U/check_engines.py; run_b; rc=$?
ok "битый check_engines.py: «не компилируется»" "[ $rc -eq 1 ] && grep -q 'check_engines.py не компилируется' $R/b.out"
upload 4.5.1-h14; echo 'if (' >> $U/rollback.sh; run_b; rc=$?
ok "битый rollback.sh: «ошибкой синтаксиса»" "[ $rc -eq 1 ] && grep -q 'ошибкой синтаксиса' $R/b.out"

echo "=== 39. сборка, где изменился только check_engines: откат и B после отката"
reset; st_de03; upload 4.5.1-h14; run_a; run_b
upload 4.5.1-h14; echo '# CE2' >> $U/check_engines.py; run_a; run_b
ok "хотфикс check_engines в работе" "grep -q '# CE2' $T/check_engines.py"
upload 4.5.2-h14; echo '# CE2' >> $U/check_engines.py; run_a               # кандидат 4.5.2 (бэкап с CE2)
run_rb; rc=$?
ok "откат хотфикса check_engines: прежний check_engines" "[ $rc -eq 0 ] && ! grep -q '# CE2' $T/check_engines.py"
: > $R/systemctl.log; run_b; rc=$?
ok "B 4.5.2 после отката: «в работе не то», таймер не трогали" "[ $rc -eq 1 ] && grep -q 'в работе не то' $R/b.out && ! grep -q '^stop' $R/systemctl.log"

echo "=== 40. старая раскладка без .baseline: откат на версию до v4.5 подсказывает, как задать .baseline"
reset; st_de03
run_rb; rc=$?
ok "откат без .baseline: отказ с командой echo … > .baseline" "[ $rc -eq 1 ] && grep -q \"echo $OLD > $R/root/sync-h14.baseline\" $R/r.out"
echo $OLD > $R/root/sync-h14.baseline; run_rb --baseline; rc=$?
ok "по подсказке: откат --baseline проходит" "[ $rc -eq 0 ] && grep -q 'v4.3' $T/stable_sync.py"

echo "=== 41. B и C передают check_engines путь синка и --retry"
reset; st_de03; upload 4.5.1-h14; run_a; rm -f $R/ce.args; run_b
ok "B: SYNC_SCRIPT и --retry 6" "grep -q \"SYNC_SCRIPT=$T/stable_sync.py .*--retry 6\" $R/ce.args"
rm -f $R/ce.args; run_c
ok "C: SYNC_SCRIPT и --retry 3 для нового check_engines" "grep -q \"SYNC_SCRIPT=$T/stable_sync.py .*--retry 3\" $R/ce.args"

echo "=== 42. сборка только с rollback.sh, B упал на daemon-reload — откат снимает кандидата и SYNC_ANON, A и B согласованы"
reset; st_de03; upload 4.5.1-h14; run_a; run_b; run_c
upload 4.5.1-h14; echo '# rb fix' >> $U/rollback.sh; run_a
touch $R/fail.daemon-reload; run_b; rc=$?; rm -f $R/fail.daemon-reload
ok "B упал: SYNC_ANON записан, кандидат не закончен" "[ $rc -ne 0 ] && [ -f $R/run/hiddify-child-sync.service.d/anon.conf ] && [ -f $R/root/sync-h14.next ]"
run_rb; rc=$?
ok "откат: «файлы не трогаю», кандидат и SYNC_ANON сняты, подсказка" "[ $rc -eq 0 ] && grep -q 'файлы не трогаю' $R/r.out && [ ! -f $R/root/sync-h14.next ] && [ ! -f $R/run/hiddify-child-sync.service.d/anon.conf ] && grep -q 'предыдущая точка отката' $R/r.out"
run_a; ra=$?; run_b; rb=$?
ok "A и B согласованы: оба «уже в работе целиком», код 0" "[ $ra -eq 0 ] && [ $rb -eq 0 ] && grep -q 'уже в работе целиком' $R/a.out && grep -q 'уже в работе целиком' $R/b.out"

echo "=== 43. после сбоя B в работу вернули прежний синк руками — доделка отказывает, а не объявляет смесь успехом"
reset; st_de03; upload 4.5.1-h14; run_a; N=$(cat $R/root/sync-h14.next)
chmod 000 $R/etc; run_b; chmod 755 $R/etc
cp $N/stable_sync.py.prev $T/stable_sync.py; : > $R/systemctl.log
run_b; rc=$?
ok "B: «не та сборка, что ставил этот этап B», откат назван, таймер не трогали, done нет" "[ $rc -eq 1 ] && grep -q 'не та сборка, что ставил' $R/b.out && grep -q 'rollback.sh' $R/b.out && ! grep -q '^stop' $R/systemctl.log && [ ! -f $N/deploy-b.done ]"
run_rb; rc=$?
ok "откат доводит дело: 4.5-h14 целиком, кандидат снят" "[ $rc -eq 0 ] && grep -q 'health 4.5' $T/sync_health_api.py && grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py && [ ! -f $R/root/sync-h14.next ]"

echo "=== 44. B упал ещё до маркера (каталог состояния) — точка отката .last не сдвинута"
reset; st_de03; upload 4.5.1-h14; run_a; run_b; L=$(cat $R/root/sync-h14.last)
upload 4.5.2-h14; run_a; N=$(cat $R/root/sync-h14.next)
rm -rf "${R:?}"/varlib; touch $R/varlib; run_b; rc=$?; rm -f $R/varlib; mkdir -p $R/varlib; mkstatus
ok "B упал до маркера: .last прежний, switched нет, синк прежний" "[ $rc -ne 0 ] && [ \"\$(cat $R/root/sync-h14.last)\" = \"$L\" ] && [ ! -f $N/deploy-b.switched ] && grep -q \"^VERSION = '4.5.1-h14'\" $T/stable_sync.py"
run_b; rc=$?
ok "повторный B проходит" "[ $rc -eq 0 ] && grep -q \"^VERSION = '4.5.2-h14'\" $T/stable_sync.py"

echo "=== 45. сообщения доделки B: отказ по предпроверке говорит «не доделан», а не «ничего не изменено»"
reset; st_de03; upload 4.5.1-h14; run_a
chmod 000 $R/etc; run_b; chmod 755 $R/etc
echo '{ path=/x ; argv[]=/py /opt/other/stable_sync.py ; }' > $R/execstart
run_b; rc=$?; rm -f $R/execstart
ok "доделка при чужом ExecStart: «не доделан (файлы в работе частично новые)», код 1" "[ $rc -eq 1 ] && grep -q 'не доделан (файлы в работе частично новые)' $R/b.out && ! grep -q 'отменён, ничего не изменено' $R/b.out"

echo "=== 46. блокировку не открыть (не root) — понятный отказ, а не «дождитесь»"
reset; st_de03; upload 4.5.1-h14; mkdir -p $R/root/sync-h14.lock
run_a; ra=$?; run_b; rb=$?; run_c; rcc=$?; run_rb; rr=$?; rmdir $R/root/sync-h14.lock
ok "A, B, C и откат: «запускайте от root», код 1" "[ $ra -eq 1 ] && [ $rb -eq 1 ] && [ $rcc -eq 1 ] && [ $rr -eq 1 ] && grep -q 'запускайте от root' $R/a.out && grep -q 'запускайте от root' $R/b.out && grep -q 'запускайте от root' $R/c.out && grep -q 'запускайте от root' $R/r.out"

echo "=== 47. собранный синк повреждён вместе с маркером — B отказывает «не компилируется»"
reset; st_de03; upload 4.5.1-h14; run_a; N=$(cat $R/root/sync-h14.next)
echo 'def (' >> $T/stable_sync.py.new; sha256sum < $T/stable_sync.py.new | cut -c1-64 > $N/new.sha256; : > $R/systemctl.log
run_b; rc=$?
ok "B: «сборка не компилируется», до остановки таймера" "[ $rc -eq 1 ] && grep -q 'сборка не компилируется' $R/b.out && ! grep -q '^stop' $R/systemctl.log"

echo "=== 48. сбой B во время переключения и сбой запуска таймера — итог «таймер: …» всё равно напечатан"
reset; st_de03; upload 4.5.1-h14; run_a
chmod 000 $R/etc; touch $R/fail.start; run_b; rc=$?; chmod 755 $R/etc; rm -f $R/fail.start
ok "trap дописал строку «таймер:» и сообщение о сбое один раз" "[ $rc -ne 0 ] && grep -q '^таймер: ' $R/b.out && [ \"\$(grep -c 'упал во время переключения' $R/b.out)\" = 1 ]"

echo "итого: прошло $PASS, не прошло $FAILN"
[ $FAILN -eq 0 ]
