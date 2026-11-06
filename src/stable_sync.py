#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hiddify Manager Child-Parent User Synchronization v4.4.1

Стабильная синхронизация пользователей и трафика между child и parent панелями.
Использует прямые MySQL запросы (PyMySQL) вместо SQLAlchemy для надёжности.

ОСНОВНАЯ ФУНКЦИОНАЛЬНОСТЬ:
  - Получение списка пользователей с parent панели
  - Создание новых пользователей на child сервере
  - Обновление существующих пользователей (лимиты, статусы, ключи)
  - Блокировка пользователей, отсутствующих на parent
  - Накопительная синхронизация трафика (child → parent)
  - Автоматическое обнуление локального трафика после успешной отправки
  - Двунаправленная синхронизация last_online (child ↔ parent)
  - Создание/изменение/удаление юзеров через локальный admin-API child → панель сама делает
    gRPC hot-add в Xray и `install.sh apply_users` (sing-box reload + WireGuard) — новые юзеры
    работают по ВСЕМ протоколам без перезагрузки сервера
  - Реконсиляция rendered-конфигов движков с БД каждый цикл (самовосстановление)
  - Режим --dry-run: показать планируемые операции, ничего не меняя

ТРЕБОВАНИЯ:
  - Hiddify Manager v11+ (child panel mode)
  - Python 3.10+
  - PyMySQL
  - Доступ к parent панели через API

Проект: https://github.com/slavafedoseev/hiddify-child-parent-user-sync
Лицензия: MIT
"""

import sys
import os
import requests
import pymysql
from datetime import datetime, date
import urllib3
import traceback
import json
import re
import glob
import time
import subprocess

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================================
# КОНФИГУРАЦИЯ
# Эти значения заменяются install.sh при установке.
# Для ручной установки укажите свои значения.
# ============================================================================

# URL родительской панели (включая admin proxy path)
# Пример: https://my.example.com/ADMIN_SECRET_PATH
PARENT_URL = "https://your-parent-panel.example.com/ADMIN_PATH"

# API ключ для доступа к parent панели
# Получите в: Admin Panel → Settings → API Keys
API_KEY = "your-api-key-here"

# URL ЛОКАЛЬНОЙ (child) панели для УДАЛЕНИЯ отсутствующих на parent юзеров.
# admin proxy-path идентичен parent -> деривируем из PARENT_URL. Удаление через Hiddify-API
# (а не прямой SQL) делает каскад БД (user_detail) + удаление из работающего Xray (gRPC RemoveUser).
CHILD_URL = "http://127.0.0.1:9000/" + PARENT_URL.rstrip("/").rsplit("/", 1)[-1]

# Конфигурация подключения к локальной базе данных MySQL
# Использует Unix socket аутентификацию (требует запуск от root)
DB_CONFIG = {
    'unix_socket': '/var/run/mysqld/mysqld.sock',
    'user': 'root',
    'database': 'hiddifypanel',
    'charset': 'utf8mb4'
}

# Минимальный объём трафика для отправки на parent (в байтах).
# Трафик ниже этого порога накапливается локально до следующего цикла.
# 1MB = 1000000 байт. Это предотвращает лишние API-запросы при малых объёмах.
MIN_TRAFFIC_THRESHOLD = 1000000

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def log(message):
    """Логирование с временной меткой. Вывод через stdout для systemd journald."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")
    sys.stdout.flush()


def get_db_connection():
    """Получить подключение к локальной базе данных MySQL."""
    try:
        connection = pymysql.connect(**DB_CONFIG)
        return connection
    except Exception as e:
        log(f"❌ Ошибка подключения к БД: {e}")
        return None


def fetch_parent_users():
    """
    Получает полный список пользователей с parent панели (один GET-запрос).
    Результат используется несколькими шагами синхронизации,
    чтобы не делать повторных запросов.

    Returns:
        list | None: Список пользователей или None при ошибке
    """
    try:
        response = requests.get(
            f"{PARENT_URL}/api/v2/admin/user/",
            headers={'Hiddify-API-Key': API_KEY},
            verify=False,
            timeout=60
        )
        if response.status_code != 200:
            log(f"❌ Ошибка получения пользователей с parent: HTTP {response.status_code}")
            return None
        parent_users = response.json()
        log(f"Получено {len(parent_users)} пользователей с parent панели")
        return parent_users
    except Exception as e:
        log(f"❌ Ошибка запроса списка пользователей с parent: {e}")
        return None


