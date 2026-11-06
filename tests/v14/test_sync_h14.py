#!/usr/bin/env python3
"""Тесты stable_sync.py v4.5.x-h14, sync_health_api.py и check_engines.py без сервера.

requests/pymysql/urllib3 — заглушки. Локальный admin-API и БД child — общее хранилище STORE,
поведение API повторяет Hiddify 14.1.0b4 (upsert применяет name/comment/telegram_id только
непустые, ed25519 — только парой, лимит обрезается до 1e6 ГБ, POST без name → 422, DELETE мягкий,
GET отдаёт только неудалённых видимых). Поэтому тесты ловят и «вечный PATCH» (второй прогон должен
давать 0 записей).

usage: python3 tests/v14/test_sync_h14.py [каталог со stable_sync.py, sync_health_api.py, check_engines.py;
       по умолчанию src/v14]
"""
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta

D = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'src', 'v14')
sys.argv = sys.argv[:1]
ONE_GIG = 1024 ** 3


# ---------------------------------------------------------------- хранилище child (БД + API 14)
class Store:
    def __init__(self):
        self.users = {}            # uuid -> строка таблицы user
        self.calls = []            # (method, path, body) записи в API
        self.fail = {}             # (method, path) -> Resp
        self.invisible = set()     # uuid, которых GET не видит (added_by вне дерева)
        self.sql = []
        self.on_reset = None       # хук: изменить current_usage между шагом 2 и 4
        self.ignore = set()        # поля, которые upsert молча не применяет (формат разошёлся)

    def row(self, uuid, **kw):
        r = {'uuid': uuid, 'name': 'n-' + uuid[:4], 'comment': None, 'telegram_id': None, 'lang': None,
             'usage_limit': 30 * ONE_GIG, 'package_days': 30, 'mode': 'no_reset', 'enable': 1,
             'start_date': None, 'last_reset_time': None, 'current_usage': 0, 'deleted': 0,
             'ed25519_private_key': '', 'ed25519_public_key': '', 'wg_pk': '', 'wg_pub': '', 'wg_psk': '',
             'last_online': datetime(2026, 11, 1, 10, 0, 0), 'max_ips': 100}
        r.update(kw)
        self.users[uuid] = r
        return r

    @staticmethod
    def is_active(r):
        if not r['enable'] or r['deleted']:
            return False
        if r['usage_limit'] and r['current_usage'] >= r['usage_limit']:
            return False
        if r['start_date'] and (date.today() - date.fromisoformat(str(r['start_date']))).days > r['package_days']:
            return False
        return True

    def schema(self, r):
        return {'uuid': r['uuid'], 'name': r['name'], 'comment': r['comment'], 'telegram_id': r['telegram_id'],
                'lang': r['lang'] or 'en', 'usage_limit_GB': r['usage_limit'] / ONE_GIG,
                'package_days': r['package_days'], 'mode': r['mode'], 'enable': bool(r['enable']),
                'is_active': self.is_active(r), 'start_date': str(r['start_date']) if r['start_date'] else None,
                'current_usage_GB': r['current_usage'] / ONE_GIG,
                'ed25519_private_key': r['ed25519_private_key'], 'ed25519_public_key': r['ed25519_public_key'],
                'wg_pk': r['wg_pk'], 'wg_pub': r['wg_pub'], 'wg_psk': r['wg_psk']}

    def upsert(self, r, b):
        b = {k: v for k, v in b.items() if k not in self.ignore}
        if b.get('name'):
            r['name'] = b['name']
        if b.get('comment'):
            r['comment'] = b['comment']
        if b.get('telegram_id'):
            r['telegram_id'] = b['telegram_id']
        if b.get('lang') is not None:
            r['lang'] = b['lang']
        if b.get('package_days') is not None:
            r['package_days'] = b['package_days']
        if 'start_date' in b:
            r['start_date'] = b['start_date']
        if b.get('current_usage_GB') is not None:
            r['current_usage'] = int(min(1e6, b['current_usage_GB']) * ONE_GIG)
        if b.get('usage_limit_GB') is not None:
            r['usage_limit'] = int(min(1e6, b['usage_limit_GB']) * ONE_GIG)
        if b.get('enable') is not None:
            r['enable'] = 1 if b['enable'] else 0
        if b.get('ed25519_private_key') and b.get('ed25519_public_key'):
            r['ed25519_private_key'], r['ed25519_public_key'] = b['ed25519_private_key'], b['ed25519_public_key']
        for k in ('wg_pk', 'wg_pub', 'wg_psk'):
            if b.get(k) is not None:
                r[k] = b[k]
        if b.get('mode') is not None:
            r['mode'] = b['mode']

    # ---- HTTP
    def request(self, method, url, headers=None, json=None, timeout=None, proxies=None, verify=None):
        path = url.split('/api/v2/admin/', 1)[1]
        if method != 'GET':
            self.calls.append((method, path, json))
        if (method, path) in self.fail:
            f = self.fail[(method, path)]
            if isinstance(f, Exception):
                raise f
            return f
        if method == 'GET' and path == 'user/':
            vis = [self.schema(r) for u, r in self.users.items() if not r['deleted'] and u not in self.invisible]
            return Resp(200, vis) if vis else Resp(404, None, '{"message": "You have no user"}')
        if method == 'POST' and path == 'user/':
            if not json.get('name'):
                return Resp(422, None, 'name missing')
            r = self.users.get(json['uuid'])
            if r and not r['deleted']:
                return Resp(400, None, 'The user exists')
            r = self.row(json['uuid'], name=json['name'], current_usage=0)
            self.upsert(r, json)
            return Resp(200, self.schema(r))
        m = re.match(r'user/([^/]+)/$', path)
        if m:
            r = self.users.get(m.group(1))
            if not r or r['deleted']:
                return Resp(404, None, 'User not found')
            if method == 'PATCH':
                self.upsert(r, json)
                return Resp(200, self.schema(r))
            if method == 'DELETE':
                r['deleted'], r['enable'] = 1, 0
                return Resp(200, {'status': 200})
        return Resp(500, None, 'unexpected')

    # ---- SQL
    def execute(self, sql, args):
        self.sql.append((sql, args))
        s = ' '.join(sql.split())
        alive = lambda: [r for r in self.users.values() if not r['deleted']]
        if 'information_schema' in s:
            return [{'c': c} for c in COLS]
        if s.startswith('SELECT uuid, name, current_usage FROM user'):
            return [{'uuid': r['uuid'], 'name': r['name'], 'current_usage': r['current_usage']}
                    for r in alive() if r['current_usage'] > args[0]]
        if s.startswith('UPDATE user SET current_usage = GREATEST'):
            if self.on_reset:
                self.on_reset()
            r = self.users[args[1]]
            r['current_usage'] = max(r['current_usage'] - args[0], 0)
            return []
        if s.startswith('SELECT uuid, name, last_online FROM user'):
            return [{'uuid': r['uuid'], 'name': r['name'], 'last_online': r['last_online']} for r in alive()]
        if s.startswith('UPDATE user SET last_online'):
            self.users[args[1]]['last_online'] = args[0]
            return []
        m = re.match(r'SELECT uuid, (.+) FROM user', s)
        if m and 'COUNT' not in s:
            fields = [f.strip() for f in m.group(1).split(',')]
            return [dict({'uuid': r['uuid']}, **{f: r[f] for f in fields}) for r in alive()]
        if s.startswith('SELECT COUNT(*) AS n FROM user'):
            return [{'n': len(alive())}]
        m = re.match(r'UPDATE user SET (.+?)(, last_modified_time=NOW\(\))? WHERE uuid=%s', s)
        if m:
            fields = [a.split('=')[0].strip() for a in m.group(1).split(', ')]
            r = self.users[args[-1]]
            for f, v in zip(fields, args[:-1]):
                r[f] = v
            return []
        raise AssertionError('неизвестный SQL: ' + s)


