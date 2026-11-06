# Runbook v4.5.x-h14 — child-панель на Hiddify Manager 14

Применимо к Hiddify Manager 14.x в роли child (проверено на 14.1.0b4, standalone, MariaDB 10.11)
и parent на Hiddify 11/12 с admin-API v2. Python venv `/opt/hiddify-manager/.venv313`.
Файлы синка — в `/opt/hiddify-custom/tools/` (не затираются обновлением Hiddify).
Все команды — от root на дочернем сервере, если не сказано иное.

## 0. Зачем отдельный вариант

- В Hiddify 14 у таблицы `user` есть NOT NULL-колонки без значения по умолчанию (`deleted`,
  `last_modified_time`), а MariaDB работает в `STRICT_TRANS_TABLES`. INSERT из v4.3/v4.4 падает с
  `1364 Field 'deleted' doesn't have a default value`, шаг 6 откатывается целиком — новые
  пользователи parent на child не появляются, выключения не применяются.
- v4.3 при этом выходил с кодом 0: systemd показывал `Result=success`, health — `healthy`
  («таймер активен и БД доступна»). На de03 так прошло больше суток.
- Сырой SQL в 14 пользователя рабочим не делает (панель не следит за БД), а helper-ы xtlsapi
  из v4.x на 14 вредны: email `uuid@hiddify.com` вместо uuid, `ValueError` на trojan, неверный flow.

## 1. Как v4.5.x-h14 работает с Hiddify 14

| Что | Как | Почему |
|---|---|---|
| Новый пользователь, активный на parent | `POST /api/v2/admin/user/` локального admin-API | панель сама добавляет его во все движки |
| Новый, но неактивный на parent | не создаётся, пока не станет активным | POST в 14 добавляет в движки без проверки активности |
| enable (= `is_active` parent), лимит, дни, дата старта, режим, ключи | `PATCH` только отличающихся полей, результат сверяется с ответом | каждый PATCH перезагружает пользователя в движках |
| имя, комментарий, telegram_id, язык, `last_reset_time` | SQL + `last_modified_time=NOW()` | на движки не влияют; upsert 14 не применяет пустые значения и `last_reset_time` |
| Нет на parent два прогона подряд | `DELETE` (мягкое удаление) | разовый сбой списка parent не удаляет |
| Трафик child → parent | на parent прибавляется дельта, локально вычитаются ровно отправленные байты | без двойного учёта и потерь при частичном сбое |

Особенности API 14 (hiddifypanel 14.1.0b4), на которые опирается синк:

- POST: `name` обязателен (пустой → `user-<первые 8 символов uuid>`); POST с uuid мягко
  удалённой строки удаляет её окончательно и создаёт заново; если пользователь в БД есть, но
  не виден в `GET user/` (чужое дерево админа ключа) — `400 The user exists`: синк считает это
  ошибкой и ничего не PATCH-ит вслепую.
- PATCH: remove_client → upsert → add_client только активному; ответ — пользователь после upsert
  (`to_schema()`), поэтому не применённое панелью поле видно сразу. upsert применяет
  name/comment/telegram_id только непустые, `start_date` и `package_days` — независимо, ed25519 —
  только парой, `usage_limit_GB` обрезает до 1e6 ГБ.
- `GET user/` отдаёт неудалённых пользователей дерева админа ключа (и выключенных тоже); пустой
  список — `404 You have no user`.
- Каждый POST/PATCH/DELETE ставит apply-users: перегенерация `generated/hiddify-core.json` и
  `generated/telemt.toml` + SIGHUP hiddify-core и telemt (на de03 — через ~4 с). Xray в режиме
  users не перезагружается: пользователи меняются в нём по gRPC сразу.
- `start_date`: реальная дата parent синхронизируется всегда; `None` (пакет ещё не начат)
  отправляется, только если пакет на child истёк по его часам, а на parent пользователь активен
  (продление «сбросом дней»), — иначе было бы бесконечное перебрасывание.

## 2. Предохранители и состояние

