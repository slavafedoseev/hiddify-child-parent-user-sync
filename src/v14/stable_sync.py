#!/opt/hiddify-manager/.venv313/bin/python
# -*- coding: utf-8 -*-
"""
Hiddify Manager Child-Parent User Synchronization v4.5.1-h14 (child на Hiddify 14)
Синхронизация пользователей и трафика child-панели Hiddify 14 с parent-панелью.

ОСНОВНАЯ ФУНКЦИОНАЛЬНОСТЬ:
  - Получение списка пользователей с parent панели (с проверкой записей)
  - Накопительная синхронизация трафика (child → parent): обнуляются только успешно
    отправленные дельты и только на отправленную величину (без двойного учёта и потерь)
  - Двунаправленная синхронизация last_online (child ↔ parent)
  - Пользователи parent → child через ЛОКАЛЬНЫЙ admin-API Hiddify 14: панель сама применяет
    их ко всем движкам (Xray по gRPC, telemt, hiddify-core через apply-users). Сырой SQL в 14
    пользователя рабочим не делает (панель не следит за БД), helper-ы xtlsapi вредны.
  - Поля, не влияющие на движки (имя, комментарий, telegram_id, язык, last_reset_time), —
    SQL-ом без перезагрузок; PATCH — только enable/лимиты/дни/даты/режим/ключи
  - Предохранители, не останавливающие остальной синк: выключение (массовое) и удаление — только
    если parent требует их два прогона подряд; удаление сверх 5% — через час по частям, больше
    половины — только вручную; не больше 40 записей через API за прогон; не применённое панелью
    поле не PATCH-ится каждые 5 минут; бюджет времени; блокировка от параллельного запуска;
    проверка TLS parent; --dry-run — только план
  - Статус последнего прогона для health: /var/lib/hiddify-child-sync/status.json;
    код выхода 1 при любой ошибке шага

ЗАПУСК: через systemd (hiddify-child-sync.service) или вручную:
  /opt/hiddify-manager/.venv313/bin/python stable_sync.py --dry-run      # план, ничего не меняет
  /opt/hiddify-manager/.venv313/bin/python stable_sync.py --allow-mass   # разово снять предохранители
  --allow-mass-delete — то же и разрешить удалить больше половины локальных (оба — только тех, кого
  нет на parent два наблюдения подряд не ближе 4 минут)
  SYNC_SKIP_USERS=1 — аварийно выключить шаг 6 (трафик и last_online продолжают синхронизироваться)

ТРЕБОВАНИЯ: child — Hiddify Manager 14.x (standalone), parent — Hiddify 11/12 с admin-API v2.
Проект: https://github.com/slavafedoseev/hiddify-child-parent-user-sync
Лицензия: MIT
"""

import sys
import os
import re
import json
import time
import fcntl
import hashlib
import tempfile
import traceback
from datetime import datetime, date

import requests
import pymysql
import urllib3

# ============================================================================
# КОНФИГУРАЦИЯ
# Укажите свои значения (install.sh ставит вариант для Hiddify 12; для 14 — docs/RUNBOOK-v4.5-h14.md).
# При обновлении deploy/v14/build_new.py переносит их из работающего файла.
# ============================================================================

# URL родительской панели (включая admin proxy path)
# Пример: https://parent.example.com/ADMIN_PATH (admin proxy path совпадает с child)
PARENT_URL = "https://your-parent-panel.example.com/ADMIN_PATH"

# API ключ для доступа к parent панели
# Получите в: Admin Panel → Settings → API Keys
API_KEY = "your-api-key-here"

# Проверка TLS-сертификата parent: True — по доверенным CA (по умолчанию); путь к файлу CA или
# сертификата — для parent с самоподписанным сертификатом; False — без проверки (не рекомендуется:
# API-ключ и admin path получит любой, кто перехватит имя parent).
PARENT_TLS_VERIFY = True

# URL ЛОКАЛЬНОЙ (child) панели: admin proxy-path идентичен parent -> деривируем из PARENT_URL.
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
MIN_TRAFFIC_THRESHOLD = 1000000

if PARENT_TLS_VERIFY is False:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================================
# ПАРАМЕТРЫ v4.5.x-h14
# ============================================================================

VERSION = '4.5.1-h14'
DRY_RUN = '--dry-run' in sys.argv            # только план: ничего не меняет (трафик и last_online тоже)
ALLOW_MASS_DELETE = '--allow-mass-delete' in sys.argv   # разово разрешить удалить больше половины локальных
ALLOW_MASS = '--allow-mass' in sys.argv or ALLOW_MASS_DELETE   # разово снять предохранители массовых изменений
SKIP_USERS = os.environ.get('SYNC_SKIP_USERS') == '1'   # аварийный выключатель шага 6
STATUS_DIR = '/var/lib/hiddify-child-sync'
STATUS_FILE = f'{STATUS_DIR}/status.json'
PENDING_DELETE_FILE = f'{STATUS_DIR}/pending_delete.json'
PENDING_BLOCK_FILE = f'{STATUS_DIR}/pending_block.json'
LAST_PATCH_FILE = f'{STATUS_DIR}/last_patch.json'
LOCK_FILE = '/run/hiddify-child-sync.lock'
MAX_WRITES = 40              # не больше записей через API за прогон, остальное — в следующих прогонах
TIME_BUDGET_S = 600          # после 10 минут новые записи не начинаем (TimeoutStartSec=900 в юните)
DELETE_CONFIRM_S = 240       # удаление и массовое выключение — только если parent требует их два прогона подряд
SEEN_GAP_S = 900             # отметка продолжается, только если прошлое наблюдение не старше 15 минут
MASS_DELETE_RELEASE_S = 3600 # удаление сверх лимита само идёт по частям, если держится час
STUCK_RETRY_S = 6 * 3600     # не прижившееся через API поле повторяем не чаще раза в 6 часов
LANGS = {'en', 'fa', 'ru', 'pt', 'zh', 'my'}            # enum user.lang в Hiddify 14
MODES = {'no_reset', 'monthly', 'weekly', 'daily'}       # enum user.mode
MAX_USAGE_GB = 1000000.0     # сеттер usage_limit_GB в 14 обрезает до 1e6 ГБ
_NO_PROXY = {'http': None, 'https': None}                # локальный API — всегда напрямую
_UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
_URL_RE = re.compile(r'https?://\S+')

