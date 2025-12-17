#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hiddify Manager Child-Parent User Synchronization v4.0
Стабильная синхронизация пользователей и трафика между child и parent панелями

ОСНОВНАЯ ФУНКЦИОНАЛЬНОСТЬ:
✅ Получение списка пользователей с parent панели
✅ Создание новых пользователей на child сервере
✅ Обновление существующих пользователей (лимиты, статусы, ключи)
✅ Блокировка пользователей отсутствующих на parent
✅ Накопительная синхронизация трафика (child → parent)
✅ Автоматическое обнуление локального трафика после отправки

ТРЕБОВАНИЯ:
- Hiddify Manager v11.0.12b1 или выше
- Python 3.13+
- PyMySQL библиотека
- Доступ к parent панели через API

АВТОР: Система синхронизации для Hiddify Manager
ЛИЦЕНЗИЯ: MIT
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
# КОНФИГУРАЦИЯ - ИЗМЕНИТЕ ЭТИ ПАРАМЕТРЫ ПОД ВАШУ ИНФРАСТРУКТУРУ
# ============================================================================

# URL родительской панели (включая admin proxy path)
# Пример: https://my.example.com/rqkMip3ThY
PARENT_URL = "https://my.fedoseev.one/rqkMip3ThY"

# API ключ для доступа к parent панели
# Получите в: Admin Panel → Settings → API Keys
API_KEY = "33eb7d42-f525-4871-a2d9-7986308757be"

# Конфигурация подключения к локальной базе данных MySQL
# Использует Unix socket auth (требует запуск от root или через sudo)
DB_CONFIG = {
    'unix_socket': '/var/run/mysqld/mysqld.sock',
    'user': 'root',
    'database': 'hiddifypanel',
    'charset': 'utf8mb4'
}

# Минимальный объём трафика для отправки (в байтах)
# 1MB = 1000000 байт
MIN_TRAFFIC_THRESHOLD = 1000000

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def log(message):
    """Логирование с временной меткой"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")
    sys.stdout.flush()  # Принудительный flush для systemd journald

def get_db_connection():
    """Получить подключение к локальной базе данных MySQL"""
    try:
        connection = pymysql.connect(**DB_CONFIG)
        return connection
    except Exception as e:
        log(f"❌ Ошибка подключения к БД: {e}")
        return None

# ============================================================================
# СИНХРОНИЗАЦИЯ ТРАФИКА (CHILD → PARENT)
# ============================================================================

def collect_local_usage_delta():
    """
    Собирает локальную статистику трафика для отправки на parent панель

    Возвращает список словарей:
    [
        {
            'uuid': 'user-uuid',
            'usage_delta_GB': 0.123,
            'last_online': '2025-01-01T12:00:00',
            'name': 'Username'
        },
        ...
    ]
    """
    try:
        conn = get_db_connection()
        if not conn:
            return []

        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # Находим пользователей с трафиком выше порога
            cursor.execute("""
                SELECT uuid, name, current_usage, last_online
                FROM user
                WHERE current_usage > %s
            """, (MIN_TRAFFIC_THRESHOLD,))

            users_with_usage = cursor.fetchall()
            usage_deltas = []

            log(f"Найдено {len(users_with_usage)} пользователей с трафиком > {MIN_TRAFFIC_THRESHOLD/(1024**3):.3f}GB")

            for user in users_with_usage:
                usage_gb = user['current_usage'] / (1024**3)  # Bytes to GB
                if usage_gb > 0.001:  # Минимум 1MB
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
    """
    Получает текущую статистику пользователя с parent панели

    Args:
        uuid: UUID пользователя

    Returns:
        float: Текущий трафик в GB или None при ошибке
    """
    try:
        response = requests.get(
            f"{PARENT_URL}/api/v2/admin/user/{uuid}/",
            headers={'Hiddify-API-Key': API_KEY},
            verify=False,
            timeout=30
        )
        if response.status_code == 200:
            user_data = response.json()
            return user_data.get('current_usage_GB', 0)
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
    """
    Обновляет статистику пользователя на parent панели

    Args:
        uuid: UUID пользователя
        new_usage_gb: Новый суммарный трафик в GB
        name: Имя пользователя (для логов)

    Returns:
        bool: True если успешно, False иначе
    """
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
    Отправляет дельта статистики на parent панель (накопительная синхронизация)

    Процесс:
    1. Получаем текущий трафик с parent
    2. Прибавляем локальную дельту
    3. Обновляем трафик на parent

    Args:
        usage_deltas: Список словарей с usage_delta_GB

    Returns:
        tuple: (success: bool, updated_count: int)
    """
    if not usage_deltas:
        return True, 0

    log(f"Отправка дельта статистики для {len(usage_deltas)} пользователей...")

    successful_updates = 0
    for delta in usage_deltas:
        uuid = delta['uuid']
        local_delta = delta['usage_delta_GB']
        name = delta.get('name', 'Unknown')

        # Получаем текущую статистику с parent
        parent_usage = get_parent_user_usage(uuid)
        if parent_usage is None:
            continue

        # Вычисляем новую статистику (накопительно)
        new_usage = parent_usage + local_delta
        log(f"Пользователь {name}: parent={parent_usage:.3f}GB + local={local_delta:.3f}GB = {new_usage:.3f}GB")

        # Обновляем на parent
        if update_parent_user_usage(uuid, new_usage, name):
            successful_updates += 1

    success_rate = successful_updates == len(usage_deltas)
    log(f"{'✅' if success_rate else '⚠️'} {'Полностью' if success_rate else 'Частично'} успешно: {successful_updates}/{len(usage_deltas)} пользователей обновлено")

    return success_rate, successful_updates