def parse_datetime(dt_str):
    """Парсит строку даты/времени из Hiddify API (формат: 'YYYY-MM-DD HH:MM:SS')."""
    if not dt_str:
        return None
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


# ============================================================================
# СИНХРОНИЗАЦИЯ ТРАФИКА (CHILD → PARENT)
#
# Алгоритм накопительной синхронизации:
# 1. Собираем локальный current_usage для пользователей > порога
# 2. GET текущий трафик с parent для каждого пользователя
# 3. new_usage = parent_usage + local_delta
# 4. PATCH на parent с new_usage
# 5. Обнуляем локальный current_usage (атомарная транзакция)
#
# Это гарантирует, что parent видит суммарный трафик со всех child-серверов.
# ============================================================================

def collect_local_usage_delta():
    """
    Собирает локальную статистику трафика для отправки на parent.
    Выбирает только пользователей с current_usage > MIN_TRAFFIC_THRESHOLD.
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("""
                SELECT uuid, name, current_usage, last_online
                FROM user
                WHERE current_usage > %s
            """, (MIN_TRAFFIC_THRESHOLD,))

            users_with_usage = cursor.fetchall()
            usage_deltas = []

            log(f"Найдено {len(users_with_usage)} пользователей с трафиком > {MIN_TRAFFIC_THRESHOLD/(1024**3):.3f}GB")

            for user in users_with_usage:
                usage_gb = user['current_usage'] / (1024**3)
                if usage_gb > 0.001:
                    usage_deltas.append({
                        'uuid': user['uuid'],
                        'usage_delta_GB': round(usage_gb, 6),
                        'last_online': user['last_online'].isoformat() if user['last_online'] else None,
                        'name': user['name']
                    })
                    log(f"  {user['name']}: +{usage_gb:.3f}GB")

        conn.close()
        return usage_deltas
    except Exception as e:
        log(f"❌ Ошибка сбора статистики: {e}")
        traceback.print_exc()
        return []


def get_parent_user_usage(uuid):
    """Получает текущий трафик пользователя с parent панели (в GB)."""
    try:
        response = requests.get(
            f"{PARENT_URL}/api/v2/admin/user/{uuid}/",
            headers={'Hiddify-API-Key': API_KEY},
            verify=False,
            timeout=30
        )
        if response.status_code == 200:
            return response.json().get('current_usage_GB', 0)
        elif response.status_code == 404:
            log(f"⚠️  Пользователь {uuid[:8]}... не найден на parent")
            return 0
        else:
            log(f"❌ Ошибка получения пользователя: HTTP {response.status_code}")
            return None
    except Exception as e:
        log(f"❌ Ошибка запроса к parent: {e}")
        return None


def update_parent_user_usage(uuid, new_usage_gb, name="Unknown"):
    """Обновляет суммарный трафик пользователя на parent панели через PATCH."""
    try:
        data = {"current_usage_GB": new_usage_gb}
        response = requests.patch(
            f"{PARENT_URL}/api/v2/admin/user/{uuid}/",
            headers={'Hiddify-API-Key': API_KEY},
            json=data,
            verify=False,
            timeout=30
        )
        if response.status_code == 200:
            log(f"✅ Обновлён трафик {name}: {new_usage_gb:.3f}GB")
            return True
        else:
            log(f"❌ Ошибка обновления {name}: HTTP {response.status_code} - {response.text[:200]}")
            return False
    except Exception as e:
        log(f"❌ Ошибка обновления пользователя {name}: {e}")
        return False


def send_usage_deltas_to_parent(usage_deltas):
    """
    Отправляет накопленную дельту трафика на parent панель.
    Для каждого пользователя: new_usage = parent_usage + local_delta.
    """
    if not usage_deltas:
        return True, 0

    log(f"Отправка дельта статистики для {len(usage_deltas)} пользователей...")

    successful_updates = 0
    for delta in usage_deltas:
        uuid = delta['uuid']
        local_delta = delta['usage_delta_GB']
        name = delta.get('name', 'Unknown')

        parent_usage = get_parent_user_usage(uuid)
        if parent_usage is None:
            continue

        new_usage = parent_usage + local_delta
        log(f"Пользователь {name}: parent={parent_usage:.3f}GB + local={local_delta:.3f}GB = {new_usage:.3f}GB")

        if update_parent_user_usage(uuid, new_usage, name):
            successful_updates += 1

    success_rate = successful_updates == len(usage_deltas)
    log(f"{'✅' if success_rate else '⚠️'} {'Полностью' if success_rate else 'Частично'} успешно: {successful_updates}/{len(usage_deltas)} пользователей обновлено")

    return success_rate, successful_updates


def reset_local_usage(usage_deltas):
    """
    Сбрасывает локальный current_usage в 0 после успешной отправки на parent.
    Это критично: без сброса трафик будет отправлен повторно.
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        with conn.cursor() as cursor:
            reset_count = 0
            for delta in usage_deltas:
                cursor.execute(
                    "UPDATE user SET current_usage = 0 WHERE uuid = %s AND current_usage > 0",
                    (delta['uuid'],)
                )
                if cursor.rowcount > 0:
                    reset_count += 1
                    log(f"Сброшен трафик для {delta['name']}: {delta['usage_delta_GB']:.3f}GB → 0GB")

            conn.commit()
            log(f"✅ Сброшен локальный трафик для {reset_count} пользователей")

        conn.close()
        return True
    except Exception as e:
        log(f"❌ Ошибка сброса локальной статистики: {e}")
        traceback.print_exc()
        return False