Каталог состояния `/var/lib/hiddify-child-sync/` (0700), файлы 0600, ключи вида `u:<sha1(uuid)[:8]>`,
без имён, UUID и значений полей:
`status.json` — итог последнего прогона для health; `pending_delete.json`, `pending_block.json` —
отметки `[первое, последнее наблюдение]`; `last_patch.json` — отпечатки (sha256) отправленных полей.

- Отметки ведутся по списку parent в каждом прогоне, даже если шаг 6 не дошёл до них (сбой
  локального API, `SYNC_SKIP_USERS=1`): вернувшийся на parent снимается с удаления, снова активный —
  с выключения. Отметка продолжается, только если прошлое наблюдение было не раньше 15 минут назад
  (`SEEN_GAP_S`); иначе — начинается заново.
- **Удаление**: только отсутствующих на parent два прогона подряд (≥ 4 мин). Больше max(3, 5%)
  локальных разом — массовое: удаление ждёт час подтверждений (предупреждение `held_deletes`, health
  `degraded`) и идёт по max(3, 5%) за прогон, старейшие первыми. Больше половины локальных — похоже
  на сбой parent: ошибка, само не удаляется никогда. При битых записях в списке parent удаление в
  прогоне выключено.
- **Массовое выключение** (больше max(5, 10% активных)) применяется, только если parent требует его
  два прогона подряд; остальной синк при этом идёт. Отметка снимается только после успешного PATCH:
  выключение, срезанное лимитом записей, бюджетом времени или сбоем, повторно не ждёт.
- **Не больше 40 записей через API за прогон**, остальное — в следующих прогонах (порядок:
  выключения, включения, создания, прочие изменения; `backlog`, health `degraded`).
- **Не применённое панелью поле** (PATCH успешен, а по ответу поле не изменилось) — ошибка прогона;
  тот же PATCH не повторяется каждые 5 минут, повтор раз в 6 ч (снова ошибка, если не применился);
  повторить раньше — удалить `last_patch.json`.
- **TLS parent проверяется** (`PARENT_TLS_VERIFY = True`); для parent с самоподписанным
  сертификатом укажите путь к сертификату/CA (имя parent должно быть в его SAN: urllib3 2.x не
  сверяет CN); `False` — без проверки (не рекомендуется).
- Бюджет времени прогона 600 с (`TimeoutStartSec=900` в drop-in), `flock /run/hiddify-child-sync.lock`.
- Ключи запуска: `--dry-run` — только план (ничего не меняет и не пишет состояние);
  `--allow-mass` — разово: снять лимит записей и задержку массового выключения, удалить сейчас
  (без часа ожидания и лимита) подтверждённых — тех, кого нет на parent два наблюдения подряд не
  ближе 4 минут; `--allow-mass-delete` — то же и разрешить удалить больше половины локальных. Окружение: `SYNC_SKIP_USERS=1` — аварийно
  выключить шаг 6, `SYNC_ANON=1` — журнал без имён пользователей.
- Журнал и статус проходят через скраббер: URL и ключ parent, admin path, хост, UUID не пишутся.

## 3. Мониторинг

```bash
curl -s http://127.0.0.1:8081/api/v2/hiddify-sync/health | python3 -m json.tool | head -20
cat /var/lib/hiddify-child-sync/status.json; echo
systemctl show -p Result -p ExecMainStatus hiddify-child-sync.service
journalctl -u hiddify-child-sync -n 40 -o cat
SYNC_SCRIPT=/opt/hiddify-custom/tools/stable_sync.py /opt/hiddify-manager/.venv313/bin/python /opt/hiddify-custom/tools/check_engines.py --retry 3
```

- `unhealthy` (HTTP 503): таймер не активен, БД недоступна, статуса нет или он старше 15 минут,
  два прогона подряд с ошибкой, шаг 6 не проходил больше 15 минут.
- `degraded` (HTTP 200): одна ошибка, `SYNC_SKIP_USERS=1`, в БД не столько пользователей, сколько
  ожидается по parent, синк догоняет (`backlog`), массовое выключение или удаление ждёт.