RESULT = {'created': 0, 'patched': 0, 'sql_fixed': 0, 'deleted': 0, 'blocked': 0, 'unblocked': 0,
          'deferred_inactive': 0, 'held_blocks': 0, 'held_deletes': 0, 'backlog': 0, 'stuck': 0, 'errors': 0,
          'parent_count': None, 'expected_count': None, 'local_count': None, 'last_error': ''}

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================


def _secrets():
    """Строки, которые нельзя писать в журнал: URL parent/child, ключ, хост и admin-путь
    по отдельности (urllib3 печатает их раздельно)."""
    from urllib.parse import urlsplit
    parts = [PARENT_URL, CHILD_URL, API_KEY, urlsplit(PARENT_URL).hostname or '',
             PARENT_URL.rstrip('/').rsplit('/', 1)[-1]]
    return sorted({x for x in parts if x and len(x) >= 4}, key=len, reverse=True)


def _scrub(text, limit=300):
    """Текст для журнала и статуса: без секретов, URL и полных UUID. Обрезка — ПОСЛЕ замен."""
    t = str(text)
    for secret in _secrets():
        t = t.replace(secret, '<hidden>')
    t = _URL_RE.sub('<url>', t)
    t = _UUID_RE.sub('<uuid>', t)
    return t[:limit] if limit else t


def log(message):
    """Логирование с временной меткой (stdout → journald). Всё проходит через _scrub."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {_scrub(message, limit=None)}")
    sys.stdout.flush()


def _log_exc():
    log(_scrub(traceback.format_exc(), limit=4000))


def _anon(user):
    """Обезличенный id пользователя: u:<8 символов sha1(uuid)>."""
    uuid = user.get('uuid', '') if isinstance(user, dict) else str(user or '')
    return 'u:' + hashlib.sha1(str(uuid).encode()).hexdigest()[:8]


def _who(user):
    """Подпись пользователя в журнале: имя; в dry-run и при SYNC_ANON=1 — обезличенный id."""
    if DRY_RUN or os.environ.get('SYNC_ANON') == '1' or not isinstance(user, dict):
        return _anon(user)
    return user.get('name') or _anon(user)


def _note_error(msg, user=None):
    """Ошибка шага: в журнал (с именем), в статус — обезличенно. {who} в msg — подпись пользователя."""
    RESULT['errors'] += 1
    RESULT['last_error'] = _scrub(msg.replace('{who}', _anon(user) if user is not None else ''))
    log(msg.replace('{who}', _who(user) if user is not None else ''))


def get_db_connection():
    """Получить подключение к локальной базе данных MySQL."""
    try:
        return pymysql.connect(**DB_CONFIG)
    except Exception as e:
        log(f"❌ Ошибка подключения к БД: {e}")
        return None


def _user_columns(cursor):
    cursor.execute("SELECT column_name AS c FROM information_schema.columns "
                   "WHERE table_schema = DATABASE() AND table_name = 'user'")
    return {row['c'] for row in cursor.fetchall()}


def fetch_parent_users():
    """Полный список пользователей parent (один GET). None при ошибке или неожиданном ответе."""
    try:
        response = requests.get(
            f"{PARENT_URL}/api/v2/admin/user/",
            headers={'Hiddify-API-Key': API_KEY},
            verify=PARENT_TLS_VERIFY,
            timeout=60
        )
        if response.status_code != 200:
            _note_error(f"❌ Ошибка получения пользователей с parent: HTTP {response.status_code}")
            return None
        parent_users = response.json()
        if not isinstance(parent_users, list):
            _note_error("❌ parent вернул не список пользователей")
            return None
        log(f"Получено {len(parent_users)} пользователей с parent панели")
        return parent_users
    except Exception as e:
        _note_error(f"❌ Ошибка запроса списка пользователей с parent: {e}")
        return None


def validate_parent_users(parent_users):
    """Отбрасывает записи parent без объекта или без канонического UUID (12.x отдаёт uuid=null
    для не-v4). Возвращает (годные, число отброшенных). При отброшенных удаление в прогоне отключается:
    child-копии этих пользователей иначе попали бы в «отсутствующие на parent»."""
    good, dropped, seen = [], 0, set()
    for pu in parent_users:
        uuid = pu.get('uuid') if isinstance(pu, dict) else None
        if not isinstance(uuid, str) or not _UUID_RE.fullmatch(uuid) or uuid in seen:
            dropped += 1
            continue
        seen.add(uuid)
        good.append(pu)
    if dropped:
        _note_error(f"❌ В списке parent {dropped} битых записей (нет объекта/UUID или дубль) — "
                    f"пропущены, удаление в этом прогоне отключено")
    return good, dropped


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
# 1. Собираем локальный current_usage (байты) пользователей parent выше порога
# 2. Для каждого: GET трафика на parent, PATCH parent_usage + дельта
# 3. Обнуляем локально ТОЛЬКО успешно отправленных и ТОЛЬКО на отправленные байты
#    (GREATEST(current_usage - отправлено, 0)): трафик, начисленный панелью за время
#    отправки, не теряется, а сбой одного пользователя не приводит к повторной отправке остальных.
#    Нет на parent (404) — дельта отбрасывается (пользователь удалится шагом 6).
# ============================================================================

def collect_local_usage_delta(parent_uuids):
    """Локальные дельты пользователей parent выше порога. None при ошибке БД."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cols = _user_columns(cursor)
            alive = "COALESCE(deleted,0)=0 AND " if 'deleted' in cols else ""
            cursor.execute(f"SELECT uuid, name, current_usage FROM user WHERE {alive}current_usage > %s",
                           (MIN_TRAFFIC_THRESHOLD,))
            rows = cursor.fetchall()
        deltas = []
        for u in rows:
            used = int(u['current_usage'] or 0)
            if u['uuid'] not in parent_uuids or used / (1024 ** 3) <= 0.001:
                continue
            deltas.append({'uuid': u['uuid'], 'name': u['name'], 'bytes': used,
                           'usage_delta_GB': round(used / (1024 ** 3), 6)})
        log(f"Найдено {len(deltas)} пользователей с трафиком > {MIN_TRAFFIC_THRESHOLD / (1024 ** 3):.3f}GB")
        return deltas
    except Exception as e:
        log(f"❌ Ошибка сбора статистики: {e}")
        _log_exc()
        return None
    finally:
        conn.close()