# ============================================================================
# СИНХРОНИЗАЦИЯ LAST_ONLINE (CHILD ↔ PARENT)
#
# Двунаправленная синхронизация: выигрывает более свежее значение.
# Это важно, т.к. пользователь может подключаться через разные child-серверы,
# а parent должен видеть самое актуальное время последнего подключения.
# ============================================================================

def sync_last_online(parent_users):
    """
    Двунаправленная синхронизация last_online между child и parent.

    Для каждого пользователя сравнивает временные метки:
      - Если local > parent → PATCH на parent (пользователь был активен здесь)
      - Если parent > local → UPDATE в локальной БД (был активен на другом child)
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        # Строим карту parent last_online {uuid: {last_online, name}}
        parent_online_map = {}
        for pu in parent_users:
            parent_online_map[pu['uuid']] = {
                'last_online': parse_datetime(pu.get('last_online')),
                'name': pu['name']
            }

        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT uuid, name, last_online FROM user")
            local_users = cursor.fetchall()

            pushed_count = 0
            pulled_count = 0

            for local_user in local_users:
                uuid = local_user['uuid']
                local_online = local_user['last_online']
                name = local_user['name']

                if uuid not in parent_online_map:
                    continue

                parent_online = parent_online_map[uuid]['last_online']

                if local_online and parent_online:
                    if local_online > parent_online:
                        if _push_last_online_to_parent(uuid, local_online, name):
                            pushed_count += 1
                    elif parent_online > local_online:
                        cursor.execute(
                            "UPDATE user SET last_online = %s WHERE uuid = %s",
                            (parent_online, uuid)
                        )
                        pulled_count += 1
                elif local_online and not parent_online:
                    if _push_last_online_to_parent(uuid, local_online, name):
                        pushed_count += 1
                elif parent_online and not local_online:
                    cursor.execute(
                        "UPDATE user SET last_online = %s WHERE uuid = %s",
                        (parent_online, uuid)
                    )
                    pulled_count += 1

            conn.commit()

            if pushed_count or pulled_count:
                log(f"✅ last_online: ↑{pushed_count} → parent, ↓{pulled_count} ← parent")
            else:
                log(f"last_online: всё актуально, обновлений не требуется")

        conn.close()
        return True
    except Exception as e:
        log(f"❌ Ошибка синхронизации last_online: {e}")
        traceback.print_exc()
        return False


def _push_last_online_to_parent(uuid, local_online, name):
    """Отправляет last_online одного пользователя на parent через PATCH."""
    try:
        data = {"last_online": local_online.strftime("%Y-%m-%d %H:%M:%S")}
        response = requests.patch(
            f"{PARENT_URL}/api/v2/admin/user/{uuid}/",
            headers={'Hiddify-API-Key': API_KEY},
            json=data,
            verify=False,
            timeout=30
        )
        if response.status_code == 200:
            return True
        else:
            log(f"  ⚠️ {name}: не удалось обновить last_online на parent: HTTP {response.status_code}")
            return False
    except Exception as e:
        log(f"  ⚠️ {name}: ошибка отправки last_online: {e}")
        return False


def _delete_missing_users(missing_uuids):
    """Удаляет юзеров, отсутствующих на parent, через ЛОКАЛЬНЫЙ admin-API child.
    Hiddify сам делает каскад БД (user_detail) + удаление из работающего Xray (gRPC RemoveUser).
    Раньше таких БЛОКИРОВАЛИ (enable=0) -> они висели на child и попадали в агрегацию подписки."""
    deleted = 0
    for uuid in missing_uuids:
        try:
            response = requests.delete(
                f"{CHILD_URL}/api/v2/admin/user/{uuid}/",
                # Без явного Host: локальный admin-API (127.0.0.1:9000) принимает
                # дефолтный Host запроса — механизм не привязан к конкретному домену.
                headers={'Hiddify-API-Key': API_KEY},
                verify=False,
                timeout=30
            )
            if response.status_code in (200, 204):
                deleted += 1
                log(f"🗑️  Удалён отсутствующий на parent: {uuid[:8]}…")
            else:
                log(f"  ⚠️ Не удалён {uuid[:8]}…: HTTP {response.status_code}")
        except Exception as e:
            log(f"  ⚠️ Ошибка удаления {uuid[:8]}…: {e}")
    return deleted


def _run_xray_helper(script_path, uuids, label):
    """
    Запускает helper активации/деактивации Xray (subprocess; venv-питон в shebang скрипта).
    Идемпотентные операции над работающим Xray по gRPC. uuids — список UUID.
    """
    if not uuids:
        return
    try:
        import subprocess
        result = subprocess.run(
            [script_path] + list(uuids),
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            summary = (result.stdout.strip().splitlines() or [''])[-1]
            log(f"✅ Xray {label}: {summary}")
        else:
            log(f"⚠️ Ошибка {label} в Xray: {result.stderr[:200]}")
    except Exception as e:
        log(f"⚠️ Сбой {label} в Xray: {e}")


# ============================================================================
# СИНХРОНИЗАЦИЯ ПОЛЬЗОВАТЕЛЕЙ (PARENT → CHILD) — v4.4 «нативный путь»
#
# Parent — единственный источник истины для списка пользователей, лимитов, статусов, ключей.
# Child управляет локально: current_usage (сбрасывается после отправки на parent), last_online.
#
# v4.4: создание/изменение/удаление выполняются через ЛОКАЛЬНЫЙ admin-API child
# (POST/PATCH/DELETE /api/v2/admin/user/), а НЕ raw-SQL. Панель Hiddify при этом делает ровно то,
# что делает при ручном добавлении юзера: gRPC hot-add в Xray (user_driver.add_client) и
# `install.sh apply_users` (jinja-перерендер конфигов из БД → SIGHUP-reload sing-box,
# пересборка WireGuard-пиров). Без второго шага sing-box (Hysteria2/TUIC/Mieru/Naive/SS) и
# WireGuard узнавали о новых юзерах только при ребуте сервера (инцидент 2026-11-02).
# PATCH делается только по реальному диффу полей → лишних apply_users не бывает.
# После батча — реконсиляция: rendered-конфиги движков сверяются с is_active из БД; при
# расхождении запускается один `install.sh apply_users` (самовосстановление каждый цикл).
# ============================================================================

HM_DIR = '/opt/hiddify-manager'
APPLY_LOCK_FILE = f'{HM_DIR}/log/0-install.lock'   # set_lock() в common/utils.sh; устаревает через 120 с
APPLY_LOCK_STALE_SEC = 120
APPLY_WAIT_SEC = 240            # сколько ждём завершения apply_users, запущенного панелью
APPLY_TIMEOUT_SEC = 300
APPLY_LOG_FILE = f'{HM_DIR}/log/system/sync-apply-users.log'
WG_IFACE = 'hiddifywg'
DRY_RUN = '--dry-run' in sys.argv  # только показать планируемые операции, ничего не менять
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
_NO_PROXY = {'http': None, 'https': None}   # локальный API — всегда напрямую, мимо любых прокси


def _child_api(method, path, json_body=None, timeout=30):
    """Запрос к локальному admin-API child (127.0.0.1:9000)."""
    return requests.request(
        method, f"{CHILD_URL}/api/v2/admin/{path}",
        headers={'Hiddify-API-Key': API_KEY}, json=json_body,
        timeout=timeout, proxies=_NO_PROXY, verify=False)


def fetch_child_users():
    """Все локальные юзеры через API (та же схема полей, что у parent) → dict по uuid; None при ошибке."""
    try:
        r = _child_api('GET', 'user/', timeout=60)
        if r.status_code == 404:          # «You have no user»
            return {}
        if r.status_code != 200:
            log(f"❌ Локальный API: список юзеров HTTP {r.status_code}")
            return None
        return {u['uuid']: u for u in r.json()}
    except Exception as e:
        log(f"❌ Локальный API недоступен: {e}")
        return None


def _norm_date(v):
    return str(v)[:10] if v else None


def _desired_fields(parent_user):
    """Поля, которые child должен иметь (из ответа parent). enable child = is_active parent
    (учитывает блокировку, исчерпание трафика и истечение срока — рвёт доступ на child)."""
    is_active = bool(parent_user.get('is_active', parent_user.get('enable')))
    d = {
        'name': parent_user.get('name') or '',
        'usage_limit_GB': float(parent_user.get('usage_limit_GB') or 0),
        'package_days': int(parent_user.get('package_days') or 0),
        'mode': parent_user.get('mode') or 'no_reset',
        'enable': is_active,
        'comment': parent_user.get('comment') or '',
        'telegram_id': parent_user.get('telegram_id') or None,
        'start_date': _norm_date(parent_user.get('start_date')),
    }
    # Ключи: слать только непустые ('' затёрло бы сгенерированные панелью ключи).
    for k in ('ed25519_private_key', 'ed25519_public_key', 'wg_pk', 'wg_pub', 'wg_psk'):
        if parent_user.get(k):
            d[k] = parent_user[k]
    if parent_user.get('lang'):
        d['lang'] = parent_user['lang']
    return d


def _changed_fields(desired, child_user):
    """Только реально отличающиеся поля → тело PATCH (пусто = ничего не делать)."""
    body = {}
    for k, v in desired.items():
        c = child_user.get(k)
        if k == 'usage_limit_GB':
            if abs(float(c or 0) - v) > 1e-6:
                body[k] = v
        elif k == 'comment':
            if (c or '') != v:
                body[k] = v
        elif k == 'telegram_id':
            if (c or None) != v:
                body[k] = v
        elif k == 'start_date':
            # Hiddify: start_date=None означает «пакет ещё не начат», панель сама ставит дату
            # первого подключения. Parent (не видит трафик напрямую) держит None, child — дату;
            # навязывать None каждый цикл = flip-flop с задачей учёта → лишние apply_users.
            # Поэтому: реальную дату parent'а синхронизируем всегда; None — только если локально
            # юзер уже ИСТЁК (is_active=False при enable=True), а на parent активен → перевзвести.
            c_sd = _norm_date(c)
            if v is not None:
                if c_sd != v:
                    body[k] = v
            elif c_sd is not None and desired['enable'] and child_user.get('enable') \
                    and not child_user.get('is_active'):
                body[k] = None
        elif k == 'enable':
            if bool(c) != v:
                body[k] = v
        elif k == 'lang':
            if c and c != v:          # пустой локальный lang = дефолт панели, не трогаем
                body[k] = v
        else:
            if (c if c is not None else '') != v:
                body[k] = v
    # User.add_or_update применяет start_date только вместе с package_days, ed25519 — только парой.
    if 'start_date' in body or 'package_days' in body:
        body['package_days'] = desired['package_days']
        body['start_date'] = desired['start_date']
    if 'ed25519_private_key' in body or 'ed25519_public_key' in body:
        for k in ('ed25519_private_key', 'ed25519_public_key'):
            if k in desired:
                body[k] = desired[k]
    return body


def _api_create_user(uuid, desired):
    body = {k: v for k, v in desired.items() if v is not None}
    body.update({'uuid': uuid, 'current_usage_GB': 0})
    r = _child_api('POST', 'user/', body)
    if r.status_code == 400 and 'exists' in r.text:     # гонка/рассинхрон списка → обновляем
        body.pop('current_usage_GB', None)
        r = _child_api('PATCH', f'user/{uuid}/', body)
    return r


def _sql_fixups(parent_users, created_uuids):
    """Поля, которые admin-API 12.x игнорирует и которые НЕ влияют на рендер конфигов:
    last_reset_time (из parent) и max_ips=2 для новых. Пишем только при реальном отличии."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT uuid, last_reset_time, max_ips FROM user")
            local = {r['uuid']: r for r in cursor.fetchall()}
            fixed = 0
            for pu in parent_users:
                row = local.get(pu['uuid'])
                if not row:
                    continue
                lrt = pu.get('last_reset_time')
                if lrt and str(row['last_reset_time'] or '')[:10] != str(lrt)[:10]:   # колонка DATE
                    if not DRY_RUN:
                        cursor.execute("UPDATE user SET last_reset_time=%s WHERE uuid=%s", (lrt, pu['uuid']))
                    fixed += 1
                if pu['uuid'] in created_uuids and row['max_ips'] != 2:
                    if not DRY_RUN:
                        cursor.execute("UPDATE user SET max_ips=2 WHERE uuid=%s", (pu['uuid'],))
            conn.commit()
            if fixed:
                log(f"🛠 last_reset_time выровнен у {fixed} юзеров (SQL, без влияния на конфиги)")
    except Exception as e:
        log(f"⚠️ SQL fixups: {e}")
    finally:
        conn.close()