COLS = {'id', 'uuid', 'name', 'comment', 'telegram_id', 'lang', 'last_reset_time', 'current_usage',
        'last_online', 'deleted', 'last_modified_time', 'enable'}


class Resp:
    def __init__(self, code=200, data=None, text=''):
        self.status_code = code
        self._data = data
        self.text = text or json.dumps(data or {})

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._data


class Parent:
    """parent 12: GET списка/юзера, PATCH трафика и last_online."""
    def __init__(self):
        self.users = {}
        self.fail_get = set()
        self.fail_patch = set()
        self.list_resp = None
        self.patches = []
        self.verify_seen = set()

    def get(self, url, headers=None, verify=None, timeout=None):
        self.verify_seen.add(verify)
        tail = url.rsplit('/api/v2/admin/', 1)[1]
        if tail == 'user/':
            return self.list_resp or Resp(200, list(self.users.values()))
        uuid = tail.split('/')[1]
        if uuid in self.fail_get:
            return Resp(500, None, 'boom')
        u = self.users.get(uuid)
        return Resp(200, u) if u else Resp(404, None, 'not found')

    def patch(self, url, headers=None, json=None, verify=None, timeout=None):
        self.verify_seen.add(verify)
        uuid = url.rsplit('/api/v2/admin/', 1)[1].split('/')[1]
        self.patches.append((uuid, json))
        if uuid in self.fail_patch or uuid not in self.users:
            return Resp(500 if uuid in self.fail_patch else 404, None, 'boom')
        self.users[uuid].update(json)
        return Resp(200, self.users[uuid])


STORE = Store()
PARENT = Parent()


class FakeCursor:
    def __init__(self):
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.rows = STORE.execute(sql, args or ())

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    commits = 0

    def cursor(self, *a):
        return FakeCursor()

    def commit(self):
        FakeConn.commits += 1

    def close(self):
        pass


req = types.ModuleType('requests')
req.request = lambda *a, **k: STORE.request(*a, **k)
req.get = lambda *a, **k: PARENT.get(*a, **k)
req.patch = lambda *a, **k: PARENT.patch(*a, **k)
req.delete = lambda *a, **k: (_ for _ in ()).throw(AssertionError('requests.delete мимо _child_api'))
sys.modules['requests'] = req
u3 = types.ModuleType('urllib3')
u3.disable_warnings = lambda *a: None
u3.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)
sys.modules['urllib3'] = u3
pm = types.ModuleType('pymysql')
pm.cursors = types.SimpleNamespace(DictCursor=object)
pm.connect = lambda **k: FakeConn()
sys.modules['pymysql'] = pm


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ss = load('stable_sync', os.path.join(D, 'stable_sync.py'))
UU = lambda n: '%08x-0000-4000-8000-%012x' % (n, n)
TODAY = date.today()


def pu(n, **kw):
    d = {'uuid': UU(n), 'name': 'user%d' % n, 'usage_limit_GB': 30.0, 'package_days': 30, 'mode': 'no_reset',
         'enable': True, 'is_active': True, 'comment': '', 'telegram_id': None, 'start_date': None,
         'last_reset_time': None, 'lang': 'en', 'current_usage_GB': 1.0, 'last_online': '2026-11-01 10:00:00'}
    d.update(kw)
    return d


def child(n, **kw):
    kw.setdefault('name', 'user%d' % n)
    kw.setdefault('lang', 'en')
    return STORE.row(UU(n), **kw)


STATE_KEYS = ('STATUS_DIR', 'STATUS_FILE', 'PENDING_DELETE_FILE', 'PENDING_BLOCK_FILE', 'LAST_PATCH_FILE',
              'LOCK_FILE', 'DRY_RUN', 'ALLOW_MASS', 'ALLOW_MASS_DELETE', 'SKIP_USERS', 'PARENT_URL', 'API_KEY',
              'CHILD_URL')


