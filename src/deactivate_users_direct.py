#!/opt/hiddify-manager/.venv313/bin/python
# -*- coding: utf-8 -*-
"""
Helper для ДЕАКТИВАЦИИ пользователей в работающем Xray (удаление из всех inbound'ов)
через прямой gRPC API. Симметричен activate_new_users_direct.py.

Назначение: при синхронизации stable_sync.py немедленно разрывает соединения
пользователей, ставших неактивными на parent (is_active=False по любой причине:
ручная блокировка / исчерпание трафика / истечение срока). Без этого юзер
продолжал бы получать трафик на child до фоновой реконсиляции Hiddify.

Идемпотентно: удаление отсутствующего клиента (EmailNotFound) трактуется как no-op.
Возвращает (через stdout) число пользователей, реально удалённых хотя бы из 1 inbound.

Использование:
    python deactivate_users_direct.py UUID1 [UUID2 UUID3 ...]
"""

import sys

# Путь к модулям venv для xtlsapi (как в activate_new_users_direct.py)
sys.path.insert(0, '/opt/hiddify-manager/.venv313/lib/python3.13/site-packages')

import xtlsapi


def get_inbound_tags(xray_client):
    """Получает список inbound tags из работающего Xray."""
    try:
        inbounds = {inb.name.split(">>>")[1] for inb in xray_client.stats_query('inbound')}
        return list(inbounds)
    except Exception as e:
        print(f"⚠️ Ошибка получения inbound tags: {e}")
        return []


def deactivate_users(uuids):
    """
    Удаляет пользователей из всех inbound'ов работающего Xray.

    Args:
        uuids: список UUID для деактивации

    Returns:
        int: число пользователей, реально удалённых хотя бы из одного inbound
             (уже отсутствующие не считаются — операция для них no-op)
    """
    try:
        xray_client = xtlsapi.XrayClient('127.0.0.1', 10085)
    except Exception as e:
        print(f"❌ Не удалось подключиться к Xray API: {e}")
        return 0

    tags = get_inbound_tags(xray_client)
    if not tags:
        print("❌ Не удалось получить inbound tags из Xray")
        return 0

    removed_count = 0
    for uuid in uuids:
        email = f'{uuid}@hiddify.com'
        removed_any = False
        for tag in tags:
            try:
                xray_client.remove_client(tag, email)
                removed_any = True  # клиент был и удалён
            except Exception:
                # EmailNotFound (клиента нет в этом inbound) или несовместимый тег — норм
                pass
        if removed_any:
            removed_count += 1
            print(f"🔌 Деактивирован в Xray: {uuid}")

    return removed_count


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python deactivate_users_direct.py UUID1 [UUID2 UUID3 ...]")
        sys.exit(1)

    uuids = sys.argv[1:]
    deactivated = deactivate_users(uuids)
    print(f"Деактивировано в Xray: {deactivated}/{len(uuids)}")
    sys.exit(0)