def sync_users_from_parent(parent_users):
    """
    Полная синхронизация пользователей с parent панели через локальный admin-API.
      - нет на child → POST (панель: add_client в Xray + apply_users);
      - есть, но поля отличаются → PATCH только отличающихся полей;
      - нет на parent → DELETE (с safeguard от массового удаления при сбое parent);
      - в конце → реконсиляция rendered-конфигов движков с БД (ensure_engines_applied).
    """
    try:
        child_users = fetch_child_users()
        if child_users is None:
            return False
        parent_uuids = {u['uuid'] for u in parent_users}

        created = patched = blocked = unblocked = errors = 0
        created_uuids = []
        activate_uuids = []   # созданные-активные + разблокированные → страховочный hot-add в Xray
        inactive_uuids = []   # все is_active=False → страховочное удаление из Xray (идемпотентно)

        for pu in parent_users:
            uuid = pu['uuid']
            desired = _desired_fields(pu)
            if not desired['enable']:
                inactive_uuids.append(uuid)
            cu = child_users.get(uuid)

            if cu is None:
                if DRY_RUN:
                    log(f"[dry-run] ➕ POST {pu['name']}")
                    created += 1
                    continue
                r = _api_create_user(uuid, desired)
                if r.ok:
                    created += 1
                    created_uuids.append(uuid)
                    if desired['enable']:
                        activate_uuids.append(uuid)
                    log(f"➕ Создан новый пользователь: {pu['name']}")
                else:
                    errors += 1
                    log(f"❌ Создание {pu['name']}: HTTP {r.status_code} {r.text[:160]}")
                continue

            body = _changed_fields(desired, cu)
            if not body:
                continue
            if DRY_RUN:
                log(f"[dry-run] ✏️ PATCH {pu['name']}: {sorted(body)}")
                patched += 1
                continue
            r = _child_api('PATCH', f'user/{uuid}/', body)
            if r.ok:
                patched += 1
                if 'enable' in body:
                    if body['enable']:
                        unblocked += 1
                        activate_uuids.append(uuid)
                        log(f"✅ Разблокирован: {pu['name']}")
                    else:
                        blocked += 1
                        log(f"🚫 Заблокирован: {pu['name']}")
                log(f"✏️ Обновлён {pu['name']}: {sorted(body)}")
            else:
                errors += 1
                log(f"❌ Обновление {pu['name']}: HTTP {r.status_code} {r.text[:160]}")

        # Отсутствующих на parent — УДАЛЯЕМ через admin-API (каскад БД + Xray + apply_users).
        # SAFEGUARD от mass-delete при сбое fetch_parent_users (пустой/частичный список):
        # не удаляем, если parent пуст ИЛИ к удалению >25% локальных (>10 шт).
        missing_uuids = list(set(child_users) - parent_uuids)
        if not parent_uuids or len(missing_uuids) > max(10, int(len(child_users) * 0.25)):
            log(f"⚠️ Удаление ПРОПУЩЕНО (safeguard): missing={len(missing_uuids)}, "
                f"parent={len(parent_uuids)}, local={len(child_users)} — подозрение на сбой fetch")
            missing_uuids = []
        deleted = 0
        if missing_uuids:
            if DRY_RUN:
                log(f"[dry-run] 🗑️ DELETE {len(missing_uuids)} юзеров")
            else:
                deleted = _delete_missing_users(missing_uuids)
                if deleted:
                    log(f"🗑️  Удалено отсутствующих на parent: {deleted}")

        _sql_fixups(parent_users, created_uuids)

        log(f"✅ Синхронизация: {len(parent_users)} проверено, {created} создано, {patched} обновлено, "
            f"{deleted} удалено, {blocked} заблок, {unblocked} разблок, {errors} ошибок"
            + (" [dry-run]" if DRY_RUN else ""))

        if DRY_RUN:
            return errors == 0

        # Страховка поверх нативного hot-add панели (идемпотентно: EmailAlreadyExists = no-op).
        _run_xray_helper('/opt/hiddify-manager/deactivate_users_direct.py', inactive_uuids, 'деактивация')
        _run_xray_helper('/opt/hiddify-manager/activate_new_users_direct.py', activate_uuids, 'активация')

        # Реконсиляция движков с БД (ждём apply_users панели, при расхождении — свой apply_users).
        ensure_engines_applied(changed=bool(created or patched or deleted))
        return errors == 0

    except Exception as e:
        log(f"❌ Ошибка синхронизации пользователей: {e}")
        traceback.print_exc()
        return False