def get_parent_user_usage(uuid):
    """('ok', ГБ) | ('missing', None) при 404 | ('error', None)."""
    try:
        response = requests.get(
            f"{PARENT_URL}/api/v2/admin/user/{uuid}/",
            headers={'Hiddify-API-Key': API_KEY},
            verify=PARENT_TLS_VERIFY,
            timeout=30
        )
        if response.status_code == 200:
            return 'ok', float(response.json().get('current_usage_GB') or 0)
        if response.status_code == 404:
            return 'missing', None
        log(f"❌ Ошибка получения пользователя {uuid[:8]}… с parent: HTTP {response.status_code}")
        return 'error', None
    except Exception as e:
        log(f"❌ Ошибка запроса к parent: {e}")
        return 'error', None


def update_parent_user_usage(uuid, new_usage_gb, name="Unknown"):
    """Обновляет суммарный трафик пользователя на parent панели через PATCH."""
    try:
        response = requests.patch(
            f"{PARENT_URL}/api/v2/admin/user/{uuid}/",
            headers={'Hiddify-API-Key': API_KEY},
            json={"current_usage_GB": new_usage_gb},
            verify=PARENT_TLS_VERIFY,
            timeout=30
        )
        if response.status_code == 200:
            log(f"✅ Обновлён трафик {name}: {new_usage_gb:.3f}GB")
            return True
        log(f"❌ Ошибка обновления {name}: HTTP {response.status_code} - {response.text[:200]}")
        return False
    except Exception as e:
        log(f"❌ Ошибка обновления пользователя {name}: {e}")
        return False


def send_usage_deltas_to_parent(usage_deltas):
    """Отправляет дельты на parent. Возвращает (отправленные, отброшенные, число сбоев)."""
    sent, dropped, failed = [], [], 0
    if not usage_deltas:
        return sent, dropped, failed
    log(f"Отправка дельта статистики для {len(usage_deltas)} пользователей...")
    for delta in usage_deltas:
        uuid, name = delta['uuid'], _who(delta)
        try:
            state, parent_usage = get_parent_user_usage(uuid)
            if state == 'missing':
                log(f"⚠️  {name}: нет на parent — дельта {delta['usage_delta_GB']:.3f}GB отброшена")
                dropped.append(delta)
                continue
            if state != 'ok':
                failed += 1
                continue
            new_usage = parent_usage + delta['usage_delta_GB']
            log(f"Пользователь {name}: parent={parent_usage:.3f}GB + local={delta['usage_delta_GB']:.3f}GB = {new_usage:.3f}GB")
            if update_parent_user_usage(uuid, new_usage, name):
                sent.append(delta)
            else:
                failed += 1
        except Exception as e:
            failed += 1
            log(f"❌ Трафик {name}: {e}")
    log(f"{'✅' if not failed else '⚠️'} Трафик: отправлено {len(sent)}, отброшено {len(dropped)}, "
        f"сбоев {failed} из {len(usage_deltas)}")
    return sent, dropped, failed


def reset_local_usage(entries):
    """Вычитает отправленные (или отброшенные) байты из локального current_usage."""
    if not entries:
        return True
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cursor:
            for d in entries:
                cursor.execute("UPDATE user SET current_usage = GREATEST(current_usage - %s, 0) WHERE uuid = %s",
                               (int(d['bytes']), d['uuid']))
        conn.commit()
        log(f"✅ Локальный трафик уменьшен на отправленное у {len(entries)} пользователей")
        return True
    except Exception as e:
        log(f"❌ Ошибка сброса локальной статистики: {e}")
        _log_exc()
        return False
    finally:
        conn.close()


# ============================================================================
# СИНХРОНИЗАЦИЯ LAST_ONLINE (CHILD ↔ PARENT): выигрывает более свежее значение
# ============================================================================