- Любая ошибка шага → код выхода 1 → `Result=exit-code` у юнита.
- `check_engines.py` сверяет активных пользователей панели с движками: xray — живое состояние по
  gRPC API; hiddify-core — сгенерированный конфиг и то, что служба активна (живого списка у движка
  нет); telemt — живой список API. Движок без своего generated-файла — «не используется».
  Код выхода: 0 — совпадает, 1 — расхождения, 2 — не все движки удалось проверить. Сразу после
  изменений hiddify-core/telemt догоняют за секунды — `--retry N` повторяет сверку с паузой 5 с.
  Печатает только числа и обезличенные id.

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

Если у parent самоподписанный сертификат — положите его на child и пропишите путь в
`PARENT_TLS_VERIFY`. Перед включением посмотрите план dry-run: ожидаются POST по числу недостающих
пользователей и почти ни одного PATCH. Если одно и то же поле PATCH-ится у многих — формат
разошёлся, не включайте.

## 5. Обновление (с v4.3/v4.4 или между сборками v4.5.x — так обновляется de03)

Скрипты `deploy/v14/` печатают только числа, обезличенные id и итоговые строки журнала. Запускайте
их **файлом** с сервера, а не через `bash -s` из ssh: тогда обрыв соединения не обрезает скрипт.

```bash
# с рабочей машины: загрузить новую сборку и скрипты
ssh child 'mkdir -p -m 700 /tmp/sync14'
scp src/v14/stable_sync.py src/v14/sync_health_api.py src/v14/check_engines.py deploy/v14/* child:/tmp/sync14/

# A: бэкап (файлы + mysqldump таблицы user), сборка stable_sync.py.new, dry-run. Ничего не включает.
ssh child 'sudo -n bash /tmp/sync14/deploy-a.sh'
# B: проверка входных файлов → остановка таймера → установка, drop-in, реальный прогон, health,
#    движки, таймер, dry-run. Аргументы — id новых пользователей из плана POST этапа A (необязательно).
ssh child 'sudo -n bash /tmp/sync14/deploy-b.sh u:xxxxxxxx u:yyyyyyyy'
# C: через ~10 минут — прогоны таймера, health, движки; снять временный SYNC_ANON, прибрать
#    (код выхода — как у check_engines: 0 совпадает, 1 расхождения, 2 не всё проверено)
ssh child 'sudo -n bash /tmp/sync14/deploy-c.sh <epoch после этапа B>'
```

- Нестандартные каталоги — через `env` (переменные с рабочей машины через ssh/sudo не доходят):
  `ssh child 'sudo -n env SYNC_TOOLS=/opt/x SYNC_UPLOAD=/tmp/x bash /tmp/x/deploy-a.sh'`.
  Этап B откажется, если юнит запускает синк не из `SYNC_TOOLS`.
- `build_new.py` переносит из работающего файла `PARENT_URL`, `API_KEY`, блок `DB_CONFIG` и, если он
  там есть, `PARENT_TLS_VERIFY`, и отказывается писать результат, если кроме этих областей он хоть
  байтом отличается от нового кода или в нём не та `VERSION`. Dry-run этапа A запускается только для
  файла новой версии: старый код с `--dry-run` сделал бы настоящий прогон.
- Точки отката: `/root/sync-h14.next` — кандидат этапа A (бэкап того, что заменит этап B, с маркером
  `new.sha256`); `/root/sync-h14.last` — то, что заменил последний этап B (переносится этапом B в
  момент переключения, поэтому этап A «посмотреть план» точку отката не сдвигает);
  `/root/sync-h14.baseline` — версия до первой v4.5 (пишется один раз). Каждый кандидат помнит
  предыдущую точку отката (`prev-last`), так что откатываться можно и дальше по цепочке. Если вся
  сборка (синк, health, check_engines, rollback.sh) уже в работе, этап A только показывает dry-run;
  если этап B кандидата начат и не закончен, этапы A и C отказываются и просят доделать B.