# ============================================================================
# РЕКОНСИЛЯЦИЯ ДВИЖКОВ С БД (rendered-конфиги == is_active)
# ============================================================================

def _wait_apply_lock(max_wait=APPLY_WAIT_SEC):
    """Ждём, пока завершится apply_users, запущенный панелью (lock-файл install.sh)."""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            st = os.stat(APPLY_LOCK_FILE)
        except FileNotFoundError:
            return True
        try:
            ts = int(open(APPLY_LOCK_FILE).read().strip() or 0)
        except Exception:
            ts = int(st.st_mtime)
        if time.time() - ts > APPLY_LOCK_STALE_SEC:   # устаревший lock install.sh игнорирует сам
            return True
        time.sleep(2)
    return False


def _rendered_uuid_sets():
    """uuid'ы в rendered-конфигах: Xray (clients[].id) и sing-box (users[]). None = не прочитать."""
    xray, sb = set(), set()
    ok_x = ok_s = False
    try:
        d = json.load(open(f'{HM_DIR}/xray/configs/05_inbounds_new.json'))
        for ib in d.get('inbounds', []):
            for c in ib.get('settings', {}).get('clients', []) or []:
                v = str(c.get('id', ''))
                if _UUID_RE.match(v):
                    xray.add(v.lower())
        ok_x = True
    except Exception as e:
        log(f"⚠️ rendered xray не прочитан: {e}")
    for p in glob.glob(f'{HM_DIR}/singbox/configs/05_inbounds_*.json'):
        try:
            d = json.load(open(p))
            ibs = d.get('inbounds', d) if isinstance(d, dict) else d
            for ib in ibs or []:
                if not isinstance(ib, dict):
                    continue
                for u in ib.get('users') or []:
                    for k in ('uuid', 'password', 'name', 'username'):
                        v = str(u.get(k, ''))
                        if _UUID_RE.match(v):
                            sb.add(v.lower())
            ok_s = True
        except Exception:
            pass
    return (xray if ok_x else None), (sb if ok_s else None)


