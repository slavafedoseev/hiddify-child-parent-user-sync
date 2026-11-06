# Runbook v4.5-h14 — child-панель на Hiddify Manager 14

Применимо к Hiddify Manager 14.x в роли child (проверено на 14.1.0b4, standalone, MariaDB 10.11)
и parent на Hiddify 11/12 с admin-API v2. Python venv `/opt/hiddify-manager/.venv313`.
Файлы синка — в `/opt/hiddify-custom/tools/` (не затираются обновлением Hiddify).
Все команды — от root на дочернем сервере.

## 0. Зачем отдельный вариант

- В Hiddify 14 у таблицы `user` есть NOT NULL-колонки без значения по умолчанию (`deleted`,
  `last_modified_time`), а MariaDB работает в `STRICT_TRANS_TABLES`. INSERT из v4.3/v4.4 падает с
  `1364 Field 'deleted' doesn't have a default value`, шаг 6 откатывается целиком — новые
  пользователи parent на child не появляются.
- v4.3 при этом выходил с кодом 0: systemd показывал `Result=success`, health — `healthy`
  («таймер активен и БД доступна»). На de03 так прошло больше суток.
- Сырой SQL в 14 пользователя рабочим не делает (панель не следит за БД), а helper-ы xtlsapi
  из v4.x на 14 вредны: email `uuid@hiddify.com` вместо uuid, `ValueError` на trojan, неверный flow.

## 1. Как v4.5-h14 работает с Hiddify 14

| Что | Как | Почему |
|---|---|---|
| Новый пользователь, активный на parent | `POST /api/v2/admin/user/` локального admin-API | панель сама добавляет его во все движки |
| Новый, но неактивный на parent | не создаётся, пока не станет активным | POST в 14 добавляет в движки без проверки активности |
| enable (= `is_active` parent), лимит, дни, дата старта, режим, ключи | `PATCH` только отличающихся полей | каждый PATCH перезагружает пользователя в движках |
| имя, комментарий, telegram_id, язык, `last_reset_time` | SQL + `last_modified_time=NOW()` | на движки не влияют; upsert 14 не применяет пустые значения и `last_reset_time` |
| Нет на parent два прогона подряд | `DELETE` (мягкое удаление) | разовый сбой списка parent не удаляет |
| Трафик child → parent | на parent прибавляется дельта, локально вычитаются ровно отправленные байты | без двойного учёта и потерь при частичном сбое |

Особенности API 14 (hiddifypanel 14.1.0b4), на которые опирается синк:

- POST: `name` обязателен (пустой → `user-<первые 8 символов uuid>`); POST с uuid мягко
  удалённой строки удаляет её окончательно и создаёт заново; если пользователь в БД есть, но
  не виден в `GET user/` (чужое дерево админа ключа) — `400 The user exists`: синк считает это
  ошибкой и ничего не PATCH-ит вслепую.
- PATCH: remove_client → upsert → add_client только активному. upsert применяет name/comment/
  telegram_id только непустые, `start_date` и `package_days` — независимо, ed25519 — только парой,
  `usage_limit_GB` обрезает до 1e6 ГБ.
- `GET user/` отдаёт неудалённых пользователей дерева админа ключа; пустой список — `404 You have no user`.
- Каждый POST/PATCH/DELETE ставит apply-users: перегенерация `generated/hiddify-core.json` и
  `generated/telemt.toml` + SIGHUP hiddify-core и telemt (на de03 — через ~4 с). Xray в режиме
  users не перезагружается: пользователи меняются в нём по gRPC сразу.
- `start_date`: реальная дата parent синхронизируется всегда; `None` (пакет ещё не начат)
  отправляется, только если пакет на child истёк по его часам, а на parent пользователь активен
  (продление «сбросом дней»), — иначе было бы бесконечное перебрасывание.

## 2. Предохранители и состояние

Каталог состояния `/var/lib/hiddify-child-sync/` (0700):
`status.json` — итог последнего прогона для health (без UUID, имён и секретов),
`pending_delete.json`, `pending_block.json`, `last_patch.json` — ключи вида `u:<sha1(uuid)[:8]>`.

