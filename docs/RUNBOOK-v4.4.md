# Runbook v4.4 — обновление дочерней панели и установка на новую

Применимо к Hiddify Manager 12.3.x (child-панель), Python venv `/opt/hiddify-manager/.venv313`.
Все команды — от root (`sudo -i`) на дочернем сервере.

## 0. Что меняется и чего ждать

- Синк создаёт пользователей через локальный admin-API и каждый цикл (5 мин) сверяет
  отрендеренные конфиги движков с БД. При расхождении выполняется `install.sh apply_users`:
  sing-box получает SIGHUP, интерфейс WireGuard пересоздаётся, на части панелей
  перезапускается Xray. У клиентов на несколько секунд рвутся сессии.
- Первый запуск после обновления почти наверняка выполнит `apply_users` (так чинятся
  пользователи, которых нет в рендере).
- Trojan hot-add начинает работать после патча xtlsapi и перезапуска `hiddify-panel`.

## 1. Обновление существующей панели (например, ru2)

```bash
# 1.1 Бэкап текущего синка и конфигов движков
TS=$(date +%Y%m%d-%H%M%S)
cp -a /opt/hiddify-manager/stable_sync.py /opt/hiddify-manager/stable_sync.py.bak.$TS
tar -czf /root/hiddify-configs-pre-v44-$TS.tgz -C /opt/hiddify-manager xray/configs singbox/configs 2>/dev/null; ls -la /root/hiddify-configs-pre-v44-$TS.tgz
cp -a /etc/wireguard/hiddifywg.conf /root/hiddifywg.conf.bak.$TS 2>/dev/null || true

# 1.2 Сохранить текущие PARENT_URL и API_KEY (они будут подставлены в новую версию)
grep -E '^(PARENT_URL|API_KEY) *=' /opt/hiddify-manager/stable_sync.py > /root/.sync-params.$TS; chmod 600 /root/.sync-params.$TS

# 1.3 Установить v4.4 штатным установщиком (спросит Parent URL и API Key — взять из /root/.sync-params.$TS)
bash <(curl -fsSL https://raw.githubusercontent.com/slavafedoseev/hiddify-child-parent-user-sync/main/install.sh)
```

Ручной вариант без установщика (если нет доступа к GitHub с сервера): скопировать
`src/stable_sync.py`, `src/hiddify-patch-xtlsapi-trojan.py`, `systemd/hiddify-panel.service.d/xtlsapi-trojan-patch.conf`,
`systemd/hiddify-child-sync.service.d/10-apply-timeout.conf`, подставить `PARENT_URL`/`API_KEY`
(`sed -i 's|PARENT_URL = ".*"|PARENT_URL = "<url>"|'` и аналогично для `API_KEY`), затем:

```bash
chown root:hiddify-common /opt/hiddify-manager/stable_sync.py; chmod 0640 /opt/hiddify-manager/stable_sync.py
chmod +x /usr/local/bin/hiddify-patch-xtlsapi-trojan.py
systemctl daemon-reload
/usr/bin/python3 /usr/local/bin/hiddify-patch-xtlsapi-trojan.py && systemctl restart hiddify-panel.service
```

## 2. Проверка перед боевым запуском

```bash
# план операций без изменений (трафик/last_online не трогаются)
/opt/hiddify-manager/.venv313/bin/python /opt/hiddify-manager/stable_sync.py --dry-run | tail -40

# патч xtlsapi на месте
grep -c PATCHED-trojan-noflow /opt/hiddify-manager/.venv313/lib/python3.13/site-packages/xtlsapi/api_services/handler/add_client.py   # 1

# drop-in'ы
systemctl cat hiddify-child-sync.service | grep -A1 TimeoutStartSec
systemctl cat hiddify-panel.service | grep xtlsapi
```

## 3. Первый запуск и контроль

```bash
systemctl start hiddify-child-sync.service
journalctl -u hiddify-child-sync.service -n 60 --no-pager | grep -E 'Расхождение|apply_users|Движки|создано|ERROR|❌'
tail -20 /opt/hiddify-manager/log/system/sync-apply-users.log

# пользователь U должен быть во всех движках (пример для Hysteria2 и WireGuard):
U=<uuid>
grep -c "$U" /opt/hiddify-manager/singbox/configs/05_inbounds_*hysteria*.json
grep -c "$U" /opt/hiddify-manager/xray/configs/05_inbounds_new.json
wg show hiddifywg allowed-ips | grep -c "10.90.0.$((ID+1))"   # ID — id пользователя в таблице user
systemctl is-active hiddify-xray hiddify-singbox wg-quick@hiddifywg
```

Проверка с тест-клиента (ожидаются живые узлы всех протоколов для нового пользователя):
`python3 ~/sbtest/splitcheck.py singbox <полный sing-box-конфиг пользователя>` и
`python3 ~/sbtest/run_ru_test.py <конфиг> <label> '<France|England>'`.

## 4. Установка на новую дочернюю панель

Та же команда установщика (п. 1.3). После установки — п. 2 и п. 3. Дополнительно
проверить, что панель в режиме child и admin-API доступен локально:
`curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9000/<admin_path>/api/v2/admin/user/ -H 'Hiddify-API-Key: <key>'` → 200.

## 5. Откат

```bash
systemctl stop hiddify-child-sync.timer
cp -a /opt/hiddify-manager/stable_sync.py.bak.<TS> /opt/hiddify-manager/stable_sync.py
rm -f /etc/systemd/system/hiddify-child-sync.service.d/10-apply-timeout.conf \
      /etc/systemd/system/hiddify-panel.service.d/xtlsapi-trojan-patch.conf
systemctl daemon-reload && systemctl start hiddify-child-sync.timer
```

Патч xtlsapi откатывается переустановкой пакета в venv
(`/opt/hiddify-manager/.venv313/bin/pip install --force-reinstall xtlsapi==3.3.0`) — без него
hot-add trojan снова будет молча падать. Отрендеренные конфиги движков — штатный результат
`apply_users` из БД, откатывать их не нужно; при необходимости — tar из п. 1.1.