def sync_last_online(parent_users):
    """Двунаправленная синхронизация last_online. Ошибка одного пользователя не мешает остальным."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        parent_online_map = {pu['uuid']: parse_datetime(pu.get('last_online')) for pu in parent_users}
        failures = 0
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cols = _user_columns(cursor)
            alive = " WHERE COALESCE(deleted,0)=0" if 'deleted' in cols else ""
            cursor.execute("SELECT uuid, name, last_online FROM user" + alive)
            local_users = cursor.fetchall()
            pushed = pulled = 0
            for local_user in local_users:
                uuid, local_online, name = local_user['uuid'], local_user['last_online'], _who(local_user)
                if uuid not in parent_online_map:
                    continue
                parent_online = parent_online_map[uuid]
                try:
                    if local_online and (not parent_online or local_online > parent_online):
                        if _push_last_online_to_parent(uuid, local_online, name):
                            pushed += 1
                        else:
                            failures += 1
                    elif parent_online and (not local_online or parent_online > local_online):
                        cursor.execute("UPDATE user SET last_online = %s WHERE uuid = %s", (parent_online, uuid))
                        pulled += 1
                except Exception as e:
                    failures += 1
                    log(f"  ⚠️ {name}: last_online: {e}")
        conn.commit()
        log(f"✅ last_online: ↑{pushed} → parent, ↓{pulled} ← parent" if (pushed or pulled)
            else "last_online: всё актуально, обновлений не требуется")
        return failures == 0
    except Exception as e:
        log(f"❌ Ошибка синхронизации last_online: {e}")
        _log_exc()
        return False
    finally:
        conn.close()


def _push_last_online_to_parent(uuid, local_online, name):
    """Отправляет last_online одного пользователя на parent через PATCH."""
    try:
        response = requests.patch(
            f"{PARENT_URL}/api/v2/admin/user/{uuid}/",
            headers={'Hiddify-API-Key': API_KEY},
            json={"last_online": local_online.strftime("%Y-%m-%d %H:%M:%S")},
            verify=PARENT_TLS_VERIFY,
            timeout=30
        )
        if response.status_code == 200:
            return True
        log(f"  ⚠️ {name}: не удалось обновить last_online на parent: HTTP {response.status_code}")
        return False
    except Exception as e:
        log(f"  ⚠️ {name}: ошибка отправки last_online: {e}")
        return False


# ============================================================================
# СИНХРОНИЗАЦИЯ ПОЛЬЗОВАТЕЛЕЙ (PARENT → CHILD) ЧЕРЕЗ ЛОКАЛЬНЫЙ ADMIN-API HIDDIFY 14
#
# Особенности API 14 (hiddifypanel 14.1.0b4):
#   POST  — add_client во все движки БЕЗ проверки активности → неактивных на parent НЕ создаём
#           (создадутся, когда станут активны); POST с uuid мягко удалённой строки удаляет её
#           окончательно и создаёт заново; name обязателен.
#   PATCH — remove_client, upsert, add_client только активному; каждый POST/PATCH/DELETE ставит
#           apply-users (SIGHUP hiddify-core и telemt) → PATCH только по реальному отличию полей,
#           влияющих на движки и активность (enable, лимит, дни, дата старта, режим, ключи).
#   upsert применяет name/comment/telegram_id только непустые, start_date и package_days —
#           независимо, ed25519 — только парой; last_reset_time не применяет вовсе → имя,
#           комментарий, telegram_id, язык и last_reset_time пишем SQL-ом (на движки не влияют).
#   DELETE — мягкое удаление (deleted=1); GET user/ отдаёт только неудалённых пользователей
#           дерева админа ключа; 404 «You have no user» — пустой список.
# ============================================================================

def _child_api(method, path, json_body=None, timeout=30):
    """Запрос к локальному admin-API child (127.0.0.1:9000)."""
    return requests.request(
        method, f"{CHILD_URL}/api/v2/admin/{path}",
        headers={'Hiddify-API-Key': API_KEY}, json=json_body,
        timeout=timeout, proxies=_NO_PROXY, verify=False)


def fetch_child_users():
    """Неудалённые локальные пользователи через API → dict по uuid; None при ошибке."""
    try:
        r = _child_api('GET', 'user/', timeout=60)
        if r.status_code == 404 and 'no user' in r.text.lower():   # «You have no user»
            return {}
        if r.status_code != 200:
            _note_error(f"❌ Локальный API: список юзеров HTTP {r.status_code} {r.text}")
            return None
        data = r.json()
        if not isinstance(data, list):
            _note_error("❌ Локальный API: список юзеров — не список")
            return None
        return {u['uuid']: u for u in data if isinstance(u, dict) and u.get('uuid')}
    except Exception as e:
        _note_error(f"❌ Локальный API недоступен: {e}")
        return None


def _norm_date(v):
    return str(v)[:10] if v else None


def _norm_tg(v):
    """telegram_id: int или None (parent 12 может отдать строку)."""
    if v is None or v == '':
        return None
    try:
        v = int(str(v).strip())
        return v or None
    except (TypeError, ValueError):
        return None


def _desired_fields(pu):
    """Поля для API (движки и активность) из записи parent. enable child = is_active parent
    (учитывает блокировку, исчерпание трафика и истечение срока)."""
    is_active = pu.get('is_active')
    if is_active is None:              # null или нет поля — по enable, а не «выключить всех»
        is_active = pu.get('enable')
    d = {
        'usage_limit_GB': min(float(pu.get('usage_limit_GB') or 0), MAX_USAGE_GB),
        'package_days': int(float(pu.get('package_days') or 0)),
        'enable': bool(is_active),
        'start_date': _norm_date(pu.get('start_date')),
    }
    mode = pu.get('mode') or 'no_reset'
    if mode in MODES:
        d['mode'] = mode
    # ed25519 upsert применяет только парой непустых; wg-ключи — по отдельности непустые.
    if pu.get('ed25519_private_key') and pu.get('ed25519_public_key'):
        d['ed25519_private_key'] = pu['ed25519_private_key']
        d['ed25519_public_key'] = pu['ed25519_public_key']
    for k in ('wg_pk', 'wg_pub', 'wg_psk'):
        if pu.get(k):
            d[k] = pu[k]
    return d


def _meta_fields(pu):
    """Поля, не влияющие на движки: пишутся SQL-ом (и в теле POST для новых)."""
    lang = pu.get('lang')
    return {
        'name': str(pu.get('name') or '')[:512],
        'comment': str(pu.get('comment') or '')[:512],
        'telegram_id': _norm_tg(pu.get('telegram_id')),
        'lang': lang if lang in LANGS else None,
        'last_reset_time': _norm_date(pu.get('last_reset_time')),
    }


def _expired(start_date, package_days):
    """Истёк ли пакет по часам child (как remaining_days < 0 в Hiddify)."""
    try:
        return (date.today() - date.fromisoformat(start_date)).days > int(package_days)
    except (TypeError, ValueError):
        return False


def _changed_fields(desired, child_user):
    """Только реально отличающиеся поля движков/активности → тело PATCH (пусто = ничего не делать)."""
    body = {}
    for k, v in desired.items():
        c = child_user.get(k)
        if k == 'usage_limit_GB':
            if abs(float(c or 0) - v) > 1e-6:
                body[k] = v
        elif k == 'package_days':
            if int(float(c or 0)) != v:
                body[k] = v
        elif k == 'enable':
            if bool(c) != v:
                body[k] = v
        elif k == 'start_date':
            # None = «пакет ещё не начат», дату первого подключения ставит панель. Реальную дату
            # parent синхронизируем всегда; None — только если пакет на child истёк по его часам,
            # а на parent пользователь активен (продление «сбросом дней»). Иначе flip-flop.
            c_sd = _norm_date(c)
            if v is not None:
                if c_sd != v:
                    body[k] = v
            elif c_sd is not None and desired['enable'] and _expired(c_sd, desired['package_days']):
                body[k] = None
        else:
            if (c if c is not None else '') != v:
                body[k] = v
    if 'ed25519_private_key' in body or 'ed25519_public_key' in body:
        body['ed25519_private_key'] = desired['ed25519_private_key']
        body['ed25519_public_key'] = desired['ed25519_public_key']
    return body


def _load_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json(path, data, mode=0o600):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.tmp.')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _sql_fixups(parent_users):
    """Имя, комментарий, telegram_id, язык и last_reset_time из parent — SQL-ом, только при отличии.
    На движки не влияют, поэтому без apply-users. В 14 вместе с ними last_modified_time=NOW()
    (хук ORM before_update обходится)."""
    conn = get_db_connection()
    if not conn:
        _note_error("❌ SQL-выравнивание: нет соединения с БД")
        return
    fixed = 0
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cols = _user_columns(cursor)
            alive = " WHERE COALESCE(deleted,0)=0" if 'deleted' in cols else ""
            fields = [f for f in ('name', 'comment', 'telegram_id', 'lang', 'last_reset_time') if f in cols]
            cursor.execute(f"SELECT uuid, {', '.join(fields)} FROM user" + alive)
            local = {r['uuid']: r for r in cursor.fetchall()}
            touch = ", last_modified_time=NOW()" if 'last_modified_time' in cols else ""
            for pu in parent_users:
                row = local.get(pu['uuid'])
                if not row:
                    continue
                try:
                    want = _meta_fields(pu)
                    sets = {}
                    if 'name' in fields and want['name'] and (row['name'] or '') != want['name']:
                        sets['name'] = want['name']
                    if 'comment' in fields and (row['comment'] or '') != want['comment']:
                        sets['comment'] = want['comment'] or None
                    if 'telegram_id' in fields and _norm_tg(row['telegram_id']) != want['telegram_id']:
                        sets['telegram_id'] = want['telegram_id']
                    if 'lang' in fields and want['lang'] and row['lang'] != want['lang']:
                        sets['lang'] = want['lang']
                    if 'last_reset_time' in fields and want['last_reset_time'] \
                            and _norm_date(row['last_reset_time']) != want['last_reset_time']:
                        sets['last_reset_time'] = want['last_reset_time']
                    if not sets:
                        continue
                    fixed += 1
                    if DRY_RUN:
                        log(f"[dry-run] 🛠 SQL {_who(pu)}: {sorted(sets)}")
                        continue
                    assign = ', '.join(f"{k}=%s" for k in sets)
                    cursor.execute(f"UPDATE user SET {assign}{touch} WHERE uuid=%s",
                                   tuple(sets.values()) + (pu['uuid'],))
                except Exception as e:
                    _note_error(f"❌ SQL-выравнивание {{who}}: {e}", pu)
        if not DRY_RUN:
            conn.commit()
        RESULT['sql_fixed'] += fixed
        if fixed:
            log(f"🛠 Без перезагрузок выровнено SQL-ом у {fixed} юзеров (имя/комментарий/telegram/язык/сброс)"
                + (" [dry-run]" if DRY_RUN else ""))
    except Exception as e:
        _note_error(f"❌ SQL-выравнивание: {e}")
    finally:
        conn.close()


def _db_user_count():
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cols = _user_columns(cursor)
            alive = " WHERE COALESCE(deleted,0)=0" if 'deleted' in cols else ""
            cursor.execute("SELECT COUNT(*) AS n FROM user" + alive)
            return int(cursor.fetchone()['n'])
    except Exception as e:
        log(f"⚠️ Подсчёт пользователей в БД: {e}")
        return None
    finally:
        conn.close()


def _delete_users(to_delete, child_users, deadline):
    deleted = 0
    for uuid in to_delete:
        cu = child_users.get(uuid) or {'uuid': uuid}
        if time.time() > deadline:
            _note_error("❌ Бюджет времени прогона исчерпан — оставшиеся удаления в следующем прогоне")
            break
        try:
            r = _child_api('DELETE', f'user/{uuid}/')
            if r.status_code in (200, 204):
                deleted += 1
                log(f"🗑️  Удалён отсутствующий на parent: {_who(cu)}")
            else:
                _note_error(f"❌ Удаление {{who}}: HTTP {r.status_code} {r.text}", cu)
        except Exception as e:
            _note_error(f"❌ Удаление {{who}}: {e}", cu)
    return deleted


def _plan_deletions(child_users, parent_uuids, dropped, now):
    """Кого удалять. Отметки [первое, последнее наблюдение] в pending_delete.json ведутся в каждом прогоне
    со списком parent: вернувшийся на parent снимается, перерыв наблюдений больше SEEN_GAP_S начинает
    отметку заново. Удаляем отсутствующих два прогона подряд (>= DELETE_CONFIRM_S). Больше max(3, 5%)
    локальных разом — массовое удаление: ждёт MASS_DELETE_RELEASE_S и идёт по limit за прогон, старейшие
    первыми (предупреждение, не ошибка); больше половины локальных — похоже на сбой parent, только вручную.
    --allow-mass — удалить сейчас всех подтверждённых (нет на parent два наблюдения подряд не ближе
    DELETE_CONFIRM_S) без часа ожидания и лимита; больше половины — только с --allow-mass-delete."""
    missing = sorted(set(child_users) - parent_uuids)
    keyed = {_anon(u): u for u in missing}
    pending = _load_json(PENDING_DELETE_FILE)
    if dropped or not parent_uuids:
        # Список parent неполный: новых отметок не ставим, вернувшихся снимаем, не удаляем.
        if not DRY_RUN:
            _save_json(PENDING_DELETE_FILE, {k: pending[k] for k in keyed if k in pending})
        if missing:
            log(f"⚠️ Удаление пропущено: список parent неполный (missing={len(missing)})")
        return []
    marks = _marks_update(pending, keyed, now)
    if not DRY_RUN:
        _save_json(PENDING_DELETE_FILE, marks)
    limit = max(3, len(child_users) * 5 // 100)
    confirmed = sorted((marks[k][0], u) for k, u in keyed.items() if now - marks[k][0] >= DELETE_CONFIRM_S)
    if len(missing) <= limit and not ALLOW_MASS:
        for k, u in keyed.items():
            if marks[k][0] == now:
                log(f"⏳ Нет на parent, удалю, если не появится к следующему прогону: {_who(child_users[u])}")
        return [u for ts, u in confirmed]
    if len(missing) * 2 > len(child_users) and not ALLOW_MASS_DELETE:
        _note_error(f"❌ Предохранитель удаления: на parent нет {len(missing)} из {len(child_users)} локальных — "
                    f"похоже на сбой parent, ничего не удаляю (проверьте parent; удалить всё равно: "
                    f"--allow-mass-delete)")
        return []
    if ALLOW_MASS:
        ready = [u for ts, u in confirmed]          # два наблюдения не ближе DELETE_CONFIRM_S, как у таймера
        if len(ready) < len(missing):
            log(f"⏳ {len(missing) - len(ready)} нет на parent меньше {DELETE_CONFIRM_S // 60} мин — их не удаляю "
                f"(повторите запуск с флагом позже)")
        return ready
    take = [u for ts, u in confirmed if now - ts >= MASS_DELETE_RELEASE_S][:limit]
    RESULT['held_deletes'] = len(missing) - len(take)
    log(f"⏸ Массовое удаление: на parent нет {len(missing)} > {limit} (local={len(child_users)}) — "
        + (f"удаляю {len(take)} старейших, остальные в следующих прогонах" if take else
           f"начну через {MASS_DELETE_RELEASE_S // 60} мин по {limit} за прогон (сразу: --allow-mass)"))
    return take


def _refresh_marks(parent_users, dropped):
    """Отметки по списку parent в каждом прогоне — даже если шаг 6 до них не дойдёт (сбой локального API,
    SYNC_SKIP_USERS, исключение): вернувшиеся на parent снимаются с удаления, снова активные — с
    выключения; по-прежнему отсутствующим (при полном списке) и неактивным свежие отметки продлеваются.
    Тогда SEEN_GAP_S прощает только настоящее отсутствие наблюдений (сбой parent, остановленный таймер)."""
    now = int(time.time())
    seen = {_anon(pu) for pu in parent_users}
    inactive = set()
    for pu in parent_users:
        try:
            if not _desired_fields(pu)['enable']:
                inactive.add(_anon(pu))
        except Exception:
            pass

    def fresh(p):
        return (isinstance(p, list) and len(p) == 2 and all(isinstance(x, int) for x in p)
                and 0 <= now - p[1] <= SEEN_GAP_S)

    dele = _load_json(PENDING_DELETE_FILE)
    new_dele = {k: ([p[0], now] if fresh(p) and not dropped else p) for k, p in dele.items() if k not in seen}
    blk = _load_json(PENDING_BLOCK_FILE)
    new_blk = {k: ([p[0], now] if fresh(p) and k in inactive else p) for k, p in blk.items()
               if k in inactive or k not in seen}
    if new_dele != dele:
        _save_json(PENDING_DELETE_FILE, new_dele)
    if new_blk != blk:
        _save_json(PENDING_BLOCK_FILE, new_blk)


def _fp(value):
    """Отпечаток значения поля (sha256, 16 символов): в last_patch.json не хранятся сами значения —
    среди полей бывают приватные ключи WireGuard и ed25519."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _marks_update(pending, keys, now):
    """Отметки [первое, последнее наблюдение] для keys: свежая (прошлое наблюдение не старше SEEN_GAP_S)
    продолжается, устаревшая или новая начинается заново. Ключей не из keys в результате нет."""
    out = {}
    for k in keys:
        p = pending.get(k)
        if isinstance(p, int):                       # формат v4.5-h14: только первое наблюдение
            p = [p, p]
        if (isinstance(p, list) and len(p) == 2 and all(isinstance(x, int) for x in p)
                and 0 <= now - p[1] <= SEEN_GAP_S):
            out[k] = [p[0], now]
        else:
            out[k] = [now, now]
    return out