def jload(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def jsave(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)


def age(path, seconds):
    """Состарить метки времени в файле состояния (pending_* или last_patch)."""
    d = jload(path)
    for k, v in d.items():
        if isinstance(v, int):
            d[k] = v - seconds
        elif isinstance(v, list):
            d[k] = [x - seconds for x in v]
        else:
            for f in v.values():
                f[1] -= seconds
    jsave(path, d)


class Base(unittest.TestCase):
    def setUp(self):
        STORE.__init__()
        PARENT.__init__()
        FakeConn.commits = 0
        self.tmp = tempfile.mkdtemp()
        for k in STATE_KEYS:
            setattr(self, '_orig_' + k, getattr(ss, k))
        ss.STATUS_DIR = self.tmp
        ss.STATUS_FILE = os.path.join(self.tmp, 'status.json')
        ss.PENDING_DELETE_FILE = os.path.join(self.tmp, 'pending_delete.json')
        ss.PENDING_BLOCK_FILE = os.path.join(self.tmp, 'pending_block.json')
        ss.LAST_PATCH_FILE = os.path.join(self.tmp, 'last_patch.json')
        ss.LOCK_FILE = os.path.join(self.tmp, 'sync.lock')
        ss.DRY_RUN = ss.ALLOW_MASS = ss.ALLOW_MASS_DELETE = ss.SKIP_USERS = False
        ss.PARENT_URL = 'https://parent.example.net/SeCrEtPaTh'
        ss.API_KEY = 'aaaaaaaa-1111-4222-8333-bbbbbbbbbbbb'
        ss.CHILD_URL = 'http://127.0.0.1:9000/SeCrEtPaTh'
        self.reset_result()

    def tearDown(self):
        for k in STATE_KEYS:
            setattr(ss, k, getattr(self, '_orig_' + k))

    def reset_result(self):
        for k, v in list(ss.RESULT.items()):
            ss.RESULT[k] = 0 if isinstance(v, int) and not isinstance(v, bool) else v
        ss.RESULT.update(errors=0, last_error='', parent_count=None, expected_count=None, local_count=None)

    def step6(self, parent, dropped=0):
        STORE.calls.clear()
        self.reset_result()
        out = io.StringIO()
        with redirect_stdout(out):
            ok = ss.sync_users_from_parent(parent, dropped)
        return ok, out.getvalue()

    def main(self):
        STORE.calls.clear()
        self.reset_result()
        out = io.StringIO()
        with redirect_stdout(out):
            ok = ss.main()
        return ok, out.getvalue()

    def writes(self):
        return [c for c in STORE.calls]

    def status(self):
        with open(ss.STATUS_FILE, encoding='utf-8') as f:
            return json.load(f)


# ---------------------------------------------------------------- шаг 6
class Step6(Base):
    def test_new_active_user_post_and_converges(self):
        child(1)
        par = [pu(1), pu(2, comment='привет', telegram_id='777', lang='ru', last_reset_time='2026-11-01')]
        ok, _ = self.step6(par)
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in self.writes()], ['POST'])
        body = self.writes()[0][2]
        self.assertEqual(body['uuid'], UU(2))
        self.assertIs(body['enable'], True)
        self.assertEqual(body['current_usage_GB'], 0)
        self.assertEqual((body['comment'], body['telegram_id'], body['lang']), ('привет', 777, 'ru'))
        self.assertNotIn('start_date', body)
        self.assertEqual(STORE.users[UU(2)]['last_reset_time'], '2026-11-01')   # SQL-выравнивание
        ok, _ = self.step6(par)
        self.assertTrue(ok)
        self.assertEqual(self.writes(), [])                       # сошлось: второй прогон без записей
        self.assertEqual(ss.RESULT['sql_fixed'], 0)

    def test_new_inactive_not_created(self):
        child(1)
        ok, out = self.step6([pu(1), pu(2, is_active=False)])
        self.assertTrue(ok)
        self.assertEqual(self.writes(), [])
        self.assertEqual(ss.RESULT['deferred_inactive'], 1)
        self.assertEqual(ss.RESULT['expected_count'], 1)

    def test_is_active_null_falls_back_to_enable(self):
        child(1)
        ok, _ = self.step6([pu(1, is_active=None, enable=True)])
        self.assertEqual(self.writes(), [])

    def test_empty_name_post_gets_fallback(self):
        ok, _ = self.step6([pu(5, name='')])
        self.assertTrue(ok)
        self.assertEqual(STORE.users[UU(5)]['name'], 'user-' + UU(5)[:8])
        ok, _ = self.step6([pu(5, name='')])
        self.assertEqual(self.writes(), [])

    def test_exists_but_invisible_is_error_without_patch(self):
        child(3)
        STORE.invisible.add(UU(3))
        ok, _ = self.step6([pu(3)])
        self.assertFalse(ok)
        self.assertEqual([c[0] for c in self.writes()], ['POST'])
        self.assertIn('не виден', ss.RESULT['last_error'])

    def test_meta_fields_via_sql_not_patch(self):
        child(1, comment='старый', telegram_id=5, name='old', lang='en')
        par = [pu(1, name='Новое', comment='', telegram_id=None, lang='ru')]
        ok, _ = self.step6(par)
        self.assertTrue(ok)
        self.assertEqual(self.writes(), [])                        # без API PATCH и apply-users
        r = STORE.users[UU(1)]
        self.assertEqual((r['name'], r['comment'], r['telegram_id'], r['lang']), ('Новое', None, None, 'ru'))
        upd = [s for s in STORE.sql if s[0].startswith('UPDATE user SET name')]
        self.assertTrue(upd and 'last_modified_time=NOW()' in upd[0][0])
        ok, _ = self.step6(par)
        self.assertEqual(ss.RESULT['sql_fixed'], 0)

    def test_invalid_lang_ignored(self):
        child(1, lang='en')
        ok, _ = self.step6([pu(1, lang='de')])
        self.assertTrue(ok)
        self.assertEqual(STORE.users[UU(1)]['lang'], 'en')

    def test_telegram_string_converges(self):
        child(1, telegram_id=12345)
        ok, _ = self.step6([pu(1, telegram_id='12345')])
        self.assertEqual(ss.RESULT['sql_fixed'], 0)

    def test_block_and_unblock_blocks_first(self):
        child(1, enable=0)
        child(2, enable=1)
        ok, _ = self.step6([pu(1), pu(2, is_active=False)])
        self.assertTrue(ok)
        w = self.writes()
        self.assertEqual([(c[1], c[2]) for c in w], [('user/%s/' % UU(2), {'enable': False}),
                                                     ('user/%s/' % UU(1), {'enable': True})])
        self.assertEqual((ss.RESULT['blocked'], ss.RESULT['unblocked']), (1, 1))

    def test_package_days_alone_no_pairing(self):
        child(1, start_date='2026-11-01')
        ok, _ = self.step6([pu(1, package_days=60, start_date=None)])
        self.assertEqual(self.writes()[0][2], {'package_days': 60})
        self.assertEqual(STORE.users[UU(1)]['start_date'], '2026-11-01')   # дата старта child не стёрта

    def test_start_date_sync_and_rearm(self):
        child(1, start_date='2026-11-01')
        self.step6([pu(1, start_date='2026-10-15')])
        self.assertEqual(self.writes()[0][2], {'start_date': '2026-10-15'})
        old = (TODAY - timedelta(days=40)).isoformat()
        child(2, start_date=old)
        self.step6([pu(2, start_date=None, package_days=30)])
        self.assertEqual(self.writes()[0][2], {'start_date': None})      # продление «сбросом дней» — сразу
        fresh = (TODAY - timedelta(days=3)).isoformat()
        child(3, start_date=fresh)
        self.step6([pu(3, start_date=None)])
        self.assertEqual(self.writes(), [])                                # не истёк — None не навязываем

    def test_usage_limit_clamp_converges(self):
        child(1)
        self.step6([pu(1, usage_limit_GB=2e6)])
        self.assertEqual(self.writes()[0][2], {'usage_limit_GB': 1e6})
        self.step6([pu(1, usage_limit_GB=2e6)])
        self.assertEqual(self.writes(), [])

    def test_ed25519_pair_only(self):
        child(1, ed25519_private_key='P1', ed25519_public_key='Q1')
        self.step6([pu(1, ed25519_private_key='', ed25519_public_key='Q2')])
        self.assertEqual(self.writes(), [])                                # одна половина — не шлём
        self.step6([pu(1, ed25519_private_key='P2', ed25519_public_key='Q1')])
        self.assertEqual(self.writes()[0][2], {'ed25519_private_key': 'P2', 'ed25519_public_key': 'Q1'})
        self.step6([pu(1, ed25519_private_key='P2', ed25519_public_key='Q1')])
        self.assertEqual(self.writes(), [])

    def test_mass_block_held_one_run_rest_syncs(self):
        for i in range(1, 21):
            child(i)
        par = [pu(i, is_active=False) for i in range(1, 21)] + [pu(30)]
        ok, out = self.step6(par)
        self.assertTrue(ok)                                                # отложено — не ошибка
        self.assertEqual([c[0] for c in self.writes()], ['POST'])          # остальной синк идёт
        self.assertEqual(ss.RESULT['held_blocks'], 20)
        self.assertEqual(len(jload(ss.PENDING_BLOCK_FILE)), 20)
        ok, _ = self.step6(par)                                            # рано: ещё не подтверждено
        self.assertEqual(self.writes(), [])
        age(ss.PENDING_BLOCK_FILE, 300)
        ok, _ = self.step6(par)
        self.assertTrue(ok)
        self.assertEqual([c[2] for c in self.writes()], [{'enable': False}] * 20)
        self.assertEqual(jload(ss.PENDING_BLOCK_FILE), {})
        ok, _ = self.step6(par)
        self.assertEqual(self.writes(), [])

    def test_mass_block_glitch_cleared(self):
        for i in range(1, 21):
            child(i)
        self.step6([pu(i, is_active=False) for i in range(1, 21)])
        age(ss.PENDING_BLOCK_FILE, 300)
        ok, _ = self.step6([pu(i) for i in range(1, 21)])                  # parent одумался
        self.assertEqual(self.writes(), [])
        self.assertEqual(jload(ss.PENDING_BLOCK_FILE), {})
        self.assertTrue(all(STORE.users[UU(i)]['enable'] for i in range(1, 21)))

    def test_mass_block_keeps_other_fields(self):
        for i in range(1, 21):
            child(i)
        self.step6([pu(i, is_active=False, package_days=60 if i == 1 else 30) for i in range(1, 21)])
        self.assertEqual(self.writes(), [('PATCH', 'user/%s/' % UU(1), {'package_days': 60})])

    def test_few_blocks_immediate(self):
        for i in range(1, 21):
            child(i)
        ok, _ = self.step6([pu(i, is_active=i > 5) for i in range(1, 21)])
        self.assertEqual(len(self.writes()), 5)
        self.assertEqual(ss.RESULT['held_blocks'], 0)

    def test_allow_mass_blocks_immediate(self):
        for i in range(1, 21):
            child(i)
        ss.ALLOW_MASS = True
        ok, _ = self.step6([pu(i, is_active=False) for i in range(1, 21)])
        self.assertTrue(ok)
        self.assertEqual(len(self.writes()), 20)

    def test_max_writes_split_across_runs(self):
        n = ss.MAX_WRITES + 5
        par = [pu(i) for i in range(1, n + 1)]
        ok, _ = self.step6(par)
        self.assertTrue(ok)
        self.assertEqual(len(self.writes()), ss.MAX_WRITES)
        self.assertEqual(ss.RESULT['backlog'], 5)
        ok, _ = self.step6(par)
        self.assertEqual(len(self.writes()), 5)
        self.assertEqual(ss.RESULT['backlog'], 0)
        ok, _ = self.step6(par)
        self.assertEqual(self.writes(), [])

    def test_cap_priority_blocks_unblocks_posts_rest(self):
        ss.MAX_WRITES = 4
        try:
            child(1)
            child(2, enable=0)
            child(3)
            child(4)
            par = [pu(1, package_days=60), pu(9), pu(2), pu(3, is_active=False), pu(4, package_days=90)]
            self.step6(par)
            got = [(c[0], c[2].get('uuid') or c[1]) for c in self.writes()]
            self.assertEqual(got, [('PATCH', 'user/%s/' % UU(3)), ('PATCH', 'user/%s/' % UU(2)),
                                   ('POST', UU(9)), ('PATCH', 'user/%s/' % UU(1))])
            self.assertEqual(ss.RESULT['backlog'], 1)
        finally:
            ss.MAX_WRITES = 40

    def test_stuck_field_not_repeated(self):
        child(1, mode='no_reset')
        child(2, mode='no_reset')
        STORE.ignore.add('mode')
        par = [pu(1, mode='monthly'), pu(2, mode='monthly', package_days=60)]
        ok, _ = self.step6(par)
        self.assertFalse(ok)                                               # ответ PATCH: mode не применён
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(ss.RESULT['stuck'], 2)
        self.assertIn('mode×2', ss.RESULT['last_error'])
        self.assertEqual(STORE.users[UU(2)]['package_days'], 60)          # остальное применилось
        ok, _ = self.step6(par)                                            # тот же PATCH снова — не шлём
        self.assertFalse(ok)
        self.assertEqual(self.writes(), [])
        self.assertEqual(ss.RESULT['stuck'], 2)
        ok, _ = self.step6(par)
        self.assertEqual(self.writes(), [])
        age(ss.LAST_PATCH_FILE, ss.STUCK_RETRY_S + 1)
        ok, _ = self.step6(par)                                            # раз в STUCK_RETRY_S — повтор,
        self.assertEqual(len(self.writes()), 2)
        self.assertFalse(ok)                                               # и он снова ошибка: health не «выздоравливает»
        STORE.ignore.clear()
        ok, _ = self.step6(par)                                            # починили API — до повтора не шлём
        self.assertEqual(self.writes(), [])
        age(ss.LAST_PATCH_FILE, ss.STUCK_RETRY_S + 1)
        ok, _ = self.step6(par)
        self.assertTrue(ok)
        self.assertEqual(len(self.writes()), 2)
        ok, _ = self.step6(par)
        self.assertTrue(ok)
        self.assertEqual(self.writes(), [])

    def test_last_patch_has_no_raw_values(self):
        child(1, wg_pk='old')
        self.step6([pu(1, wg_pk='PRIVATE-KEY-VALUE-123')])
        self.assertEqual(self.writes()[0][2], {'wg_pk': 'PRIVATE-KEY-VALUE-123'})
        with open(ss.LAST_PATCH_FILE, encoding='utf-8') as f:
            self.assertNotIn('PRIVATE-KEY-VALUE-123', f.read())

    def test_confirmed_blocks_failed_keep_marks(self):
        for i in range(1, 31):
            child(i)
        par = [pu(i, is_active=False) for i in range(1, 31)]
        self.step6(par)                                                    # 30 > max(5, 3): отложены
        self.assertEqual(self.writes(), [])
        age(ss.PENDING_BLOCK_FILE, 300)
        for i in range(1, 11):
            STORE.fail[('PATCH', 'user/%s/' % UU(i))] = Resp(502, None, 'busy')
        ok, _ = self.step6(par)
        self.assertFalse(ok)
        self.assertEqual(ss.RESULT['blocked'], 20)
        self.assertEqual(len(jload(ss.PENDING_BLOCK_FILE)), 10)            # отметки не прошедших сохранены
        STORE.fail.clear()
        age(ss.PENDING_BLOCK_FILE, 300)
        ok, _ = self.step6(par)                                            # 10 > 5, но уже подтверждены
        self.assertTrue(ok)
        self.assertEqual([c[2] for c in self.writes()], [{'enable': False}] * 10)
        self.assertEqual(ss.RESULT['held_blocks'], 0)
        self.assertEqual(jload(ss.PENDING_BLOCK_FILE), {})

    def test_cap_cut_blocks_not_held_again(self):
        ss.MAX_WRITES = 10
        try:
            for i in range(1, 31):
                child(i)
            par = [pu(i, is_active=False) for i in range(1, 31)]
            self.step6(par)
            age(ss.PENDING_BLOCK_FILE, 300)
            self.step6(par)
            self.assertEqual(len(self.writes()), 10)
            self.assertEqual(ss.RESULT['backlog'], 20)
            age(ss.PENDING_BLOCK_FILE, 300)
            self.step6(par)
            self.assertEqual(len(self.writes()), 10)                       # не отложены заново
            self.assertEqual(ss.RESULT['held_blocks'], 0)
        finally:
            ss.MAX_WRITES = 40

    def test_block_marks_stale_after_gap(self):
        for i in range(1, 21):
            child(i)
        par = [pu(i, is_active=False) for i in range(1, 21)]
        self.step6(par)
        age(ss.PENDING_BLOCK_FILE, ss.SEEN_GAP_S + 60)                     # наблюдений не было 16 минут
        self.step6(par)
        self.assertEqual(self.writes(), [])                                # отметки начаты заново
        self.assertEqual(ss.RESULT['held_blocks'], 20)

    def test_changed_value_not_stuck(self):
        child(1)
        self.step6([pu(1, package_days=60)])
        ok, _ = self.step6([pu(1, package_days=90)])
        self.assertTrue(ok)
        self.assertEqual(self.writes()[0][2], {'package_days': 90})

    def test_failed_patch_not_recorded(self):
        child(1)
        STORE.fail[('PATCH', 'user/%s/' % UU(1))] = Resp(500, None, 'boom')
        self.step6([pu(1, package_days=60)])
        del STORE.fail[('PATCH', 'user/%s/' % UU(1))]
        ok, _ = self.step6([pu(1, package_days=60)])
        self.assertTrue(ok)
        self.assertEqual(len(self.writes()), 1)

    def test_delete_needs_two_runs(self):
        for i in range(1, 6):
            child(i)
        par = [pu(i) for i in range(1, 5)]
        ok, out = self.step6(par)
        self.assertTrue(ok)
        self.assertEqual(self.writes(), [])                                # первый раз — только отметка
        ok, out = self.step6(par)
        self.assertEqual(self.writes(), [])                                # рано: прошло < DELETE_CONFIRM_S
        age(ss.PENDING_DELETE_FILE, 300)
        ok, _ = self.step6(par)
        self.assertEqual([c[0] for c in self.writes()], ['DELETE'])
        self.assertEqual(ss.RESULT['deleted'], 1)
        self.assertTrue(STORE.users[UU(5)]['deleted'])
        ok, _ = self.step6(par + [pu(5)])                                  # вернулся на parent → POST с purge
        self.assertEqual([c[0] for c in self.writes()], ['POST'])
        self.assertFalse(STORE.users[UU(5)]['deleted'])

    def test_mass_delete_releases_after_hour(self):
        for i in range(1, 21):
            child(i)
        par = [pu(i) for i in range(7, 21)]                                # нет 6 из 20 > max(3, 1)
        ok, out = self.step6(par)
        self.assertTrue(ok)                                                # не ошибка, а отложено
        self.assertEqual(self.writes(), [])
        self.assertEqual(ss.RESULT['held_deletes'], 6)
        self.assertNotIn('удалю, если не появится', out)
        age(ss.PENDING_DELETE_FILE, 300)
        ok, _ = self.step6(par)
        self.assertEqual(self.writes(), [])                                # подтверждено, но час не прошёл
        runs = 0
        while not self.writes():                                           # наблюдения каждые 5 минут
            age(ss.PENDING_DELETE_FILE, 300)
            self.step6(par)
            runs += 1
            self.assertLess(runs, 20)
        first_age = 300 + 300 * runs                                       # возраст отметок к этому прогону
        self.assertTrue(first_age >= ss.MASS_DELETE_RELEASE_S > first_age - 300, first_age)
        self.assertEqual([c[0] for c in self.writes()], ['DELETE'] * 3)    # по limit=3 за прогон
        self.assertEqual(ss.RESULT['held_deletes'], 3)
        age(ss.PENDING_DELETE_FILE, 300)
        self.step6(par)
        self.assertEqual(len(self.writes()), 3)                            # остаток 3 <= limit
        age(ss.PENDING_DELETE_FILE, 300)
        ok, _ = self.step6(par)
        self.assertTrue(ok)
        self.assertEqual(self.writes(), [])
        self.assertEqual(sum(1 for i in range(1, 7) if STORE.users[UU(i)]['deleted']), 6)

    def test_allow_mass_deletes_in_one_run(self):
        for i in range(1, 21):
            child(i)
        par = [pu(i) for i in range(7, 21)]
        ok, _ = self.step6(par)
        self.assertEqual(self.writes(), [])
        age(ss.PENDING_DELETE_FILE, 300)
        ss.ALLOW_MASS = True
        ok, _ = self.step6(par)                                            # сразу, без второго запуска
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in self.writes()], ['DELETE'] * 6)

    def test_allow_mass_needs_previous_observation(self):
        for i in range(1, 21):
            child(i)
        ss.ALLOW_MASS = True
        ok, out = self.step6([pu(i) for i in range(7, 21)])               # отсутствие видно впервые
        self.assertEqual(self.writes(), [])
        self.assertIn('меньше 4 мин', out)
        age(ss.PENDING_DELETE_FILE, 60)
        ok, _ = self.step6([pu(i) for i in range(7, 21)])                  # повтор через минуту — рано
        self.assertEqual(self.writes(), [])
        age(ss.PENDING_DELETE_FILE, 240)
        ok, _ = self.step6([pu(i) for i in range(8, 21)])                  # 1–6 подтверждены, 7 — впервые
        self.assertEqual(len(self.writes()), 6)
        self.assertFalse(STORE.users[UU(7)]['deleted'])

    def test_over_half_needs_allow_mass_delete(self):
        for i in range(1, 21):
            child(i)
        par = [pu(i) for i in range(12, 21)]                               # нет 11 из 20
        self.step6(par)
        age(ss.PENDING_DELETE_FILE, 300)
        ss.ALLOW_MASS = True
        ok, _ = self.step6(par)
        self.assertFalse(ok)
        self.assertEqual(self.writes(), [])
        self.assertIn('--allow-mass-delete', ss.RESULT['last_error'])
        age(ss.PENDING_DELETE_FILE, 300)
        ss.ALLOW_MASS = ss.ALLOW_MASS_DELETE = True
        ok, _ = self.step6(par)
        self.assertTrue(ok)
        self.assertEqual(len(self.writes()), 11)

    def test_allow_mass_does_not_bypass_stuck(self):
        child(1, mode='no_reset')
        STORE.ignore.add('mode')
        self.step6([pu(1, mode='monthly')])
        ss.ALLOW_MASS = True
        ok, _ = self.step6([pu(1, mode='monthly')])
        self.assertFalse(ok)
        self.assertEqual(self.writes(), [])

    def test_mass_delete_over_half_never_auto(self):
        for i in range(1, 21):
            child(i)
        par = [pu(i) for i in range(12, 21)]                               # нет 11 из 20
        for _ in range(15):
            ok, _ = self.step6(par)
            self.assertFalse(ok)
            self.assertEqual(self.writes(), [])
            age(ss.PENDING_DELETE_FILE, 300)

    def test_delete_mark_cleared_when_seen_in_incomplete_list(self):
        for i in range(1, 6):
            child(i)
        self.step6([pu(i) for i in range(1, 5)])                           # 5 нет → отметка
        age(ss.PENDING_DELETE_FILE, 300)
        self.step6([pu(i) for i in range(1, 6)], dropped=1)                # 5 вернулся, список с битой записью
        self.assertEqual(jload(ss.PENDING_DELETE_FILE), {})
        age(ss.PENDING_DELETE_FILE, 300)
        self.step6([pu(i) for i in range(1, 5)])                           # снова нет — это первое отсутствие
        self.assertEqual(self.writes(), [])

    def test_delete_mark_restarts_after_gap(self):
        for i in range(1, 6):
            child(i)
        self.step6([pu(i) for i in range(1, 5)])
        age(ss.PENDING_DELETE_FILE, ss.SEEN_GAP_S + 60)                    # шаг 6 не шёл 16 минут
        self.step6([pu(i) for i in range(1, 5)])
        self.assertEqual(self.writes(), [])
        age(ss.PENDING_DELETE_FILE, 300)
        self.step6([pu(i) for i in range(1, 5)])
        self.assertEqual([c[0] for c in self.writes()], ['DELETE'])

    def test_reappeared_user_leaves_pending(self):
        for i in range(1, 6):
            child(i)
        self.step6([pu(i) for i in range(1, 5)])
        self.step6([pu(i) for i in range(1, 6)])
        self.assertEqual(jload(ss.PENDING_DELETE_FILE), {})

    def test_delete_limit_and_dropped(self):
        for i in range(1, 21):
            child(i)
        ok, _ = self.step6([pu(i) for i in range(1, 10)])                 # нет 11 из 20: больше половины
        self.assertFalse(ok)
        self.assertEqual(self.writes(), [])
        self.assertEqual(len(jload(ss.PENDING_DELETE_FILE)), 11)          # отметки ведутся и здесь
        ok, out = self.step6([pu(i) for i in range(1, 20)], dropped=1)
        self.assertIn('Удаление пропущено', out)
        self.assertEqual(self.writes(), [])

    def test_bad_record_isolated(self):
        child(1, name='old1')
        child(2, enable=0)
        ok, _ = self.step6([pu(1, usage_limit_GB='unlimited'), pu(2), pu(3)])
        self.assertFalse(ok)
        meths = [(c[0], c[1]) for c in self.writes()]
        self.assertIn(('PATCH', 'user/%s/' % UU(2)), meths)
        self.assertIn(('POST', 'user/'), meths)
        self.assertEqual(ss.RESULT['errors'], 1)

    def test_validate_parent_drops_bad(self):
        out = io.StringIO()
        with redirect_stdout(out):
            good, dropped = ss.validate_parent_users([pu(1), {'uuid': None, 'name': 'x'}, 'str', pu(1), pu(2)])
        self.assertEqual([g['uuid'] for g in good], [UU(1), UU(2)])
        self.assertEqual(dropped, 3)

    def test_api_error_isolated_and_scrubbed(self):
        child(1, enable=0)
        child(2, enable=0)
        STORE.fail[('PATCH', 'user/%s/' % UU(1))] = Resp(500, None, 'boom ' + UU(1) + ' at http://127.0.0.1:9000/SeCrEtPaTh/x')
        ok, _ = self.step6([pu(1), pu(2)])
        self.assertFalse(ok)
        self.assertEqual(STORE.users[UU(2)]['enable'], 1)
        for bad in (UU(1), 'SeCrEtPaTh', 'user1', '[0-9a-f]{8}-'):
            self.assertIsNone(re.search(re.escape(bad) if bad != '[0-9a-f]{8}-' else bad, ss.RESULT['last_error']), bad)

    def test_timeout_isolated(self):
        child(1, enable=0)
        child(2, enable=0)
        STORE.fail[('PATCH', 'user/%s/' % UU(1))] = TimeoutError('read timed out')
        ok, _ = self.step6([pu(1), pu(2)])
        self.assertFalse(ok)
        self.assertEqual(STORE.users[UU(2)]['enable'], 1)

    def test_child_list_404_other_is_error(self):
        STORE.fail[('GET', 'user/')] = Resp(404, None, 'Not Found')
        ok, _ = self.step6([pu(1)])
        self.assertFalse(ok)
        self.assertEqual(self.writes(), [])

    def test_child_list_empty_is_ok(self):
        ok, _ = self.step6([pu(1)])
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in self.writes()], ['POST'])

    def test_dry_run_no_writes_and_anonymous(self):
        child(1, enable=0, name='Секрет Иванов')
        child(9, name='Удаляемый')
        ss.DRY_RUN = True
        ok, out = self.step6([pu(1), pu(2, name='Новый')])
        self.assertTrue(ok)
        self.assertEqual(self.writes(), [])
        self.assertFalse([s for s in STORE.sql if s[0].startswith('UPDATE')])
        self.assertEqual(FakeConn.commits, 0)
        self.assertIn('[dry-run] ✏️ PATCH u:', out)
        self.assertIn('(включить)', out)
        self.assertIn('[dry-run] ➕ POST u:', out)
        for name in ('Секрет', 'Новый', 'Удаляемый'):
            self.assertNotIn(name, out)
        for f in (ss.PENDING_DELETE_FILE, ss.PENDING_BLOCK_FILE, ss.LAST_PATCH_FILE):
            self.assertFalse(os.path.exists(f), f)


