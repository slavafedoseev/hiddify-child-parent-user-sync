#!/usr/bin/env python3
"""
Идемпотентный патчер для Hiddify Celery usage.py
Добавляет db.session.rollback() в обработчик ошибок update_local_usage(),
чтобы PendingRollbackError не накапливался в shared SQLAlchemy session.

Запускается автоматически перед стартом hiddify-panel-background-tasks.service.
Запуск настроен с префиксом systemd "+-" (от root, провал не валит юнит).
Дополнительно: запись обёрнута в try/except, любые ошибки -> exit 0.
"""

import sys
import os
import glob

MARKER = "# PATCHED: db.session.rollback on error"
PATCH_BLOCK = '''        try:
            db.session.rollback()
        except Exception:
            pass
        {marker}'''.format(marker=MARKER)


def find_usage_py():
    """Ищет usage.py в любой версии venv"""
    patterns = [
        "/opt/hiddify-manager/.venv*/lib/python*/site-packages/hiddifypanel/panel/usage.py",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


def patch_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # Уже пропатчен?
    if MARKER in content:
        print(f"[celery-patch] Already patched: {filepath}")
        return False

    # Ищем точку вставки: после logger.exception("Exception in update usage")
    target = 'logger.exception("Exception in update usage")'
    if target not in content:
        # Попробуем альтернативный вариант
        target = "logger.exception('Exception in update usage')"
        if target not in content:
            print(f"[celery-patch] WARNING: patch target not found in {filepath}")
            return False

    # Вставляем rollback после строки с logger.exception
    patched = content.replace(
        target,
        target + "\n" + PATCH_BLOCK
    )

    try:
        with open(filepath, 'w') as f:
            f.write(patched)
    except (PermissionError, OSError) as e:
        print(f"[celery-patch] WARNING: cannot write {filepath}: {e}")
        return False

    print(f"[celery-patch] Successfully patched: {filepath}")
    return True


def main():
    filepath = find_usage_py()
    if not filepath:
        print("[celery-patch] WARNING: usage.py not found, skipping (exit 0)")
        return

    try:
        patch_file(filepath)
    except Exception as e:
        # Никогда не валим старт сервиса из-за патчера
        print(f"[celery-patch] WARNING: unexpected error, skipping: {e}")


if __name__ == "__main__":
    main()