def _run_apply_users():
    """Нативный `install.sh apply_users --no-gui --no-log` (рендер из БД + reload sing-box + WG)."""
    t0 = time.time()
    try:
        os.makedirs(os.path.dirname(APPLY_LOG_FILE), exist_ok=True)
        with open(APPLY_LOG_FILE, 'a') as lf:
            lf.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} stable_sync → install.sh apply_users =====\n")
            lf.flush()
            p = subprocess.run(['/bin/bash', f'{HM_DIR}/install.sh', 'apply_users', '--no-gui', '--no-log'],
                               cwd=HM_DIR, stdout=lf, stderr=subprocess.STDOUT, timeout=APPLY_TIMEOUT_SEC,
                               env={**os.environ, 'HIDDIFY_CONFIG_PATH': HM_DIR})
    except subprocess.TimeoutExpired:
        log(f"❌ apply_users не завершился за {APPLY_TIMEOUT_SEC} с, см. {APPLY_LOG_FILE}")
        return 1
    except Exception as e:
        log(f"❌ apply_users не запущен: {e}")
        return 1
    if p.returncode == 12:
        log("⚠️ apply_users: параллельный apply панели (lock) — повторим в следующем цикле")
    elif p.returncode != 0:
        log(f"❌ apply_users rc={p.returncode}, см. {APPLY_LOG_FILE}")
    else:
        log(f"✅ install.sh apply_users выполнен за {time.time() - t0:.1f} с")
    return p.returncode