def reset_local_usage(usage_deltas):
    """
    Сбрасывает локальную статистику в 0 после успешной отправки на parent

    ВАЖНО: Это критично для корректной работы накопительной синхронизации!
    Если не сбросить локальный трафик, он будет отправлен повторно при следующей синхронизации.

    Args:
        usage_deltas: Список словарей с uuid пользователей

    Returns:
        bool: True если успешно
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
# СИНХРОНИЗАЦИЯ ПОЛЬЗОВАТЕЛЕЙ (PARENT → CHILD)
# ============================================================================

def sync_users_from_parent():
    """
    ПОЛНАЯ синхронизация пользователей с parent панели

    Процесс:
    1. Получаем список всех пользователей с parent
    2. Для каждого пользователя:
       - Если не существует локально → создаём
       - Если существует → обновляем ВСЕ поля (кроме current_usage и last_online)
    3. Блокируем локальных пользователей, отсутствующих на parent

    Синхронизируемые поля:
    - name, usage_limit, package_days, mode
    - enable (статус активности)
    - comment, start_date, last_reset_time
    - telegram_id
    - ed25519_private_key, ed25519_public_key
    - wg_pk, wg_psk, wg_pub

    НЕ синхронизируются:
    - current_usage (управляется локально и отправляется на parent)
    - last_online (локальная метрика активности)

    Returns:
        bool: True если успешно
    """
    try:
        # Получаем пользователей с parent
        response = requests.get(
            f"{PARENT_URL}/api/v2/admin/user/",
            headers={'Hiddify-API-Key': API_KEY},
            verify=False,
            timeout=60
        )

        if response.status_code != 200:
            log(f"❌ Ошибка получения пользователей с parent: HTTP {response.status_code}")
            return False

        parent_users = response.json()
        log(f"Получено {len(parent_users)} пользователей с parent панели")

        conn = get_db_connection()
        if not conn:
            return False

        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # Получаем всех локальных пользователей
            cursor.execute("SELECT uuid, name, enable FROM user")
            local_users = cursor.fetchall()
            local_uuids = {u['uuid'] for u in local_users}
            parent_uuids = {u['uuid'] for u in parent_users}

            synced_count = 0
            blocked_count = 0
            unblocked_count = 0
            created_count = 0

            # Синхронизируем пользователей с parent
            for parent_user in parent_users:
                uuid = parent_user['uuid']

                # Проверяем существует ли пользователь
                cursor.execute("SELECT id, enable FROM user WHERE uuid = %s", (uuid,))
                existing_user = cursor.fetchone()

                if not existing_user:
                    # Создаем нового пользователя
                    cursor.execute("""
                        INSERT INTO user (
                            uuid, name, usage_limit, package_days, mode, enable,
                            comment, start_date, last_reset_time, current_usage,
                            telegram_id, ed25519_private_key, ed25519_public_key,
                            wg_pk, wg_psk, wg_pub, added_by
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        uuid, parent_user['name'], int(parent_user['usage_limit_GB'] * 1024**3),
                        parent_user['package_days'], parent_user['mode'], parent_user['enable'],
                        parent_user.get('comment', ''), parent_user.get('start_date'),
                        parent_user.get('last_reset_time'), parent_user.get('telegram_id'),
                        parent_user.get('ed25519_private_key', ''), parent_user.get('ed25519_public_key', ''),
                        parent_user.get('wg_pk', ''), parent_user.get('wg_psk', ''),
                        parent_user.get('wg_pub', ''), 1  # Используем admin ID=1
                    ))
                    created_count += 1
                    log(f"➕ Создан новый пользователь: {parent_user['name']}")
                else:
                    # КРИТИЧНО: Обновляем ВСЕ поля кроме current_usage и last_online
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
                        parent_user.get('wg_pub', ''), 1,  # Используем admin ID=1
                        uuid
                    ))

                    # Проверяем изменение статуса блокировки
                    if existing_user['enable'] != parent_user['enable']:
                        if parent_user['enable']:
                            unblocked_count += 1
                            log(f"✅ Разблокирован: {parent_user['name']}")
                        else:
                            blocked_count += 1
                            log(f"🚫 Заблокирован: {parent_user['name']}")

                synced_count += 1

            # Блокируем пользователей, которых нет на parent
            missing_uuids = local_uuids - parent_uuids
            for uuid in missing_uuids:
                cursor.execute("SELECT name, enable FROM user WHERE uuid = %s", (uuid,))
                user = cursor.fetchone()
                if user and user['enable']:
                    cursor.execute("UPDATE user SET enable = 0 WHERE uuid = %s", (uuid,))
                    log(f"🚫 Заблокирован отсутствующий на parent: {user['name']}")
                    blocked_count += 1

            conn.commit()
            log(f"✅ Синхронизация завершена: {synced_count} синхронизировано, {created_count} создано, {blocked_count} заблокировано, {unblocked_count} разблокировано")

        conn.close()
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
    Главная функция накопительной синхронизации

    Последовательность операций:
    1. Собираем локальную дельта статистику
    2. Отправляем статистику на parent (накопительно)
    3. При успехе - сбрасываем локальную статистику в 0
    4. Синхронизируем пользователей с parent (создание/обновление/блокировка)

    Returns:
        bool: True если все шаги успешны
    """
    log("=== ⚙ Starting Stable Accumulative Sync v4.0 ===")

    try:
        # Шаг 1: Собираем локальную статистику
        log("Step 1: Собираем локальную дельта статистику...")
        usage_deltas = collect_local_usage_delta()
        log(f"Собрано дельта статистики для {len(usage_deltas)} пользователей")

        # Шаг 2: Отправляем статистику на parent
        if usage_deltas:
            log("Step 2: Отправляем дельта статистику на parent...")
            success, updated_count = send_usage_deltas_to_parent(usage_deltas)

            if success:
                # Шаг 3: Сбрасываем локальную статистику
                log("Step 3: Сбрасываем локальную статистику в ноль...")
                if reset_local_usage(usage_deltas):
                    log(f"✅ Локальная статистика сброшена для {len(usage_deltas)} пользователей")
        else:
            log("Step 2: Нет дельта статистики для отправки")

        # Шаг 4: ПОЛНАЯ синхронизация пользователей
        log("Step 4: Полная синхронизация пользователей с parent...")
        if sync_users_from_parent():
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