# ---------------------------------------------------------------- трафик, main, статус
class MainFlow(Base):
    def setUp(self):
        super().setUp()
        for i in (1, 2, 3):
            child(i, current_usage=int(2 * ONE_GIG))
            PARENT.users[UU(i)] = pu(i, current_usage_GB=10.0, start_date=None)

    def test_all_ok_status(self):
        ok, out = self.main()
        self.assertTrue(ok)
        st = self.status()
        self.assertTrue(st['ok'])
        self.assertTrue(st['steps']['users'])
        self.assertEqual(st['consecutive_failures'], 0)
        self.assertEqual((st['expected_count'], st['local_count']), (3, 3))
        self.assertEqual(oct(os.stat(ss.STATUS_FILE).st_mode & 0o777), '0o600')
        self.assertAlmostEqual(PARENT.users[UU(1)]['current_usage_GB'], 12.0, places=3)
        self.assertEqual(STORE.users[UU(1)]['current_usage'], 0)

    def test_partial_usage_failure_no_double_count(self):
        PARENT.fail_patch.add(UU(2))
        ok, _ = self.main()
        self.assertFalse(ok)
        self.assertTrue(self.status()['steps']['users'])                  # шаг 6 не «упал» из-за шага 3
        self.assertEqual(STORE.users[UU(1)]['current_usage'], 0)
        self.assertEqual(STORE.users[UU(2)]['current_usage'], int(2 * ONE_GIG))
        PARENT.fail_patch.clear()
        ok, _ = self.main()
        self.assertTrue(ok)
        self.assertAlmostEqual(PARENT.users[UU(1)]['current_usage_GB'], 12.0, places=3)   # не 14
        self.assertAlmostEqual(PARENT.users[UU(2)]['current_usage_GB'], 12.0, places=3)

    def test_subtract_keeps_traffic_added_meanwhile(self):
        def add_more():
            STORE.users[UU(1)]['current_usage'] += int(0.5 * ONE_GIG)
            STORE.on_reset = None
        STORE.on_reset = add_more
        self.main()
        self.assertEqual(STORE.users[UU(1)]['current_usage'], int(0.5 * ONE_GIG))

    def test_missing_on_parent_drops_delta(self):
        PARENT.users.pop(UU(3))
        PARENT.list_resp = Resp(200, [PARENT.users[UU(1)], PARENT.users[UU(2)]])
        ok, out = self.main()
        self.assertTrue(ok)
        self.assertEqual(STORE.users[UU(3)]['current_usage'], int(2 * ONE_GIG))   # не на parent — не шлём

    def test_parent_404_on_get_drops(self):
        orig = PARENT.get

        def get404(url, **k):
            if url.endswith('/%s/' % UU(2)):
                return Resp(404, None, 'nf')
            return orig(url, **k)
        req.get = get404
        try:
            ok, out = self.main()
        finally:
            req.get = lambda *a, **k: PARENT.get(*a, **k)
        self.assertIn('отброшена', out)
        self.assertEqual(STORE.users[UU(2)]['current_usage'], 0)

    def test_step5_failure_keeps_users_step_ok(self):
        STORE.users[UU(1)]['last_online'] = datetime(2026, 11, 6, 12, 0, 0)
        PARENT.fail_patch.add(UU(1))
        ok, _ = self.main()
        st = self.status()
        self.assertFalse(ok)
        self.assertFalse(st['steps']['last_online'])
        self.assertTrue(st['steps']['users'])
        self.assertIsNotNone(st['last_users_ok_ts'])

    def test_exception_in_usage_step_still_runs_step6(self):
        orig = ss.collect_local_usage_delta
        ss.collect_local_usage_delta = lambda p: (_ for _ in ()).throw(RuntimeError('boom'))
        try:
            PARENT.users[UU(4)] = pu(4)
            ok, _ = self.main()
        finally:
            ss.collect_local_usage_delta = orig
        st = self.status()
        self.assertFalse(st['steps']['usage'])
        self.assertTrue(st['steps']['users'])
        self.assertIn(UU(4), STORE.users)

    def test_consecutive_failures_and_corrupt_status(self):
        with open(ss.STATUS_FILE, 'w') as f:
            f.write('{"ts": 1')
        PARENT.list_resp = Resp(500, None, 'down')
        self.main()
        self.assertEqual(self.status()['consecutive_failures'], 1)
        self.main()
        self.assertEqual(self.status()['consecutive_failures'], 2)
        PARENT.list_resp = None
        self.main()
        self.assertEqual(self.status()['consecutive_failures'], 0)

    def test_lock_busy(self):
        import fcntl
        fd = os.open(ss.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            ok, out = self.main()
        finally:
            os.close(fd)
        self.assertTrue(ok)
        self.assertIn('Другой прогон', out)
        self.assertEqual(PARENT.patches, [])
        self.assertFalse(os.path.exists(ss.STATUS_FILE))

    def test_skip_users(self):
        ss.SKIP_USERS = True
        PARENT.users[UU(4)] = pu(4)
        ok, out = self.main()
        self.assertTrue(ok)
        self.assertNotIn(UU(4), STORE.users)
        st = self.status()
        self.assertTrue(st['users_skipped'])
        self.assertIsNone(st['last_users_ok_ts'])

    def test_dry_run_main(self):
        ss.DRY_RUN = True
        ok, out = self.main()
        self.assertTrue(ok)
        self.assertEqual(PARENT.patches, [])
        self.assertEqual(STORE.users[UU(1)]['current_usage'], int(2 * ONE_GIG))
        self.assertFalse(os.path.exists(ss.STATUS_FILE))
        self.assertIn('dry-run завершён', out)

    def test_dropped_records_fail_run_but_sync_rest(self):
        PARENT.list_resp = Resp(200, [PARENT.users[UU(1)], PARENT.users[UU(2)], {'uuid': None}, PARENT.users[UU(3)]])
        ok, _ = self.main()
        self.assertFalse(ok)
        self.assertTrue(self.status()['steps']['users'])

    def test_sync_anon_hides_names_everywhere(self):
        STORE.users[UU(1)]['last_online'] = datetime(2026, 11, 6, 12, 0, 0)
        PARENT.users[UU(4)] = pu(4)
        PARENT.fail_patch.add(UU(2))
        os.environ['SYNC_ANON'] = '1'
        try:
            ok, out = self.main()
        finally:
            del os.environ['SYNC_ANON']
        self.assertIn('u:', out)
        for name in ('user1', 'user2', 'user3', 'user4'):
            self.assertNotIn(name, out)
        ok, out = self.main()
        self.assertIn('user2', out)                                       # без SYNC_ANON — с именами

    def test_marks_refreshed_when_step6_fails(self):
        PARENT.list_resp = Resp(200, [PARENT.users[UU(1)], PARENT.users[UU(2)]])
        self.main()                                                        # 3 нет на parent → отметка
        self.assertEqual(len(jload(ss.PENDING_DELETE_FILE)), 1)
        age(ss.PENDING_DELETE_FILE, 300)
        PARENT.list_resp = None                                            # 3 вернулся, но child API лёг
        STORE.fail[('GET', 'user/')] = Resp(502, None, 'Bad Gateway')
        ok, _ = self.main()
        self.assertFalse(ok)
        self.assertEqual(jload(ss.PENDING_DELETE_FILE), {})                # отметка снята по списку parent
        del STORE.fail[('GET', 'user/')]
        age(ss.PENDING_DELETE_FILE, 300)
        PARENT.list_resp = Resp(200, [PARENT.users[UU(1)], PARENT.users[UU(2)]])
        self.main()                                                        # снова нет — первое отсутствие
        self.assertNotIn('DELETE', [c[0] for c in self.writes()])
        self.assertFalse(STORE.users[UU(3)]['deleted'])

    def test_block_marks_refreshed_when_step6_skipped(self):
        for i in range(4, 21):
            child(i)
            PARENT.users[UU(i)] = pu(i)
        for i in range(1, 21):
            PARENT.users[UU(i)]['is_active'] = False
        self.main()                                                        # 20 выключений отложены
        self.assertEqual(len(jload(ss.PENDING_BLOCK_FILE)), 20)
        age(ss.PENDING_BLOCK_FILE, 300)
        for i in range(1, 21):
            PARENT.users[UU(i)]['is_active'] = True                        # parent одумался,
        ss.SKIP_USERS = True                                               # но шаг 6 выключен
        self.main()
        self.assertEqual(jload(ss.PENDING_BLOCK_FILE), {})
        ss.SKIP_USERS = False
        age(ss.PENDING_BLOCK_FILE, 300)
        for i in range(1, 21):
            PARENT.users[UU(i)]['is_active'] = False
        self.main()                                                        # снова массово — снова ждём
        self.assertEqual([c for c in self.writes() if c[0] == 'PATCH'], [])
        self.assertTrue(all(STORE.users[UU(i)]['enable'] for i in range(1, 21)))

    def test_marks_extended_by_parent_list_alone(self):
        PARENT.list_resp = Resp(200, [PARENT.users[UU(1)], PARENT.users[UU(2)]])
        self.main()
        age(ss.PENDING_DELETE_FILE, 600)
        STORE.fail[('GET', 'user/')] = Resp(502, None, 'Bad Gateway')
        self.main()                                                        # шаг 6 не дошёл, но наблюдение есть
        del STORE.fail[('GET', 'user/')]
        age(ss.PENDING_DELETE_FILE, 600)                                   # от первого 1200 с, от последнего 600
        self.main()
        self.assertEqual([c[0] for c in self.writes() if c[0] == 'DELETE'], ['DELETE'])

    def test_parent_tls_verified_by_default(self):
        self.main()
        self.assertEqual(PARENT.verify_seen, {True})
        orig = ss.PARENT_TLS_VERIFY
        ss.PARENT_TLS_VERIFY = '/etc/hiddify-child-sync/parent.pem'
        try:
            PARENT.verify_seen.clear()
            self.main()
            self.assertEqual(PARENT.verify_seen, {'/etc/hiddify-child-sync/parent.pem'})
        finally:
            ss.PARENT_TLS_VERIFY = orig

    def test_log_scrubs_secrets(self):
        out = io.StringIO()
        with redirect_stdout(out):
            ss.log("HTTPSConnectionPool(host='parent.example.net', port=443): url: /SeCrEtPaTh/api/v2/admin/user/"
                   + UU(7) + "/ key " + ss.API_KEY + " see https://x.example/y")
        t = out.getvalue()
        for bad in ('parent.example.net', 'SeCrEtPaTh', UU(7), ss.API_KEY, 'x.example'):
            self.assertNotIn(bad, t)


# ---------------------------------------------------------------- health
class Health(unittest.TestCase):
    def setUp(self):
        self.h = load('sync_health_api', os.path.join(D, 'sync_health_api.py'))
        self.tmp = tempfile.mkdtemp()
        self.h.STATUS_FILE = os.path.join(self.tmp, 'status.json')
        H = self.h.SyncHealthHandler
        self.obj = H.__new__(H)
        self.sent = {}
        self.obj.send_json_response = lambda data, code=200: self.sent.update(data=data, code=code)
        self.obj.get_sync_service_status = lambda: {'active': True, 'enabled': True}
        self.obj.get_database_status = lambda: {'accessible': True, 'user_count': 92}
        self.obj.get_last_sync_info = lambda: {}
        self.obj.get_users_summary = lambda: {}

    def put(self, **kw):
        now = int(time.time())
        st = {'ts': now, 'ok': True, 'last_users_ok_ts': now, 'expected_count': 92, 'consecutive_failures': 0,
              'steps': {}, 'users_skipped': False}
        st.update(kw)
        with open(self.h.STATUS_FILE, 'w', encoding='utf-8') as f:
            json.dump(st, f)

    def run_h(self):
        self.obj.handle_health()
        return self.sent['data'], self.sent['code']

    def test_healthy(self):
        self.put()
        d, code = self.run_h()
        self.assertEqual((d['status'], code, d['reasons']), ('healthy', 200, []))

    def test_single_failure_degraded(self):
        self.put(ok=False, consecutive_failures=1, last_error='boom')
        d, code = self.run_h()
        self.assertEqual((d['status'], code), ('degraded', 200))

    def test_two_failures_unhealthy_503(self):
        self.put(ok=False, consecutive_failures=2, last_error='boom')
        d, code = self.run_h()
        self.assertEqual((d['status'], code), ('unhealthy', 503))

    def test_no_status_and_corrupt(self):
        d, code = self.run_h()
        self.assertEqual((d['status'], code), ('unhealthy', 503))
        with open(self.h.STATUS_FILE, 'w') as f:
            f.write('[1, 2]')
        d, code = self.run_h()
        self.assertEqual(code, 503)
        with open(self.h.STATUS_FILE, 'w') as f:
            f.write('{"ts": "x"}')
        d, code = self.run_h()
        self.assertEqual(code, 503)

    def test_stale_and_users_step_old(self):
        old = int(time.time()) - 3600
        self.put(ts=old, last_users_ok_ts=old)
        d, code = self.run_h()
        self.assertEqual(code, 503)
        self.assertTrue(any('статус устарел' in r for r in d['reasons']), d['reasons'])
        self.put(last_users_ok_ts=old)
        self.assertEqual(self.run_h()[0]['status'], 'unhealthy')

    def test_skip_users_degraded(self):
        self.put(users_skipped=True, last_users_ok_ts=None)
        d, code = self.run_h()
        self.assertEqual((d['status'], code), ('degraded', 200))

    def test_count_mismatch_degraded(self):
        self.put(expected_count=90)
        d, code = self.run_h()
        self.assertEqual(d['status'], 'degraded')
        self.assertTrue(any('ожидается 90' in r for r in d['reasons']))

    def test_backlog_and_held_degraded(self):
        self.put(backlog=5)
        d, code = self.run_h()
        self.assertEqual((d['status'], code), ('degraded', 200))
        self.put(held_blocks=20)
        self.assertEqual(self.run_h()[0]['status'], 'degraded')
        self.put(held_deletes=6)
        d, code = self.run_h()
        self.assertEqual((d['status'], code), ('degraded', 200))
        self.assertTrue(any('удален' in r for r in d['reasons']), d['reasons'])

    def test_server_threaded_with_timeout(self):
        self.assertTrue(self.h.SyncHealthHandler.timeout and self.h.SyncHealthHandler.timeout <= 30)
        import inspect
        self.assertIn('ThreadingHTTPServer', inspect.getsource(self.h.main))
        for k in ('connect_timeout', 'read_timeout'):
            self.assertIn(k, self.h.DB_CONFIG)

    def test_timer_inactive(self):
        self.put()
        self.obj.get_sync_service_status = lambda: {'active': False, 'enabled': True}
        self.assertEqual(self.run_h()[1], 503)



# ---------------------------------------------------------------- check_engines
class Engines(unittest.TestCase):
    def setUp(self):
        self.ce = load('check_engines', os.path.join(D, 'check_engines.py'))
        self.gen = tempfile.mkdtemp()
        self.ce.GEN = self.gen
        xb = os.path.join(self.gen, 'xray')
        with open(xb, 'w') as f:
            f.write('#!/bin/sh\n')
        os.chmod(xb, 0o755)
        self.ce.XRAY_BINS = (xb,)
        self.ce.time.sleep = lambda s: None
        self.u = [UU(i) for i in (1, 2, 3)]                               # 1, 2 активны, 3 — нет
        self.users = {self.u[0]: {'is_active': True}, self.u[1]: {'is_active': True}, self.u[2]: {'is_active': False}}
        self.ce.load_panel = lambda: dict(self.users)
        self.xray_emails = [self.u[0], self.u[1]]
        self.core_state = 'active'
        self.telemt = [{'username': u.replace('-', ''), 'enabled': True, 'in_runtime': True} for u in self.u[:2]]
        self.telemt_fail = False
        self.calls = 0
        for name, data in (('xray.json', {'inbounds': [{'tag': 't1', 'settings': {'clients': []}},
                                                       {'tag': 'api', 'settings': {}}]}),
                           ('hiddify-core.json', {'inbounds': [{'tag': 'c1', 'users': [{'name': self.u[0], 'password': 'x'},
                                                                                       {'uuid': self.u[1]}]}]})):
            with open(os.path.join(self.gen, name), 'w') as f:
                json.dump(data, f)
        open(os.path.join(self.gen, 'telemt.toml'), 'w').close()

        def run(cmd, **k):
            self.calls += 1
            if cmd[1:3] == ['api', 'inbounduser']:
                return types.SimpleNamespace(returncode=0, stdout=json.dumps({'users': [{'email': e} for e in self.xray_emails]}), stderr='')
            if cmd[:2] == ['systemctl', 'is-active']:
                return types.SimpleNamespace(returncode=0, stdout=self.core_state + '\n', stderr='')
            raise AssertionError(cmd)
        self.ce.subprocess.run = run

        test = self

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(url, timeout=None):
            if test.telemt_fail:
                raise OSError('connection refused')
            return Resp(json.dumps({'ok': True, 'data': test.telemt}).encode())
        self.ce.urllib.request.urlopen = urlopen

    def main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.ce.main(list(args))
        return rc, out.getvalue()

    def test_match(self):
        rc, out = self.main(_anon_ce(self.u[0]))
        self.assertEqual(rc, 0, out)
        self.assertIn('движки совпадают', out)
        self.assertIn('xray 1, core 1, telemt 1', out)
        for u in self.u:
            self.assertNotIn(u, out)                                       # только обезличенные id

    def test_mismatch_and_retry(self):
        self.xray_emails = [self.u[0]]
        rc, out = self.main()
        self.assertEqual(rc, 1)
        self.assertIn('нет активных 1', out)
        n = self.calls
        rc, out = self.main('--retry', '2')
        self.assertEqual(rc, 1)
        self.assertIn('попытка 3 из 3', out)
        self.assertEqual(self.calls - n, 3 * 2)                            # xray + systemctl в каждой попытке

    def test_inactive_user_in_engine_is_extra(self):
        self.xray_emails = list(self.u)
        rc, out = self.main()
        self.assertEqual(rc, 1)
        self.assertIn('лишних 1 (неактивных 1)', out)

    def test_wrong_email_format(self):
        self.xray_emails = [self.u[0], self.u[1] + '@hiddify.com']
        rc, out = self.main()
        self.assertEqual(rc, 1)
        self.assertIn('чужих id 1', out)

    def test_core_service_down(self):
        self.core_state = 'failed'
        rc, out = self.main()
        self.assertEqual(rc, 1)
        self.assertIn('hiddify-core: служба failed', out)

    def test_unchecked_engine_exit_2(self):
        self.telemt_fail = True
        rc, out = self.main()
        self.assertEqual(rc, 2)
        self.assertIn('telemt: не проверен', out)

    def test_engine_not_used(self):
        for n in ('xray.json', 'hiddify-core.json', 'telemt.toml'):
            os.remove(os.path.join(self.gen, n))
        rc, out = self.main()
        self.assertEqual(rc, 0)
        self.assertEqual(out.count('не используется'), 3)

    def test_no_xray_binary(self):
        self.ce.XRAY_BINS = ('/nonexistent/xray',)
        self.ce.shutil.which = lambda n: None
        rc, out = self.main()
        self.assertEqual(rc, 2)
        self.assertIn('xray: не проверен', out)

    def test_panel_unavailable(self):
        self.ce.load_panel = lambda: None
        rc, out = self.main()
        self.assertEqual(rc, 2)

    def test_ids(self):
        self.assertEqual(self.ce.core_id({'name': self.u[0], 'password': 'p'}), self.u[0])
        self.assertEqual(self.ce.core_id({'uuid': self.u[1], 'name': self.u[0]}), self.u[0])
        self.assertIsNone(self.ce.core_id({'username': 'abc'}))
        self.assertEqual(self.ce.dashed(self.u[0].replace('-', '')), self.u[0])
        self.assertEqual(self.ce.dashed('special'), 'special')


def _anon_ce(u):
    import hashlib
    return 'u:' + hashlib.sha1(u.encode()).hexdigest()[:8]


if __name__ == '__main__':
    unittest.main(verbosity=1)