def _drop_stuck_fields(patches, now):
    """Не прижившиеся поля: тот же PATCH поле=значение уже успешно ушёл прошлым прогоном, а отличие
    осталось — формат или семантика API разошлись. Повторять бессмысленно (каждый PATCH перезагружает
    юзера в движках): такие поля убираем из тел (ошибка прогона), повтор — раз в STUCK_RETRY_S.
    Возвращает записи журнала PATCH, которые надо перенести в следующий прогон."""
    last = _load_json(LAST_PATCH_FILE)
    carry, stuck = {}, {}
    for pu, body, cu in patches:
        a = _anon(pu)
        prev = last.get(a) if isinstance(last.get(a), dict) else {}
        for k in list(body):
            p = prev.get(k)
            if (isinstance(p, list) and len(p) == 2 and p[0] == _fp(body[k])
                    and isinstance(p[1], int) and now - p[1] < STUCK_RETRY_S):
                stuck[k] = stuck.get(k, 0) + 1
                carry.setdefault(a, {})[k] = p
                del body[k]
    if stuck:
        RESULT['stuck'] += sum(stuck.values())
        _note_error("❌ Не применяются через API (тот же PATCH уже был успешен): "
                    + ", ".join(f"{k}×{n}" for k, n in sorted(stuck.items()))
                    + f" — формат полей Hiddify разошёлся? Повтор раз в {STUCK_RETRY_S // 3600} ч "
                    f"(раньше: удалить {LAST_PATCH_FILE})")
    return carry


