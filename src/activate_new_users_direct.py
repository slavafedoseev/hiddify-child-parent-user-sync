#!/opt/hiddify-manager/.venv313/bin/python
# -*- coding: utf-8 -*-
"""
Helper скрипт для активации новых пользователей через прямой вызов Xray API
Работает БЕЗ Flask app context, используя только pymysql и xtlsapi

Использование:
    python activate_new_users_direct.py UUID1 UUID2 UUID3 ...

Автор: Claude Sonnet 4.5 (Anthropic)
Дата: 2025-12-18
"""

import sys
import pymysql

# Добавляем путь к модулям для xtlsapi
sys.path.insert(0, '/opt/hiddify-manager/.venv313/lib/python3.13/site-packages')

import xtlsapi

# Конфигурация БД (аналогично stable_sync.py)
DB_CONFIG = {
    'unix_socket': '/var/run/mysqld/mysqld.sock',
    'user': 'root',
    'password': '',
    'database': 'hiddifypanel',
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

def get_db_connection():
    """Подключение к БД"""
    try:
        conn = pymysql.connect(**DB_CONFIG)
        return conn
    except Exception as e:
        print(f"❌ Ошибка подключения к БД: {e}")
        return None

def get_inbound_tags(xray_client):
    """Получает список inbound tags из Xray"""
    try:
        inbounds = {inb.name.split(">>>")[1] for inb in xray_client.stats_query('inbound')}
        return list(inbounds)
    except Exception as e:
        print(f"⚠️ Ошибка получения inbound tags: {e}")
        return []

def add_uuid_to_tag(xray_client, uuid, tag, debug=False):
    """Добавляет UUID в конкретный inbound tag (логика из Hiddify xray_api.py)"""
    try:
        # Карта определения протокола по ключевым словам в теге (из Hiddify)
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

        # Определяем протокол по первому совпадению в теге
        protocol = None
        for keyword, proto in proto_map.items():
            if keyword in tag.lower():
                protocol = proto
                break

        if not protocol:
            raise ValueError(f"Unknown protocol for tag: {tag}")

        # flow='xtls-rprx-vision' только для realityin_tcp, для остальных - null byte
        flow = 'xtls-rprx-vision' if 'realityin_tcp' in tag.lower() else '\0'

        xray_client.add_client(
            tag,
            uuid,
            f'{uuid}@hiddify.com',
            protocol=protocol,
            flow=flow,
            alter_id=0,
            cipher='chacha20_poly1305'
        )
        return True
    except xtlsapi.xtlsapi.exceptions.EmailAlreadyExists:
        # UUID уже существует в этом inbound - это нормально
        return True
    except Exception as e:
        # Пропускаем невалидные теги
        if debug:
            print(f"  DEBUG: Не удалось добавить в {tag}: {e}")
        return False

def activate_users(uuids):
    """
    Активирует пользователей в Xray через прямой вызов API

    Args:
        uuids: Список UUID пользователей для активации

    Returns:
        int: Количество успешно активированных пользователей
    """
    conn = get_db_connection()
    if not conn:
        return 0

    try:
        # Подключаемся к Xray API
        xray_client = xtlsapi.XrayClient('127.0.0.1', 10085)

        # Получаем список inbound tags
        tags = get_inbound_tags(xray_client)
        if not tags:
            print("❌ Не удалось получить inbound tags из Xray")
            return 0

        print(f"📋 Найдено inbound tags: {len(tags)}")

        activated_count = 0

        with conn.cursor() as cursor:
            for uuid in uuids:
                # Проверяем существование пользователя в БД
                cursor.execute("""
                    SELECT name, enable, usage_limit, current_usage, package_days, start_date
                    FROM user
                    WHERE uuid = %s
                """, (uuid,))
                user = cursor.fetchone()

                if not user:
                    print(f"⚠️ UUID {uuid} не найден в БД")
                    continue

                if not user['enable']:
                    print(f"⚠️ Пользователь {user['name']} ({uuid}) отключен (enable=0)")
                    continue

                # Добавляем UUID во все inbound tags
                added_to_any = False
                added_tags = []
                for tag in tags:
                    if add_uuid_to_tag(xray_client, uuid, tag, debug=False):
                        added_to_any = True
                        added_tags.append(tag)

                if added_to_any:
                    activated_count += 1
                    print(f"✅ Активирован: {user['name']} ({uuid}) в {len(added_tags)} inbound(s)")
                else:
                    print(f"⚠️ Не удалось активировать {user['name']} ({uuid}) ни в одном inbound")

        return activated_count

    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        return 0
    finally:
        conn.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python activate_new_users_direct.py UUID1 [UUID2 UUID3 ...]")
        print("\nПример:")
        print("  python activate_new_users_direct.py 75498599-9c8b-4665-af53-e7a8a34ddab8")
        sys.exit(1)

    uuids = sys.argv[1:]
    print(f"🔧 Активация {len(uuids)} новых пользователей в Xray...")

    activated = activate_users(uuids)

    if activated > 0:
        print(f"\n✅ Активировано пользователей: {activated}/{len(uuids)}")
        sys.exit(0)
    else:
        print(f"\n❌ Не удалось активировать пользователей")
        sys.exit(1)
