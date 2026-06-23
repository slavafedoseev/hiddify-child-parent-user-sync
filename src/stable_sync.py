#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hiddify Manager Child-Parent User Synchronization v4.2

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
  - Мгновенная активация новых пользователей в Xray (без перезапуска)

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


# ============================================================================
# СИНХРОНИЗАЦИЯ ПОЛЬЗОВАТЕЛЕЙ (PARENT → CHILD)
#
# Parent — единственный источник истины для:
#   - Списка пользователей (создание/блокировка)
#   - Лимитов, пакетов, статусов, ключей
#
# Child управляет локально:
#   - current_usage (сбрасывается после отправки на parent)
#   - last_online (синхронизируется отдельно в sync_last_online)
# ============================================================================

def sync_users_from_parent(parent_users):
    """
    Полная синхронизация пользователей с parent панели.

    Создаёт новых, обновляет существующих, блокирует отсутствующих.
    Новые пользователи мгновенно активируются в Xray через activate_new_users_direct.py.

    Args:
        parent_users: Список пользователей с parent (из fetch_parent_users)
    """
    try:
        conn = get_db_connection()
        if not conn:
            return False

        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT uuid, name, enable FROM user")
            local_users = cursor.fetchall()
            local_uuids = {u['uuid'] for u in local_users}
            parent_uuids = {u['uuid'] for u in parent_users}

            synced_count = 0
            blocked_count = 0
            unblocked_count = 0
            created_count = 0
            created_uuids = []

            for parent_user in parent_users:
                uuid = parent_user['uuid']

                cursor.execute("SELECT id, enable FROM user WHERE uuid = %s", (uuid,))
                existing_user = cursor.fetchone()

                if not existing_user:
                    # Создаём нового пользователя с пустыми username/password
                    # (Hiddify использует UUID-авторизацию, НЕ генерируйте password!)
                    cursor.execute("""
                        INSERT INTO user (
                            uuid, name, usage_limit, package_days, mode, enable,
                            comment, start_date, last_reset_time, current_usage,
                            telegram_id, ed25519_private_key, ed25519_public_key,
                            wg_pk, wg_psk, wg_pub, added_by, last_online, max_ips,
                            username, password
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s, %s, NOW(), 2, '', '')
                    """, (
                        uuid, parent_user['name'], int(parent_user['usage_limit_GB'] * 1024**3),
                        parent_user['package_days'], parent_user['mode'], parent_user['enable'],
                        parent_user.get('comment', ''), parent_user.get('start_date'),
                        parent_user.get('last_reset_time'), parent_user.get('telegram_id'),
                        parent_user.get('ed25519_private_key', ''), parent_user.get('ed25519_public_key', ''),
                        parent_user.get('wg_pk', ''), parent_user.get('wg_psk', ''),
                        parent_user.get('wg_pub', ''), 1
                    ))
                    created_count += 1
                    created_uuids.append(uuid)
                    log(f"➕ Создан новый пользователь: {parent_user['name']}")
                else:
                    # Обновляем все поля кроме current_usage и last_online
                    cursor.execute("""
                        UPDATE user SET
                            name = %s, usage_limit = %s, package_days = %s,
                            mode = %s, enable = %s, comment = %s, start_date = %s,
                            last_reset_time = %s, telegram_id = %s,
                            ed25519_private_key = %s, ed25519_public_key = %s,
                            wg_pk = %s, wg_psk = %s, wg_pub = %s, added_by = %s
                        WHERE uuid = %s
                    """, (
                        parent_user['name'], int(parent_user['usage_limit_GB'] * 1024**3), parent_user['package_days'],
                        parent_user['mode'], parent_user['enable'], parent_user.get('comment', ''),
                        parent_user.get('start_date'), parent_user.get('last_reset_time'),
                        parent_user.get('telegram_id'),
                        parent_user.get('ed25519_private_key', ''), parent_user.get('ed25519_public_key', ''),
                        parent_user.get('wg_pk', ''), parent_user.get('wg_psk', ''),
                        parent_user.get('wg_pub', ''), 1,
                        uuid
                    ))

                    if existing_user['enable'] != parent_user['enable']:
                        if parent_user['enable']:
                            unblocked_count += 1
                            log(f"✅ Разблокирован: {parent_user['name']}")
                        else:
                            blocked_count += 1
                            log(f"🚫 Заблокирован: {parent_user['name']}")

                synced_count += 1

            # Отсутствующих на parent — НЕ блокируем (раньше висели как disabled и попадали в
            # агрегацию подписки), а УДАЛЯЕМ после коммита через admin-API child.
            # SAFEGUARD: защита от mass-delete при сбое fetch_parent_users (пустой/частичный список):
            # не удаляем, если parent пуст ИЛИ к удалению >25% локальных (>10 шт).
            missing_uuids = list(local_uuids - parent_uuids)
            if not parent_uuids or len(missing_uuids) > max(10, int(len(local_uuids) * 0.25)):
                log(f"⚠️ Удаление ПРОПУЩЕНО (safeguard): missing={len(missing_uuids)}, "
                    f"parent={len(parent_uuids)}, local={len(local_uuids)} — подозрение на сбой fetch")
                missing_uuids = []

            conn.commit()
            log(f"✅ Синхронизация: {synced_count} синхр, {created_count} создано, "
                f"{blocked_count} заблок, {unblocked_count} разблок")

        conn.close()

        # Удаление отсутствующих на parent через admin-API child (каскад БД + Xray), ВНЕ транзакции
        deleted_count = _delete_missing_users(missing_uuids)
        if deleted_count:
            log(f"🗑️  Удалено отсутствующих на parent: {deleted_count}")

        # Мгновенная активация новых пользователей в работающем Xray
        if created_uuids:
            log(f"🔧 Активация {len(created_uuids)} новых пользователей в прокси...")
            try:
                import subprocess
                result = subprocess.run(
                    ['/opt/hiddify-manager/activate_new_users_direct.py'] + created_uuids,
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                if result.returncode == 0:
                    log(f"✅ Новые пользователи активированы в прокси")
                else:
                    log(f"⚠️ Не удалось активировать пользователей: {result.stderr}")
            except Exception as e:
                log(f"⚠️ Ошибка активации новых пользователей: {e}")

        return True

    except Exception as e:
        log(f"❌ Ошибка синхронизации пользователей: {e}")
        traceback.print_exc()
        return False


# ============================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================================

def main():
    """
    Главная функция накопительной синхронизации v4.2.

    Последовательность операций:
      1. Получаем список пользователей с parent (один GET-запрос)
      2. Собираем локальную дельту трафика
      3. Отправляем дельту на parent (накопительно)
      4. Сбрасываем локальный трафик в 0
      5. Синхронизируем last_online (двунаправленно)
      6. Синхронизируем пользователей (parent → child)
    """
    log("=== ⚙ Starting Stable Accumulative Sync v4.2 ===")

    try:
        # Шаг 1: Получаем пользователей с parent (один раз для всех шагов)
        log("Step 1: Получаем список пользователей с parent...")
        parent_users = fetch_parent_users()
        if parent_users is None:
            log("❌ Невозможно продолжить без данных с parent")
            return False

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