- Массовое выключение (больше max(5, 10% активных)) применяется, только если parent требует его
  два прогона подряд; остальной синк при этом идёт.
- Удаление — только отсутствующих на parent два прогона подряд и не больше max(3, 5%) локальных за
  прогон (иначе ошибка и ничего не удаляется); при битых записях в списке parent удаление выключено.
- Не больше 40 записей через API за прогон, остальное — в следующих прогонах (порядок: выключения,
  включения, создания, прочие изменения).
- Не прижившееся поле (тот же PATCH поле=значение уже был успешен прошлым прогоном, а отличие
  осталось — формат API разошёлся) не повторяется каждые 5 минут: ошибка прогона, повтор раз в 6 ч.
- Бюджет времени прогона 600 с (`TimeoutStartSec=900` в drop-in), `flock /run/hiddify-child-sync.lock`.
- Ключи запуска: `--dry-run` — только план (ничего не меняет и не пишет состояние);
  `--allow-mass` — разово снять предохранители; окружение `SYNC_SKIP_USERS=1` — аварийно
  выключить шаг 6, `SYNC_ANON=1` — журнал без имён пользователей.
- Журнал и статус проходят через скраббер: URL и ключ parent, admin path, хост, UUID не пишутся.

## 3. Мониторинг

```bash
curl -s http://127.0.0.1:8081/api/v2/hiddify-sync/health | python3 -m json.tool | head -20
cat /var/lib/hiddify-child-sync/status.json; echo
systemctl show -p Result -p ExecMainStatus hiddify-child-sync.service
journalctl -u hiddify-child-sync -n 40 -o cat
/opt/hiddify-manager/.venv313/bin/python /opt/hiddify-custom/tools/check_engines.py
```

- `unhealthy` (HTTP 503): таймер не активен, БД недоступна, статуса нет или он старше 15 минут,
  два прогона подряд с ошибкой, шаг 6 не проходил больше 15 минут.
- `degraded` (HTTP 200): одна ошибка, `SYNC_SKIP_USERS=1`, в БД не столько пользователей, сколько
  ожидается по parent, синк догоняет (`backlog`), массовое выключение ждёт подтверждения.
- Любая ошибка шага → код выхода 1 → `Result=exit-code` у юнита.
- `check_engines.py` сверяет активных пользователей панели с живыми движками: xray (gRPC API,
  `xray api inbounduser` по каждому inbound из `generated/xray.json`), hiddify-core
  (`generated/hiddify-core.json`), telemt (API `127.0.0.1:10088/v1/users`). Печатает только
  числа и обезличенные id, код выхода 1 при расхождениях. Сразу после изменений движки
  hiddify-core/telemt догоняют за несколько секунд (apply-users).

## 4. Установка на новую child-панель

```bash
install -d -m 0755 /opt/hiddify-custom/tools
install -m 0700 src/v14/stable_sync.py src/v14/sync_health_api.py src/v14/check_engines.py /opt/hiddify-custom/tools/
# подставить свои значения (admin proxy path child совпадает с parent)
read -rp 'Parent URL с admin path: ' PURL; read -rsp 'API key parent: ' PKEY; echo
PURL="$PURL" PKEY="$PKEY" python3 -c '
import os
p = "/opt/hiddify-custom/tools/stable_sync.py"
s = open(p, encoding="utf-8").read()
s = s.replace("https://your-parent-panel.example.com/ADMIN_PATH", os.environ["PURL"], 1)
s = s.replace("your-api-key-here", os.environ["PKEY"], 1)
open(p, "w", encoding="utf-8").write(s)'
unset PURL PKEY
install -m 0644 systemd/v14/hiddify-child-sync.service systemd/v14/hiddify-sync-api.service systemd/hiddify-child-sync.timer /etc/systemd/system/
install -d -m 0755 /etc/systemd/system/hiddify-child-sync.service.d
install -m 0644 systemd/v14/hiddify-child-sync.service.d/10-timeout.conf /etc/systemd/system/hiddify-child-sync.service.d/
systemctl daemon-reload
cd /opt/hiddify-custom/tools && SYNC_ANON=1 /opt/hiddify-manager/.venv313/bin/python stable_sync.py --dry-run
systemctl enable --now hiddify-sync-api.service hiddify-child-sync.timer
```