- Этап B всё проверяет и раскладывает до остановки таймера: маркер этапа A, что загружена та же
  сборка, которую собирал этап A, что в работе всё ещё то, что этап A сохранил в бэкап (иначе — откат
  или правка конфига после этапа A: «повторите этап A»), файлы загрузки и их компиляцию, путь в юните;
  кладёт `rollback.sh` рядом с синком. После остановки таймер
  запускается снова при любом выходе (trap), обрыв ssh этап не прерывает; журнал этапа —
  `deploy-b.log` в каталоге кандидата. Законченный этап помечается `deploy-b.done`; если этап B
  упал после переключения, повторный запуск его доделывает, а «уже выкачена» отвечает, только
  когда этап закончен и все файлы сборки совпадают.
- Ожидаемо после этапа B: `Result=success`, `status.json` `ok: true`, `expected_count == local_count`,
  health `healthy`, движки совпадают с панелью, второй прогон и dry-run — 0 изменений.
- На время проверки журнал синка обезличен runtime-drop-in-ом
  `/run/systemd/system/hiddify-child-sync.service.d/anon.conf` (`SYNC_ANON=1`); этап C его снимает
  (перезагрузка тоже).

## 6. Если что-то пошло не так

**Сначала — аварийный режим, без отката файлов.** Синк перестаёт трогать пользователей, а трафик и
last_online продолжают синхронизироваться; health честно показывает `degraded`:

```bash
install -d -m 0755 /etc/systemd/system/hiddify-child-sync.service.d
printf '[Service]\nEnvironment=SYNC_SKIP_USERS=1\n' > /etc/systemd/system/hiddify-child-sync.service.d/90-skip-users.conf
systemctl daemon-reload
# вернуть: rm /etc/systemd/system/hiddify-child-sync.service.d/90-skip-users.conf && systemctl daemon-reload
```

**Откат на предыдущую сборку** (то, что заменил последний этап B; `/root/sync-h14.last`):

```bash
ssh child 'sudo -n bash /opt/hiddify-custom/tools/rollback.sh'     # этап B кладёт его рядом с синком
```

Всё проверяет до остановки таймера (бэкап, путь в юните) и отказывается, если в работе уже всё
сохранённое (синк, health, check_engines) или если сохранённая версия старше v4.5; после отката
печатает предыдущую точку цепочки, если она есть; после остановки таймер запускается снова при любом
выходе, сбой health API откат не прерывает. Созданных синком пользователей не трогает; дамп таблицы
`user` до выкатки — `user-table.sql` в каталоге бэкапа.

**Откат на версию до v4.5** (`rollback.sh --baseline`) — только осознанно: на Hiddify 14 v4.3 не
создаёт пользователей, не применяет выключения parent и при этом пишет `success`/`healthy`. После
такого отката проверяйте блокировки вручную.

## 7. Тесты

```bash
python3 tests/v14/test_sync_h14.py          # 83 теста, Python 3.12+, без сервера
bash tests/v14/deploy_sandbox.sh            # 52 проверки deploy-a/b/c и rollback в песочнице
```

Заглушки `requests`/`pymysql`/`urllib3`; локальный admin-API и БД — модель поведения API 14
(upsert-правила, ответ PATCH после upsert, 422 без name, 400 exists, мягкий DELETE, 404 «no user»).
Есть проверка сходимости: второй прогон после изменений — 0 записей. Песочница deploy заменяет
systemctl, mysqldump, journalctl, curl и venv-python заглушками и проверяет точки отката, обрыв
канала, сбои до и после остановки таймера, доделку незаконченного этапа B, откат и правку конфига
между этапами A и B, сборки, где изменился только health.

## 8. Известные ограничения

- parent на Hiddify 12 запускает apply_users на каждый PATCH трафика и last_online от child —
  нагрузка на parent (вне синка).
- `start_date` на parent не отправляется: дату первого подключения ставит child, срок на parent
  считается по его собственной дате.
- Нет сверки «БД → движки» в каждом прогоне (как реконсиляция в v4.4): если apply-users на child не
  отработает, синк этого не заметит — для проверки есть `check_engines.py` (hiddify-core — только по
  сгенерированному конфигу и состоянию службы).
- Как SIGHUP hiddify-core и telemt влияет на текущие сессии клиентов, не измерялось.