def _hold_mass_blocks(patches, child_users, now):
    """Массовое выключение (больше max(5, 10% активных)): каждое применяется, только если parent требует
    его два прогона подряд (>= DELETE_CONFIRM_S без перерыва наблюдений), — разовый сбой parent не
    выключит всех, а настоящая массовая блокировка пройдёт через прогон. Остальной синк не ждёт.
    Отметки ведутся для всех запрошенных выключений и снимаются только после успешного PATCH (в цикле
    записей): выключение, срезанное MAX_WRITES, бюджетом времени или сбоем, повторного ожидания не требует.
    Возвращает (отметки, число отложенных выключений)."""
    blocks = {_anon(pu): body for pu, body, cu in patches if body.get('enable') is False}
    active_local = sum(1 for u in child_users.values() if u.get('enable'))
    limit = max(5, active_local // 10)
    marks = _marks_update(_load_json(PENDING_BLOCK_FILE), blocks, now)
    held = 0
    if len(blocks) > limit and not ALLOW_MASS:
        for k, body in blocks.items():
            if now - marks[k][0] < DELETE_CONFIRM_S:
                del body['enable']
                held += 1
        if held:
            log(f"⏸ Массовое выключение: {len(blocks)} > {limit} (активных {active_local}) — "
                f"{held} выключу, если parent подтвердит в следующем прогоне")
    return marks, held


def _patch_user(pu, body):
    """PATCH. Возвращает (успех HTTP, поля тела, которые панель так и не применила). Ответ PATCH в 14 —
    пользователь после upsert, поэтому не применённое видно сразу, без ожидания следующего прогона."""
    try:
        r = _child_api('PATCH', f"user/{pu['uuid']}/", body)
        if not r.ok:
            _note_error(f"❌ Обновление {{who}}: HTTP {r.status_code} {r.text}", pu)
            return False, set()
        try:
            resp = r.json()
            not_applied = (set(body) & set(_changed_fields(_desired_fields(pu), resp))
                           if isinstance(resp, dict) and resp.get('uuid') else set())
        except Exception:
            not_applied = set()
        RESULT['patched'] += 1
        if 'enable' in body and 'enable' not in not_applied:
            RESULT['unblocked' if body['enable'] else 'blocked'] += 1
        log(f"✏️ Обновлён {_who(pu)}: {sorted(body)}"
            + ('' if 'enable' not in body else (' — включён' if body['enable'] else ' — выключен'))
            + (f"; не применено панелью: {sorted(not_applied)}" if not_applied else ''))
        return True, not_applied
    except Exception as e:
        _note_error(f"❌ Обновление {{who}}: {e}", pu)
        return False, set()


def _post_user(pu, desired):
    try:
        meta = _meta_fields(pu)
        body = {k: v for k, v in desired.items() if v is not None}
        body.update({'uuid': pu['uuid'], 'current_usage_GB': 0,
                     'name': meta['name'] or f"user-{pu['uuid'][:8]}"})   # name обязателен в 14
        for k in ('comment', 'telegram_id', 'lang'):
            if meta[k]:
                body[k] = meta[k]
        r = _child_api('POST', 'user/', body)
        if r.ok:
            RESULT['created'] += 1
            log(f"➕ Создан: {_who(pu)}")
        elif r.status_code == 400 and 'exists' in r.text:
            _note_error("❌ Создание {who}: в БД есть, но в GET user/ не виден "
                        "(added_by вне дерева админа ключа?) — разберитесь вручную", pu)
        else:
            _note_error(f"❌ Создание {{who}}: HTTP {r.status_code} {r.text}", pu)
    except Exception as e:
        _note_error(f"❌ Создание {{who}}: {e}", pu)


def sync_users_from_parent(parent_users, dropped=0):
    """
    Синхронизация пользователей parent → child через локальный admin-API (Hiddify 14).
      - нет на child и активен на parent → POST (неактивных не создаём);
      - есть, отличаются поля движков → PATCH только отличающихся полей (сначала выключения);
      - имя/комментарий/telegram/язык/last_reset_time → SQL без перезагрузок;
      - нет на parent два прогона подряд → DELETE (мягкое в 14).
    Массовое выключение — через прогон, массовое удаление — через час по частям, больше MAX_WRITES
    записей — по частям, не применённые панелью поля не повторяются (ошибка). Каждый пользователь отдельно.
    Возвращает True, если в шаге 6 не было своих ошибок.
    """
    err0 = RESULT['errors']
    deadline = time.time() + TIME_BUDGET_S
    try:
        child_users = fetch_child_users()
        if child_users is None:
            return False
        parent_uuids = {pu['uuid'] for pu in parent_users}
        RESULT['parent_count'] = len(parent_users)

        posts, patches = [], []
        for pu in parent_users:
            try:
                desired = _desired_fields(pu)
                cu = child_users.get(pu['uuid'])
                if cu is None:
                    if desired['enable']:
                        posts.append((pu, desired))
                    else:
                        RESULT['deferred_inactive'] += 1
                    continue
                body = _changed_fields(desired, cu)
                if body:
                    patches.append((pu, body, cu))
            except Exception as e:
                _note_error(f"❌ Юзер {{who}}: данные parent не разобраны: {e}", pu)
        RESULT['expected_count'] = len(parent_users) - RESULT['deferred_inactive']

        now = int(time.time())
        carry = _drop_stuck_fields(patches, now)
        block_marks, RESULT['held_blocks'] = _hold_mass_blocks(patches, child_users, now)
        # Порядок: выключения, включения, создания, прочие обновления; не больше MAX_WRITES за прогон.
        ops = [(0 if b.get('enable') is False else 1 if b.get('enable') is True else 3, 'PATCH', pu, b)
               for pu, b, cu in patches if b]
        ops += [(2, 'POST', pu, desired) for pu, desired in posts]
        ops.sort(key=lambda o: o[0])
        if len(ops) > MAX_WRITES and not ALLOW_MASS:
            RESULT['backlog'] = len(ops) - MAX_WRITES
            log(f"⏳ Изменений через API {len(ops)} > {MAX_WRITES} за прогон — "
                f"{RESULT['backlog']} в следующих прогонах")
            ops = ops[:MAX_WRITES]

        if DRY_RUN:
            for prio, kind, pu, body in ops:
                if kind == 'POST':
                    log(f"[dry-run] ➕ POST {_who(pu)}")
                    continue
                arrow = '' if 'enable' not in body else (' (включить)' if body['enable'] else ' (выключить)')
                log(f"[dry-run] ✏️ PATCH {_who(pu)}: {sorted(body)}{arrow}")
            RESULT.update(created=sum(1 for o in ops if o[1] == 'POST'),
                          patched=sum(1 for o in ops if o[1] == 'PATCH'),
                          blocked=sum(1 for o in ops if o[0] == 0), unblocked=sum(1 for o in ops if o[0] == 1))
        else:
            not_applied = {}
            for i, (prio, kind, pu, body) in enumerate(ops):
                if time.time() > deadline:
                    RESULT['backlog'] += len(ops) - i
                    _note_error("❌ Бюджет времени прогона исчерпан — остальные изменения в следующем прогоне")
                    break
                if kind == 'POST':
                    _post_user(pu, body)
                    continue
                ok, failed = _patch_user(pu, body)
                if not ok:
                    continue
                a = _anon(pu)
                carry.setdefault(a, {}).update({k: [_fp(v), now] for k, v in body.items()})
                for k in failed:
                    not_applied[k] = not_applied.get(k, 0) + 1
                if body.get('enable') is False and 'enable' not in failed:
                    block_marks.pop(a, None)
            if not_applied:
                RESULT['stuck'] += sum(not_applied.values())
                _note_error("❌ PATCH успешен, но панель не применила: "
                            + ", ".join(f"{k}×{n}" for k, n in sorted(not_applied.items()))
                            + f" — формат полей Hiddify разошёлся? Повтор раз в {STUCK_RETRY_S // 3600} ч")
            _save_json(LAST_PATCH_FILE, carry)
            _save_json(PENDING_BLOCK_FILE, block_marks)

        to_delete = _plan_deletions(child_users, parent_uuids, dropped, now)
        if to_delete:
            if DRY_RUN:
                for u in to_delete:
                    log(f"[dry-run] 🗑️ DELETE {_who(child_users[u])}")
                RESULT['deleted'] = len(to_delete)
            else:
                RESULT['deleted'] = _delete_users(to_delete, child_users, deadline)

        _sql_fixups(parent_users)

        RESULT['local_count'] = _db_user_count() if not DRY_RUN else len(child_users)
        errs = RESULT['errors'] - err0
        log(f"✅ Синхронизация: {len(parent_users)} проверено, {RESULT['created']} создано, "
            f"{RESULT['patched']} обновлено, {RESULT['sql_fixed']} выровнено SQL, {RESULT['deleted']} удалено, "
            f"{RESULT['blocked']} выключено, {RESULT['unblocked']} включено, "
            f"{RESULT['deferred_inactive']} неактивных не создано, {RESULT['held_blocks']} выключений и "
            f"{RESULT['held_deletes']} удалений отложено, "
            f"{RESULT['backlog']} изменений перенесено, {errs} ошибок"
            + (" [dry-run]" if DRY_RUN else ""))
        return errs == 0

    except Exception as e:
        _note_error(f"❌ Ошибка синхронизации пользователей: {e}")
        _log_exc()
        return False


# ============================================================================
# СТАТУС И БЛОКИРОВКА
# ============================================================================

def write_status(ok, steps, started):
    """Статус последнего прогона для health API (атомарно, 0600; без UUID, имён и секретов).
    В dry-run не пишем."""
    if DRY_RUN:
        return
    try:
        prev = _load_json(STATUS_FILE)
        now = int(time.time())
        st = dict(RESULT)
        st.update(version=VERSION, ts=now, ok=bool(ok), steps=steps,
                  duration_s=round(time.time() - started, 1),
                  last_ok_ts=now if ok else prev.get('last_ok_ts'),
                  last_users_ok_ts=now if steps.get('users') else prev.get('last_users_ok_ts'),
                  users_skipped=SKIP_USERS,
                  consecutive_failures=0 if ok else int(prev.get('consecutive_failures') or 0) + 1)
        _save_json(STATUS_FILE, st)
    except Exception as e:
        log(f"⚠️ Не записан статус: {e}")


def _acquire_lock():
    """flock от параллельного прогона. fd — захвачено, False — занято, None — без блокировки."""
    try:
        fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    except Exception as e:
        log(f"⚠️ Не открыт файл блокировки: {e}")
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return False


# ============================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================================

def main():
    """
    Последовательность (каждый шаг в своём try; шаг 6 выполняется при любом исходе шагов 2–5):
      1. Список пользователей parent (с проверкой записей)
      2–4. Трафик child → parent и вычитание отправленного
      5. last_online (двунаправленно)
      6. Пользователи parent → child через локальный admin-API (SYNC_SKIP_USERS=1 — пропустить)
    Код выхода 0 — только если все шаги прошли; иначе 1 (systemd Result=exit-code, health).
    В --dry-run выполняются только шаги 1 и 6 без изменений.
    """
    started = time.time()
    steps = {}
    log(f"=== ⚙ Starting Stable Accumulative Sync v{VERSION} ===" + (" [DRY-RUN]" if DRY_RUN else "")
        + (" [ALLOW-MASS]" if ALLOW_MASS else ""))

    lock = None
    if not DRY_RUN:
        lock = _acquire_lock()
        if lock is False:
            log("⏳ Другой прогон синка ещё идёт — выхожу без изменений")
            return True

    try:
        log("Step 1: Получаем список пользователей с parent...")
        parent_users = fetch_parent_users()
        steps['fetch_parent'] = parent_users is not None
        if parent_users is None:
            write_status(False, steps, started)
            return False
        parent_users, dropped = validate_parent_users(parent_users)
        if dropped:
            steps['fetch_parent'] = False
        parent_uuids = {pu['uuid'] for pu in parent_users}

        if not DRY_RUN:
            try:
                _refresh_marks(parent_users, dropped)
            except Exception as e:
                log(f"⚠️ Отметки удалений/выключений не обновлены: {e}")

        if DRY_RUN:
            log("Step 6 [dry-run]: план синхронизации пользователей...")
            ok = sync_users_from_parent(parent_users, dropped) and not dropped
            log("=== dry-run завершён: " + ("без ошибок" if ok else f"ошибок {RESULT['errors']}") + " ===")
            return ok

        # Шаги 2–4: трафик
        try:
            log("Step 2: Собираем локальную дельта статистику...")
            deltas = collect_local_usage_delta(parent_uuids)
            if deltas is None:
                steps['usage'] = False
                _note_error("❌ Не собрана локальная статистика трафика")
            elif deltas:
                log("Step 3: Отправляем дельта статистику на parent...")
                sent, dropped_usage, failed = send_usage_deltas_to_parent(deltas)
                log("Step 4: Вычитаем отправленное из локальной статистики...")
                reset_ok = reset_local_usage(sent + dropped_usage)
                steps['usage'] = (failed == 0) and reset_ok
                if failed:
                    _note_error(f"❌ Трафик не отправлен для {failed} пользователей (повтор в следующем прогоне)")
                if not reset_ok:
                    _note_error("❌ Не удалось вычесть отправленный трафик локально")
            else:
                steps['usage'] = True
                log("Step 3: Нет дельта статистики для отправки")
        except Exception as e:
            steps['usage'] = False
            _note_error(f"❌ Шаги 2–4 (трафик): {e}")
            _log_exc()

        # Шаг 5: last_online
        try:
            log("Step 5: Синхронизация last_online (child ↔ parent)...")
            steps['last_online'] = bool(sync_last_online(parent_users))
            if steps['last_online']:
                log("✅ Синхронизация last_online завершена")
            else:
                _note_error("❌ Ошибка синхронизации last_online")
        except Exception as e:
            steps['last_online'] = False
            _note_error(f"❌ Шаг 5 (last_online): {e}")
            _log_exc()

        # Шаг 6: пользователи
        if SKIP_USERS:
            steps['users'] = None
            log("⏭ Step 6 пропущен: SYNC_SKIP_USERS=1")
        else:
            log("Step 6: Полная синхронизация пользователей с parent...")
            steps['users'] = bool(sync_users_from_parent(parent_users, dropped))
            log("✅ Синхронизация пользователей завершена успешно" if steps['users']
                else "❌ Синхронизация пользователей с ошибками")

        ok = all(v for v in steps.values() if v is not None)
        write_status(ok, steps, started)
        log("✅ Stable sync completed successfully!" if ok else f"❌ Stable sync завершён с ошибками: {steps}")
        return ok

    except Exception as e:
        _note_error(f"❌ Критическая ошибка в синхронизации: {e}")
        _log_exc()
        write_status(False, steps, started)
        return False
    finally:
        if lock not in (None, False):
            os.close(lock)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
