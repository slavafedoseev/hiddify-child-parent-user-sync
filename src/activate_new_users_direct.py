#!/opt/hiddify-manager/.venv313/bin/python
# -*- coding: utf-8 -*-
"""
Activate New Users in Xray Direct API v2.0 (FIXED with proto_map)
Прямая активация новых пользователей в работающих Xray inbound'ах

НАЗНАЧЕНИЕ:
- Активирует новосозданных пользователей в Xray без Apply Config
- Подключается напрямую к Xray API через xtlsapi
- Работает без Flask app context (standalone скрипт)

ИСПОЛЬЗОВАНИЕ:
    sudo /opt/hiddify-manager/activate_new_users_direct.py <UUID1> [UUID2] [UUID3] ...

АВТОР: Auto-activation system for Hiddify Manager
ВЕРСИЯ: 2.0 (CRITICAL FIX: proto_map для правильного определения протоколов)
"""

import sys
import pymysql
import xtlsapi
import traceback

# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================

# Конфигурация подключения к MySQL
DB_CONFIG = {
    'unix_socket': '/var/run/mysqld/mysqld.sock',
    'user': 'root',
    'password': '',
    'database': 'hiddifypanel',
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

# Xray API адрес
XRAY_API_HOST = '127.0.0.1'
XRAY_API_PORT = 10085

# ============================================================================
# ФУНКЦИИ
# ============================================================================

def log(message):
    """Вывод лога с префиксом"""
    print(message)
    sys.stdout.flush()

def get_db_connection():
    """Подключение к MySQL"""
    try:
        conn = pymysql.connect(**DB_CONFIG)
        return conn
    except Exception as e:
        log(f"❌ Ошибка подключения к БД: {e}")
        return None

def get_user_info(uuid):
    """
    Получить информацию о пользователе из БД

    Args:
        uuid: UUID пользователя

    Returns:
        dict: {'name': str, 'enable': int, 'uuid': str} или None
    """
    conn = get_db_connection()
    if not conn:
        return None

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT uuid, name, enable FROM user WHERE uuid = %s",
                (uuid,)
            )
            user = cursor.fetchone()
            conn.close()
            return user
    except Exception as e:
        log(f"❌ Ошибка получения пользователя {uuid}: {e}")
        conn.close()
        return None

def get_xray_inbound_tags():
    """
    Получить список всех inbound tags из Xray

    Returns:
        list: Список тегов (строки)
    """
    try:
        xray_client = xtlsapi.XrayClient(XRAY_API_HOST, XRAY_API_PORT)
        inbounds = xray_client.stats_query('inbound')

        tags = []
        for inb in inbounds:
            # Name формата: "inbound>>>tag_name"
            if ">>>" in inb.name:
                tag = inb.name.split(">>>")[1]
                tags.append(tag)

        return tags
    except Exception as e:
        log(f"❌ Ошибка получения inbound tags: {e}")
        traceback.print_exc()
        return []

def determine_protocol_and_flow(tag):
    """
    Определить protocol и flow для тега (FIXED: proto_map как в Hiddify)

    Args:
        tag: Строка с именем тега

    Returns:
        tuple: (protocol, flow)
    """
    # Карта определения протокола по ключевым словам - из Hiddify xray_api.py
    proto_map = {
        'vless': 'vless',
        'realityin': 'vless',
        'xtls': 'vless',
        'quic': 'vless',
        'reality': 'vless',
        'kcp': 'vless',
        'trojan': 'trojan',
        'dispatcher': 'trojan',
        'vmess': 'vmess',
        'ss': 'shadowsocks',
        'v2ray': 'shadowsocks',
    }
    
    # Определяем протокол по первому совпадению
    protocol = None
    tag_lower = tag.lower()
    for keyword, proto in proto_map.items():
        if keyword in tag_lower:
            protocol = proto
            break
    
    if not protocol:
        # Если не опознан - по умолчанию vless (как в оригинале)
        protocol = 'vless'
    
    # flow='xtls-rprx-vision' только для realityin_tcp, для остальных - null byte (как в Hiddify)
    flow = 'xtls-rprx-vision' if 'realityin_tcp' in tag_lower else '\0'
    
    return (protocol, flow)

def activate_user_in_xray(uuid, user_name):
    """
    Активировать пользователя во всех доступных Xray inbound'ах

    Args:
        uuid: UUID пользователя
        user_name: Имя пользователя (для логов)

    Returns:
        int: Количество успешно активированных inbound'ов
    """
    try:
        xray_client = xtlsapi.XrayClient(XRAY_API_HOST, XRAY_API_PORT)
    except Exception as e:
        log(f"❌ Не удалось подключиться к Xray API: {e}")
        return 0

    # Получаем список inbound tags
    tags = get_xray_inbound_tags()
    if not tags:
        log(f"⚠️ Не найдено inbound tags в Xray")
        return 0

    log(f"📋 Найдено inbound tags: {len(tags)}")

    activated_count = 0
    email = f'{uuid}@hiddify.com'

    for tag in tags:
        protocol, flow = determine_protocol_and_flow(tag)

        try:
            # Добавляем клиента в inbound
            xray_client.add_client(
                tag,  # позиционный аргумент
                uuid,
                email,
                protocol=protocol,
                flow=flow,
                alter_id=0,
                cipher='chacha20_poly1305'
            )
            log(f"  ✓ Добавлен в {tag} ({protocol})")
            activated_count += 1

        except xtlsapi.xtlsapi.exceptions.EmailAlreadyExists:
            # UUID уже существует в этом inbound - это нормально
            log(f"  ✓ Уже существует в {tag} ({protocol})")
            log(f"  ✓ Добавлен в {tag} ({protocol})")
            activated_count += 1
            pass

        except Exception as e:
            # Игнорируем ошибки для невалидных тегов
            log(f"  ✗ Ошибка {tag}: {e}")
            pass

    return activated_count

def activate_users(uuids):
    """
    Активировать список пользователей

    Args:
        uuids: Список UUID пользователей

    Returns:
        tuple: (успешно_активировано, всего_пользователей)
    """
    log(f"🔧 Активация {len(uuids)} новых пользователей в Xray...")

    success_count = 0
    for uuid in uuids:
        # Получаем информацию о пользователе
        user = get_user_info(uuid)

        if not user:
            log(f"⚠️ UUID {uuid} не найден в БД")
            continue

        if not user['enable']:
            log(f"⚠️ Пользователь {user['name']} ({uuid}) отключен (enable=0)")
            continue

        # Активируем пользователя
        activated_count = activate_user_in_xray(uuid, user['name'])

        if activated_count > 0:
            log(f"✅ Активирован: {user['name']} ({uuid}) в {activated_count} inbound(s)")
            success_count += 1
        else:
            log(f"❌ Не удалось активировать: {user['name']} ({uuid})")

    return success_count, len(uuids)

# ============================================================================
# MAIN
# ============================================================================

def main():
    """Главная функция"""
    if len(sys.argv) < 2:
        log("❌ Использование: activate_new_users_direct.py <UUID1> [UUID2] ...")
        return 1

    # Получаем UUID из аргументов командной строки
    uuids = sys.argv[1:]

    try:
        success_count, total_count = activate_users(uuids)

        log(f"")
        log(f"✅ Активировано пользователей: {success_count}/{total_count}")

        return 0 if success_count > 0 else 1

    except Exception as e:
        log(f"❌ Критическая ошибка: {e}")
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