Перед включением посмотрите план dry-run: ожидаются POST по числу недостающих пользователей,
и почти ни одного PATCH. Если одно и то же поле PATCH-ится у многих — формат разошёлся, не включайте.

## 5. Обновление с v4.3/v4.4 (так обновлён de03)

Скрипты `deploy/v14/` печатают только числа, обезличенные id и итоговые строки журнала.
Каталог установки — `SYNC_TOOLS` (по умолчанию `/opt/hiddify-custom/tools`), каталог загрузки —
`SYNC_UPLOAD` (по умолчанию `/tmp/sync14`).

```bash
# с рабочей машины
ssh child 'mkdir -p -m 700 /tmp/sync14'
scp src/v14/stable_sync.py src/v14/sync_health_api.py src/v14/check_engines.py deploy/v14/build_new.py child:/tmp/sync14/

# A: бэкап (файлы + mysqldump таблицы user), сборка stable_sync.py.new, dry-run. Ничего не включает.
ssh child 'sudo -n bash -s' < deploy/v14/deploy-a.sh
# B: остановить таймер, поставить файлы и drop-in, реальный прогон, health, движки, таймер, dry-run
ssh child 'sudo -n bash -s -- u:xxxxxxxx u:yyyyyyyy' < deploy/v14/deploy-b.sh   # id из плана POST этапа A
# C: через ~10 минут — прогоны таймера, health, движки; снять временный SYNC_ANON, прибрать
ssh child 'sudo -n bash -s -- <epoch после этапа B>' < deploy/v14/deploy-c.sh
```

- `build_new.py` переносит `PARENT_URL`, `API_KEY` и блок `DB_CONFIG` из работающего файла и
  отказывается писать результат, если кроме этих трёх областей он хоть байтом отличается от
  нового кода или в нём нет `VERSION = '4.5-h14'`. Важно: старый код с `--dry-run` сделал бы
  настоящий прогон, поэтому dry-run этапа A запускается только для файла v4.5-h14.
- Ожидаемо после этапа B: `Result=success`, `status.json` `ok: true`, `expected_count == local_count`,
  health `healthy`, движки совпадают с панелью, второй прогон и dry-run — 0 изменений.
- На время проверки журнал синка обезличен runtime-drop-in-ом
  `/run/systemd/system/hiddify-child-sync.service.d/anon.conf` (`SYNC_ANON=1`); этап C его снимает
  (перезагрузка тоже).

## 6. Откат

```bash
ssh child 'sudo -n bash -s' < deploy/v14/rollback.sh
```

Возвращает файлы из бэкапа этапа A (путь — в `/root/sync-h14.last`), снимает drop-in-ы,
перезапускает health API и таймер. Созданных синком пользователей не трогает; дамп таблицы `user`
до выкатки — `user-table.sql` в каталоге бэкапа.

## 7. Тесты

```bash
python3 tests/v14/test_sync_h14.py          # 56 тестов, Python 3.12+, без сервера
```

Заглушки `requests`/`pymysql`/`urllib3`; локальный admin-API и БД — модель поведения API 14
(upsert-правила, 422 без name, 400 exists, мягкий DELETE, 404 «no user»). Есть проверка
сходимости: второй прогон после изменений — 0 записей.

## 8. Известные ограничения

- parent на Hiddify 12 запускает apply_users на каждый PATCH трафика и last_online от child —
  нагрузка на parent (вне синка).
- `start_date` на parent не отправляется: дату первого подключения ставит child, срок на parent
  считается по его собственной дате.
- Нет сверки «БД → движки» в каждом прогоне (как реконсиляция в v4.4): если apply-users на child не
  отработает, синк этого не заметит — для проверки есть `check_engines.py`.
- Как SIGHUP hiddify-core и telemt влияет на текущие сессии клиентов, не измерялось.
