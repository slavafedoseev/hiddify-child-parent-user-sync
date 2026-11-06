#!/bin/bash
# Песочница для deploy/v14 (deploy-a/b/c и rollback): заглушки systemctl/mysqldump/journalctl/curl/install
# и venv-python, пути /root, /var/lib, /etc/systemd, /run/systemd, venv заменены на каталоги песочницы.
# usage: bash tests/v14/deploy_sandbox.sh
set -u
CODE=$(cd "$(dirname "$0")/../../src/v14" && pwd); DEP=$(cd "$(dirname "$0")/../../deploy/v14" && pwd)
R=$(mktemp -d); trap 'rm -rf "$R"' EXIT
T=$R/tools; U=$R/upload; mkdir -p $T $U $R/bin $R/root $R/etc $R/run $R/varlib
PASS=0; FAILN=0
ok() { if eval "$2"; then PASS=$((PASS+1)); echo "  ✓ $1"; else FAILN=$((FAILN+1)); echo "  ✗ $1"; fi; }

# --- заглушки
cat > $R/bin/systemctl <<EOF
#!/bin/bash
echo "\$*" >> $R/systemctl.log
case "\$1" in
  is-active) cat $R/state.\${2%%.*} 2>/dev/null || echo inactive ;;
  stop) echo inactive > $R/state.\${2%%.*} ;;
  start) [ -f $R/fail.start ] && exit 1; echo active > $R/state.\${2%%.*} ;;
  restart) [ -f $R/fail.restart ] && exit 1; echo active > $R/state.\${2%%.*} ;;
  show) case "\$*" in *ExecStart*) cat $R/execstart 2>/dev/null || echo "{ path=/x ; argv[]=/py $T/stable_sync.py ; }";; *Result*) echo success;; *ExecMainStatus*) echo 0;; *Environment*) cat $R/run/hiddify-child-sync.service.d/anon.conf 2>/dev/null | grep -o 'SYNC_ANON=1';; *) echo 15min;; esac ;;
  list-timers) printf 'NEXT\nsoon\n' ;;
