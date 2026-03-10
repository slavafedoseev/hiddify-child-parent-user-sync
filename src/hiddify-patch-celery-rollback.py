#!/usr/bin/env python3
"""
Идемпотентный патчер для Hiddify Celery usage.py
Добавляет db.session.rollback() в обработчик ошибок update_local_usage(),
чтобы PendingRollbackError не накапливался в shared SQLAlchemy session.

Запускается автоматически перед стартом hiddify-panel-background-tasks.service.
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

    with open(filepath, 'w') as f:
        f.write(patched)

    print(f"[celery-patch] Successfully patched: {filepath}")
    return True


def main():
    filepath = find_usage_py()
    if not filepath:
        print("[celery-patch] ERROR: usage.py not found!")
        sys.exit(1)

    patch_file(filepath)


if __name__ == "__main__":
    main()