def _drift(expected, xray_set, sb_set, known=None):
    """known — все uuid юзеров child (активные и нет). «Лишними» считаются только известные БД,
    но неактивные юзеры: статические идентификаторы inbound'ов в формате UUID (например, общий
    пароль ShadowTLS v3 в 05_inbounds_*_shadowtls.json) юзерами не являются и не должны
    каждый цикл запускать apply_users (v4.4.1)."""
    out = []
    for name, s in (('xray', xray_set), ('sing-box', sb_set)):
        if s is None:
            continue
        miss = expected - s
        extra = (s - expected) if known is None else ((s & known) - expected)
        if miss or extra:
            out.append(f"{name}: нет {len(miss)}, лишних {len(extra)}")
    return out


def ensure_engines_applied(changed):
    """rendered-конфиги движков должны содержать ровно is_active-юзеров из БД.
    Расхождение (напр. панельный apply_users ещё не отработал/пропущен по lock, или юзер
    отключён) → один `install.sh apply_users`. WireGuard-пиры — информативно."""
    try:
        if not _wait_apply_lock():
            log(f"⚠️ apply_users панели не завершился за {APPLY_WAIT_SEC} с — реконсиляция отложена")
            return
        child = fetch_child_users()
        if child is None:
            return
        expected = {u.lower() for u, v in child.items() if v.get('is_active')}
        known = {u.lower() for u in child}
        drift = _drift(expected, *_rendered_uuid_sets(), known=known)
        if drift:
            log(f"🔧 Расхождение rendered↔БД ({'; '.join(drift)}) → install.sh apply_users")
            if _run_apply_users() == 0:
                drift = _drift(expected, *_rendered_uuid_sets(), known=known)
                log("✅ Движки согласованы с БД после apply_users" if not drift
                    else f"❌ Расхождение сохраняется: {'; '.join(drift)}")
        else:
            log("✅ Движки согласованы с БД (rendered == is_active)"
                + (", apply_users панели отработал" if changed else ""))
        try:
            peers = set(subprocess.check_output(['wg', 'show', WG_IFACE, 'peers'], text=True,
                                                stderr=subprocess.DEVNULL).split())
            exp_wg = {child[u]['wg_pub'] for u in child if child[u].get('is_active') and child[u].get('wg_pub')}
            if peers != exp_wg:
                log(f"⚠️ WireGuard-пиры: в ядре {len(peers)}, ожидается {len(exp_wg)} "
                    f"(нет {len(exp_wg - peers)}, лишних {len(peers - exp_wg)})")
        except Exception:
            pass
    except Exception as e:
        log(f"⚠️ Реконсиляция движков: {e}")