esac
exit 0
EOF
cat > $R/bin/mysqldump <<'EOF'
#!/bin/bash
echo "INSERT INTO user VALUES (1);"
EOF
cat > $R/bin/journalctl <<'EOF'
#!/bin/bash
echo "[x] === ⚙ Starting Stable Accumulative Sync v4.5.1-h14 ==="
EOF
cat > $R/bin/curl <<'EOF'
#!/bin/bash
echo '{"status": "healthy", "reasons": [], "database": {"user_count": 3}}'
EOF
cat > $R/bin/install <<'EOF'
#!/bin/bash
args=(); skip=0
for a in "$@"; do if [ $skip = 1 ]; then skip=0; continue; fi; case "$a" in -o|-g) skip=1;; *) args+=("$a");; esac; done
exec /usr/bin/install "${args[@]}"
EOF
cat > $R/bin/pystub <<EOF
#!/bin/bash
case "\$*" in
  *--dry-run*) echo "[t] === dry-run ==="; echo "[t] [dry-run] ➕ POST u:aaaaaaaa"; exit 0 ;;
  *check_engines.py*) echo "ИТОГ: движки совпадают с панелью"; exit 0 ;;
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
  sed "s/^VERSION = .*/VERSION = '$1'/" $CODE/stable_sync.py > $U/stable_sync.py
  cp $CODE/sync_health_api.py $CODE/check_engines.py $DEP/build_new.py $DEP/rollback.sh $U/
}
reset() { rm -rf $T/* $R/root/* $R/etc/* $R/run/* $R/varlib/* $R/state.* $R/systemctl.log $R/fail.* $R/execstart; mkdir -p $T $U; echo active > $R/state.hiddify-child-sync; mkstatus; }

echo "=== 1. первое обновление с v4.3: A, B, откат"
reset; v43 > $T/stable_sync.py; echo 'print("health v4.3")' > $T/sync_health_api.py; chmod 700 $T/stable_sync.py; upload 4.5.1-h14
bash $R/deploy-a.sh > $R/a.out 2>&1; ok "A прошёл" "[ \$? -eq 0 ]"
B1=$(cat $R/root/sync-h14.next)
ok "A: кандидат .next, .last не тронут" "[ -d \"$B1\" ] && [ ! -f $R/root/sync-h14.last ]"
ok "baseline = бэкап v4.3" "[ \"\$(cat $R/root/sync-h14.baseline)\" = \"$B1\" ] && grep -qx 4.3 $B1/prev.version"
ok "маркер A записан" "[ -s $B1/new.sha256 ]"
ok "конфиг перенесён" "grep -q 'AbCdEfPath' $T/stable_sync.py.new && grep -q \"^VERSION = '4.5.1-h14'\" $T/stable_sync.py.new"
bash $R/deploy-b.sh u:aaaaaaaa > $R/b.out 2>&1
ok "B: в работе 4.5.1" "grep -q \"^VERSION = '4.5.1-h14'\" $T/stable_sync.py"
ok "B: таймер запущен" "grep -qx active $R/state.hiddify-child-sync"
ok "B: журнал этапа" "grep -q 'реальный прогон 1' $B1/deploy-b.log"
ok "B: .last = кандидат, .next снят, done" "[ \"\$(cat $R/root/sync-h14.last)\" = \"$B1\" ] && [ ! -f $R/root/sync-h14.next ] && [ -f $B1/deploy-b.done ]"
ok "B: check_engines и rollback.sh рядом с синком" "[ -f $T/check_engines.py ] && [ -x $T/rollback.sh ]"
bash $R/rollback.sh > $R/r.out 2>&1; rc=$?
ok "откат без --baseline на v4.3 — отказ" "[ $rc -ne 0 ] && grep -q 'только явно' $R/r.out && grep -q \"4.5.1-h14\" $T/stable_sync.py"
bash $R/rollback.sh --baseline > $R/r.out 2>&1
ok "откат --baseline → v4.3 с предупреждением" "grep -q 'v4.3' $T/stable_sync.py && grep -q '⚠️' $R/r.out && [ ! -f $R/etc/hiddify-child-sync.service.d/timeout.conf ]"

echo "=== 2. de03: 4.5-h14 → 4.5.1-h14, старый .last с .v43"
reset; OLD=$R/root/sync-h14-old; mkdir -p $OLD; v43 > $OLD/stable_sync.py.v43; echo 'print("h43")' > $OLD/sync_health_api.py.v43
echo $OLD > $R/root/sync-h14.last
v43 | sed '1a VERSION = '"'"'4.5-h14'"'"'' > $T/stable_sync.py; echo 'print("health 4.5")' > $T/sync_health_api.py; chmod 700 $T/stable_sync.py
upload 4.5.1-h14
bash $R/deploy-a.sh > $R/a.out 2>&1; ok "A прошёл" "[ \$? -eq 0 ]"
B2=$(cat $R/root/sync-h14.next)
ok "baseline = старый .v43, .last пока прежний" "[ \"\$(cat $R/root/sync-h14.baseline)\" = \"$OLD\" ] && [ \"\$(cat $R/root/sync-h14.last)\" = \"$OLD\" ]"
ok "кандидат = бэкап 4.5-h14" "grep -qx 4.5-h14 $B2/prev.version"
ok "PARENT_TLS_VERIFY по умолчанию нового кода" "grep -q '^PARENT_TLS_VERIFY = True' $T/stable_sync.py.new"
bash $R/deploy-b.sh > $R/b.out 2>&1
ok "после B .last = кандидат" "[ \"\$(cat $R/root/sync-h14.last)\" = \"$B2\" ]"
bash $R/rollback.sh > $R/r.out 2>&1
ok "откат → 4.5-h14 (prev)" "grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py && grep -q 'health 4.5' $T/sync_health_api.py"
bash $R/rollback.sh > $R/r.out 2>&1; rc=$?
ok "повторный откат — «нечего»" "[ $rc -ne 0 ] && grep -q 'откатывать нечего' $R/r.out"

echo "=== 3. B без каталога загрузки (перезагрузка между A и B) — таймер не трогаем"
reset; v43 > $T/stable_sync.py; echo 'print("h")' > $T/sync_health_api.py; upload 4.5.1-h14
bash $R/deploy-a.sh > /dev/null 2>&1; rm -rf $U/*; : > $R/systemctl.log
bash $R/deploy-b.sh > $R/b.out 2>&1; rc=$?
ok "B отказал до остановки таймера" "[ $rc -ne 0 ] && grep -q 'отменён' $R/b.out && ! grep -q '^stop' $R/systemctl.log && grep -qx active $R/state.hiddify-child-sync"
ok "живой файл не тронут" "grep -q 'v4.3' $T/stable_sync.py"
upload 4.5.1-h14; echo '# изменено' >> $T/stable_sync.py.new
bash $R/deploy-b.sh > $R/b.out 2>&1; rc=$?
ok "B: .new не тот, что проверял A — отказ" "[ $rc -ne 0 ] && grep -q 'не тот' $R/b.out"

echo "=== 4. сбой после переключения: trap запускает таймер, повторный B доделывает"
reset; v43 > $T/stable_sync.py; echo 'print("h")' > $T/sync_health_api.py; upload 4.5.1-h14
bash $R/deploy-a.sh > /dev/null 2>&1; B4=$(cat $R/root/sync-h14.next)
chmod 000 $R/etc   # mkdir drop-in упадёт под set -e — уже после переключения синка
bash $R/deploy-b.sh > $R/b.out 2>&1; rc=$?; chmod 755 $R/etc
ok "B упал" "[ $rc -ne 0 ]"
ok "таймер снова запущен" "grep -qx active $R/state.hiddify-child-sync && awk '/^stop hiddify-child-sync.timer/{s=NR} /^start hiddify-child-sync.timer/{t=NR} END{exit !(t>s && s>0)}' $R/systemctl.log"
ok "не помечен как завершённый" "[ ! -f $B4/deploy-b.done ] && [ -f $R/root/sync-h14.next ]"
bash $R/deploy-a.sh > $R/a.out 2>&1; rc=$?
ok "A после упавшего B: «не закончен», код 1, кандидат прежний" "[ $rc -eq 1 ] && grep -q 'начат и не закончен' $R/a.out && [ \"\$(cat $R/root/sync-h14.next)\" = \"$B4\" ]"
bash $R/deploy-c.sh 0 > $R/c.out 2>&1; rc=$?
ok "C после упавшего B: отказ, код 1, загрузка цела" "[ $rc -eq 1 ] && grep -q 'начат и не закончен' $R/c.out && [ -f $U/stable_sync.py ]"
: > $R/systemctl.log
bash $R/deploy-b.sh > $R/b.out 2>&1; rc=$?
ok "повторный B: «доделываю», а не «уже выкачена»" "[ $rc -eq 0 ] && grep -q 'доделываю' $R/b.out && ! grep -q 'уже выкачена' $R/b.out"
ok "повторный B: health перезапущен, drop-in на месте, done" "grep -q '^restart hiddify-sync-api' $R/systemctl.log && [ -f $R/etc/hiddify-child-sync.service.d/timeout.conf ] && [ -f $B4/deploy-b.done ] && [ ! -f $R/root/sync-h14.next ]"

echo "=== 5. обрыв канала (читатель вывода закрылся) — этап доходит до конца"
reset; v43 > $T/stable_sync.py; echo 'print("h")' > $T/sync_health_api.py; upload 4.5.1-h14
bash $R/deploy-a.sh > /dev/null 2>&1; B5=$(cat $R/root/sync-h14.next)
bash $R/deploy-b.sh 2>&1 | head -1 > /dev/null; sleep 1
ok "этап B доработал (dry-run в журнале)" "grep -q 'повторный dry-run' $B5/deploy-b.log && [ -f $B5/deploy-b.done ]"
ok "таймер запущен" "grep -qx active $R/state.hiddify-child-sync"

echo "=== 6. повтор A и B после выкатки; сборка, где изменился только health"
BL=$(cat $R/root/sync-h14.baseline); LAST=$(cat $R/root/sync-h14.last); MARK=$(cat $LAST/new.sha256)
bash $R/deploy-a.sh > $R/a.out 2>&1; rc=$?
ok "A: «уже в работе целиком», код 0" "[ $rc -eq 0 ] && grep -q 'уже в работе целиком' $R/a.out && [ ! -f $T/stable_sync.py.new ] && [ ! -f $R/root/sync-h14.next ]"
ok "A: .last, baseline и маркер прежние" "[ \"\$(cat $R/root/sync-h14.last)\" = \"$LAST\" ] && [ \"\$(cat $R/root/sync-h14.baseline)\" = \"$BL\" ] && [ \"\$(cat $LAST/new.sha256)\" = \"$MARK\" ]"
bash $R/deploy-b.sh > $R/b.out 2>&1; rc=$?
ok "повторный B: «уже выкачена», код 0, таймер не трогали" "[ $rc -eq 0 ] && grep -q 'уже выкачена' $R/b.out && grep -qx active $R/state.hiddify-child-sync"
bash $R/rollback.sh > $R/r.out 2>&1; rc=$?
ok "откат на v4.3 без --baseline — отказ без остановки таймера" "[ $rc -ne 0 ] && grep -q 'только явно' $R/r.out && grep -qx active $R/state.hiddify-child-sync"
echo '# health fix' >> $U/sync_health_api.py
bash $R/deploy-a.sh > $R/a.out 2>&1; rc=$?
ok "только health изменился: A готовит кандидата" "[ $rc -eq 0 ] && grep -q 'код синка тот же' $R/a.out && [ -f $R/root/sync-h14.next ]"
bash $R/deploy-b.sh > $R/b.out 2>&1
ok "B ставит новый health" "grep -q '# health fix' $T/sync_health_api.py && [ ! -f $R/root/sync-h14.next ]"
HL=$(cat $R/root/sync-h14.last)
bash $R/rollback.sh > $R/r.out 2>&1; rc=$?
ok "откат хотфикса health: прежний health, не «нечего»" "[ $rc -eq 0 ] && ! grep -q '# health fix' $T/sync_health_api.py && grep -q 'предыдущая точка отката' $R/r.out"
echo "$HL" > $R/root/sync-h14.last; cp $U/sync_health_api.py $T/sync_health_api.py   # вернуть хотфикс
bash $R/deploy-c.sh 0 > $R/c.out 2>&1; rc=$?
ok "C: код 0 после успешной выкатки" "[ $rc -eq 0 ]"
ok "C: снят SYNC_ANON, точки отката и цепочка живы" "[ ! -f $R/run/hiddify-child-sync.service.d/anon.conf ] && [ -d \"\$(cat $R/root/sync-h14.last)\" ] && [ -d \"\$(cat $R/root/sync-h14.baseline)\" ] && [ -d \"\$(cat \$(cat $R/root/sync-h14.last)/prev-last)\" ]"
ok "C: каталог загрузки удалён, rollback.sh на месте" "[ ! -d $U ] && [ -x $T/rollback.sh ]"

echo "=== 7. откат: health не поднялся — таймер всё равно запущен; чужой ExecStart — отказ до stop"
reset; OLD=$R/root/sync-h14-old; mkdir -p $OLD; v43 > $OLD/stable_sync.py.v43; echo 'print("h43")' > $OLD/sync_health_api.py.v43
echo $OLD > $R/root/sync-h14.last
v43 | sed '1a VERSION = '"'"'4.5-h14'"'"'' > $T/stable_sync.py; echo 'print("health 4.5")' > $T/sync_health_api.py; upload 4.5.1-h14
bash $R/deploy-a.sh > /dev/null 2>&1; bash $R/deploy-b.sh > /dev/null 2>&1
echo '{ path=/x ; argv[]=/py /opt/other/stable_sync.py ; }' > $R/execstart; : > $R/systemctl.log
bash $R/rollback.sh > $R/r.out 2>&1; rc=$?
ok "чужой ExecStart — отказ, таймер не останавливали" "[ $rc -ne 0 ] && grep -q 'запускает не' $R/r.out && ! grep -q '^stop' $R/systemctl.log"
rm -f $R/execstart; touch $R/fail.restart; : > $R/systemctl.log
bash $R/rollback.sh > $R/r.out 2>&1; rc=$?
ok "health не поднялся — откат доделан, таймер запущен" "grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py && grep -q 'health API не поднялся' $R/r.out && grep -qx active $R/state.hiddify-child-sync && grep -q '^start hiddify-child-sync.timer' $R/systemctl.log"

echo "=== 8. указатель со «/» в конце — C не удаляет точку отката"
reset; v43 > $T/stable_sync.py; echo 'print("h")' > $T/sync_health_api.py; upload 4.5.1-h14
bash $R/deploy-a.sh > /dev/null 2>&1; bash $R/deploy-b.sh > /dev/null 2>&1
B8=$(cat $R/root/sync-h14.last); echo "$B8/" > $R/root/sync-h14.baseline; echo "$B8/" > $R/root/sync-h14.last
cp -a $B8 $R/root/sync-h14-dup                                       # настоящий дубль
bash $R/deploy-c.sh 0 > $R/c.out 2>&1
ok "точка отката цела, дубль удалён" "[ -d $B8 ] && [ ! -d $R/root/sync-h14-dup ]"
bash $R/rollback.sh --baseline > $R/r.out 2>&1
ok "откат --baseline по указателю со «/» работает" "grep -q 'v4.3' $T/stable_sync.py"

echo "=== 9. A новой сборки без B не сдвигает точку отката"
reset; OLD=$R/root/sync-h14-old; mkdir -p $OLD; v43 > $OLD/stable_sync.py.v43; echo 'print("h43")' > $OLD/sync_health_api.py.v43
echo $OLD > $R/root/sync-h14.last
v43 | sed '1a VERSION = '"'"'4.5-h14'"'"'' > $T/stable_sync.py; echo 'print("health 4.5")' > $T/sync_health_api.py; upload 4.5.1-h14
bash $R/deploy-a.sh > /dev/null 2>&1; bash $R/deploy-b.sh > /dev/null 2>&1
L9=$(cat $R/root/sync-h14.last); upload 4.5.2-h14
bash $R/deploy-a.sh > $R/a.out 2>&1
ok "A 4.5.2: кандидат есть, .last прежний" "[ -f $R/root/sync-h14.next ] && [ \"\$(cat $R/root/sync-h14.last)\" = \"$L9\" ]"
bash $R/rollback.sh > $R/r.out 2>&1
ok "откат по §6 → 4.5-h14 (то, что заменил последний B)" "grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py"
bash $R/deploy-c.sh 0 > $R/c.out 2>&1
ok "C не трогает каталог кандидата" "[ -d \"\$(cat $R/root/sync-h14.next)\" ]"

echo "=== 10. откат между A и B; правка живого файла после A — B отказывает до остановки таймера"
reset; OLD=$R/root/sync-h14-old; mkdir -p $OLD; v43 > $OLD/stable_sync.py.v43; echo 'print("h43")' > $OLD/sync_health_api.py.v43
echo $OLD > $R/root/sync-h14.last
v43 | sed '1a VERSION = '"'"'4.5-h14'"'"'' > $T/stable_sync.py; echo 'print("health 4.5")' > $T/sync_health_api.py; upload 4.5.1-h14
bash $R/deploy-a.sh > /dev/null 2>&1; bash $R/deploy-b.sh > /dev/null 2>&1
upload 4.5.2-h14; bash $R/deploy-a.sh > /dev/null 2>&1                # кандидат 4.5.2 (бэкап 4.5.1)
bash $R/rollback.sh > /dev/null 2>&1                                   # инцидент: откат на 4.5-h14
: > $R/systemctl.log
bash $R/deploy-b.sh > $R/b.out 2>&1; rc=$?
ok "B после отката: «в работе не то», таймер не останавливали" "[ $rc -ne 0 ] && grep -q 'в работе не то' $R/b.out && ! grep -q '^stop' $R/systemctl.log && grep -q \"^VERSION = '4.5-h14'\" $T/stable_sync.py"
bash $R/deploy-a.sh > /dev/null 2>&1; sed -i 's/k-live/k-rotated/' $T/stable_sync.py; : > $R/systemctl.log
bash $R/deploy-b.sh > $R/b.out 2>&1; rc=$?
ok "B после правки живого конфига: отказ до stop" "[ $rc -ne 0 ] && grep -q 'в работе не то' $R/b.out && ! grep -q '^stop' $R/systemctl.log && grep -q k-rotated $T/stable_sync.py"

echo "=== 11. загружена другая сборка после A — B отказывает; «уже выкачена» не врёт"
reset; v43 > $T/stable_sync.py; echo 'print("h")' > $T/sync_health_api.py; upload 4.5.1-h14
bash $R/deploy-a.sh > /dev/null 2>&1
echo '# правка синка' >> $U/stable_sync.py; : > $R/systemctl.log
bash $R/deploy-b.sh > $R/b.out 2>&1; rc=$?
ok "B: «загружена не та сборка», таймер не останавливали" "[ $rc -ne 0 ] && grep -q 'загружена не та сборка' $R/b.out && ! grep -q '^stop' $R/systemctl.log"
upload 4.5.1-h14; bash $R/deploy-b.sh > /dev/null 2>&1                 # штатно выкатили
echo '# правка синка' >> $U/stable_sync.py
bash $R/deploy-b.sh > $R/b.out 2>&1; rc=$?
ok "B без A после новой загрузки: не «уже выкачена»" "[ $rc -ne 0 ] && ! grep -q 'уже выкачена' $R/b.out && grep -q 'сначала этап A' $R/b.out"

echo "итого: прошло $PASS, не прошло $FAILN"
[ $FAILN -eq 0 ]
