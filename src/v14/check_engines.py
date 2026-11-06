#!/opt/hiddify-manager/.venv313/bin/python
"""Сверка пользователей панели Hiddify 14 с живыми движками (de03).

Эталон — локальный admin-API (is_active). Движки:
  xray        — gRPC API 127.0.0.1:10085, `xray api inbounduser` по каждому inbound из generated/xray.json
                (email должен быть uuid; uuid@hiddify.com — след helper-ов xtlsapi);
  hiddify-core — generated/hiddify-core.json (users[].username = uuid), применяется по SIGHUP;
  telemt      — API 127.0.0.1:10088/v1/users (username = uuid без дефисов, enabled и in_runtime).
Печатает только числа и обезличенные id (u:sha1[:8]); код выхода 1 при расхождениях.
usage: check_engines.py [u:xxxxxxxx ...]   — дополнительно показать, где есть указанные пользователи
       SYNC_SCRIPT=/путь/stable_sync.py   — где взять конфиг синка (по умолчанию /opt/hiddify-custom/tools)
"""
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.request

UU = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def anon(u):
    return 'u:' + hashlib.sha1(u.encode()).hexdigest()[:8]


SYNC_SCRIPT = os.environ.get('SYNC_SCRIPT', '/opt/hiddify-custom/tools/stable_sync.py')
spec = importlib.util.spec_from_file_location('ss', SYNC_SCRIPT)
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)
users = ss.fetch_child_users()
if users is None:
    sys.exit('локальный API недоступен')
users = {u.lower(): x for u, x in users.items()}
active = {u for u, x in users.items() if x.get('is_active')}
inactive = set(users) - active
want = set(sys.argv[1:])
print(f"панель: {len(users)} пользователей, активных {len(active)}, неактивных {len(inactive)}")
bad = 0
seen_want = {}


def check(engine, tag, present, wrong=0):
    """present — множество uuid в движке для inbound tag."""
    global bad
    miss, extra = active - present, present - active
    for u in present:
        if anon(u) in want:
            seen_want.setdefault(anon(u), []).append(f"{engine}:{tag}")
    if miss or extra or wrong:
        bad += 1
        print(f"  ✗ {engine} {tag}: есть {len(present)}, нет активных {len(miss)} {sorted(map(anon, miss))[:5]}, "
              f"лишних {len(extra)} (неактивных {len(extra & inactive)}) {sorted(map(anon, extra))[:5]}"
              + (f", чужих email {wrong}" if wrong else ''))
    return not (miss or extra or wrong)


# xray: живые пользователи каждого inbound по gRPC API
x = json.load(open('/opt/hiddify-manager/generated/xray.json'))
ok_n = tags = 0
for ib in x.get('inbounds', []):
    st = ib.get('settings') or {}
    if not ('clients' in st or 'users' in st):
        continue
    tags += 1
    r = subprocess.run(['xray', 'api', 'inbounduser', '--server=127.0.0.1:10085', f"-tag={ib['tag']}"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        bad += 1
        print(f"  ✗ xray {ib['tag']}: API rc={r.returncode} {r.stderr.strip()[:120]}")
        continue
    emails = [str(u.get('email', '')).lower() for u in (json.loads(r.stdout or '{}').get('users') or [])]
    present = {e for e in emails if UU.fullmatch(e)}
    ok_n += check('xray', ib['tag'], present, wrong=len(emails) - len(present))
print(f"xray: inbound с пользователями {tags}, совпали с панелью {ok_n}")

# hiddify-core: сгенерированный конфиг (применяется по SIGHUP). Поле с uuid зависит от протокола
# (name/uuid/password/username) — берём uuid-значение пользователя, при нескольких — name.
def core_id(user):
    ids = {str(v).lower() for v in user.values() if isinstance(v, str) and UU.fullmatch(str(v).lower())}
    if len(ids) > 1:
        n = str(user.get('name', '')).lower()
        return n if n in ids else sorted(ids)[0]
    return ids.pop() if ids else None


h = json.load(open('/opt/hiddify-manager/generated/hiddify-core.json'))
ok_n = tags = 0
for ib in h.get('inbounds', []):
    if not ib.get('users'):
        continue
    tags += 1
    ids = [core_id(u) for u in ib['users']]
    ok_n += check('core', ib.get('tag'), {i for i in ids if i}, wrong=sum(1 for i in ids if not i))
print(f"hiddify-core: inbound с пользователями {tags}, совпали с панелью {ok_n}")

# telemt: живой список
d = json.load(urllib.request.urlopen('http://127.0.0.1:10088/v1/users', timeout=10))
rows = d.get('data') or []


def dashed(n):
    n = str(n).lower()
    return f"{n[:8]}-{n[8:12]}-{n[12:16]}-{n[16:20]}-{n[20:]}" if re.fullmatch(r'[0-9a-f]{32}', n) else n


present = {dashed(u.get('username')) for u in rows if u.get('enabled') and u.get('in_runtime')}
disabled = sum(1 for u in rows if not (u.get('enabled') and u.get('in_runtime')))
foreign = sum(1 for p in present if not UU.fullmatch(p))
ok_t = check('telemt', 'users', {p for p in present if UU.fullmatch(p)})
print(f"telemt: записей {len(rows)}, включённых в runtime {len(present)} (не-uuid {foreign}), выключенных {disabled}, "
      f"совпали с панелью: {'да' if ok_t else 'нет'}")

for w in sorted(want):
    places = seen_want.get(w, [])
    print(f"{w}: xray {sum(p.startswith('xray:') for p in places)}, core {sum(p.startswith('core:') for p in places)}, "
          f"telemt {sum(p.startswith('telemt:') for p in places)}")
print("ИТОГ:", "движки совпадают с панелью" if not bad else f"расхождений: {bad}")
sys.exit(1 if bad else 0)