# ============================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================================

def main():
    """
    Главная функция накопительной синхронизации v4.4.

    Последовательность операций:
      1. Получаем список пользователей с parent (один GET-запрос)
      2. Собираем локальную дельту трафика
      3. Отправляем дельту на parent (накопительно)
      4. Сбрасываем локальный трафик в 0
      5. Синхронизируем last_online (двунаправленно)
      6. Синхронизируем пользователей (parent → child)
    """
    log("=== ⚙ Starting Stable Accumulative Sync v4.4.1 ===" + (" [DRY-RUN]" if DRY_RUN else ""))

    try:
        # Шаг 1: Получаем пользователей с parent (один раз для всех шагов)
        log("Step 1: Получаем список пользователей с parent...")
        parent_users = fetch_parent_users()
        if parent_users is None:
            log("❌ Невозможно продолжить без данных с parent")
            return False
        if DRY_RUN:   # только план операций по юзерам; трафик/last_online не трогаем
            return sync_users_from_parent(parent_users)

        # Шаг 2: Собираем локальную статистику
        log("Step 2: Собираем локальную дельта статистику...")
        usage_deltas = collect_local_usage_delta()
        log(f"Собрано дельта статистики для {len(usage_deltas)} пользователей")

        # Шаг 3-4: Отправляем на parent и сбрасываем локально
        if usage_deltas:
            log("Step 3: Отправляем дельта статистику на parent...")
            success, updated_count = send_usage_deltas_to_parent(usage_deltas)

            if success:
                log("Step 4: Сбрасываем локальную статистику в ноль...")
                if reset_local_usage(usage_deltas):
                    log(f"✅ Локальная статистика сброшена для {len(usage_deltas)} пользователей")
        else:
            log("Step 3: Нет дельта статистики для отправки")

        # Шаг 5: Двунаправленная синхронизация last_online
        log("Step 5: Синхронизация last_online (child ↔ parent)...")
        if sync_last_online(parent_users):
            log("✅ Синхронизация last_online завершена")
        else:
            log("⚠️ Ошибка синхронизации last_online")

        # Шаг 6: Полная синхронизация пользователей
        log("Step 6: Полная синхронизация пользователей с parent...")
        if sync_users_from_parent(parent_users):
            log("✅ Синхронизация пользователей завершена успешно")
        else:
            log("❌ Ошибка синхронизации пользователей")

        log("✅ Stable sync completed successfully!")
        return True

    except Exception as e:
        log(f"❌ Критическая ошибка в синхронизации: {e}")
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
