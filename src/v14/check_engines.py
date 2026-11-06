#!/opt/hiddify-manager/.venv313/bin/python
"""Сверка пользователей панели Hiddify 14 с движками.

Эталон — локальный admin-API панели (is_active). Движки:
  xray         — живое состояние по gRPC API 127.0.0.1:10085: `xray api inbounduser` по каждому inbound
                 из generated/xray.json (email должен быть uuid; uuid@hiddify.com — след helper-ов xtlsapi);
  hiddify-core — СГЕНЕРИРОВАННЫЙ конфиг generated/hiddify-core.json (живого списка у движка нет: панель
                 меняет пользователей перерисовкой файла и SIGHUP) и то, что служба hiddify-core активна;
  telemt       — живой список API 127.0.0.1:10088/v1/users (username = uuid без дефисов, enabled, in_runtime).
Движок без своего generated-файла считается неиспользуемым. После изменений hiddify-core и telemt догоняют
за несколько секунд (apply-users): --retry N повторяет сверку до N раз с паузой 5 с.
Печатает только числа и обезличенные id (u:sha1[:8]).
Код выхода: 0 — совпадает; 1 — расхождения; 2 — расхождений нет, но не все движки удалось проверить.
usage: check_engines.py [--retry N] [u:xxxxxxxx ...]   — u:… — показать, где есть указанные пользователи
       SYNC_SCRIPT=/путь/stable_sync.py   — где взять конфиг синка (по умолчанию /opt/hiddify-custom/tools)
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

UU = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
GEN = '/opt/hiddify-manager/generated'
XRAY_BINS = ('/opt/hiddify-manager/services/xray/bin/xray',)      # как вызывает панель; PATH — запасной
SYNC_SCRIPT = os.environ.get('SYNC_SCRIPT', '/opt/hiddify-custom/tools/stable_sync.py')


def anon(u):
    return 'u:' + hashlib.sha1(u.encode()).hexdigest()[:8]


def dashed(n):
    n = str(n).lower()
    return f"{n[:8]}-{n[8:12]}-{n[12:16]}-{n[16:20]}-{n[20:]}" if re.fullmatch(r'[0-9a-f]{32}', n) else n


def core_id(user):
    """uuid пользователя inbound hiddify-core: поле с uuid зависит от протокола (name/uuid/password/
    username); при нескольких разных — name."""
    ids = {str(v).lower() for v in user.values() if isinstance(v, str) and UU.fullmatch(str(v).lower())}
    if len(ids) > 1:
        n = str(user.get('name', '')).lower()
        return n if n in ids else sorted(ids)[0]
    return ids.pop() if ids else None


def load_panel():
    spec = importlib.util.spec_from_file_location('ss', SYNC_SCRIPT)
    ss = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ss)
    users = ss.fetch_child_users()
    return None if users is None else {u.lower(): x for u, x in users.items()}


class Check:
    def __init__(self, users, want):
        self.active = {u for u, x in users.items() if x.get('is_active')}
        self.inactive = set(users) - self.active
        self.want = want
        self.lines, self.bad, self.unchecked, self.seen = [], 0, 0, {}

    def compare(self, engine, tag, present, wrong=0):
        miss, extra = self.active - present, present - self.active
        for u in present:
            if anon(u) in self.want:
                self.seen.setdefault(anon(u), []).append(engine)
        if miss or extra or wrong:
            self.bad += 1
            self.lines.append(
                f"  ✗ {engine} {tag}: есть {len(present)}, нет активных {len(miss)} {sorted(map(anon, miss))[:5]}, "
                f"лишних {len(extra)} (неактивных {len(extra & self.inactive)}) {sorted(map(anon, extra))[:5]}"
                + (f", чужих id {wrong}" if wrong else ''))
            return False
        return True

    def xray(self):
        path = f'{GEN}/xray.json'
        if not os.path.exists(path):
            self.lines.append('xray: не используется (нет generated/xray.json)')
            return
        xbin = next((b for b in XRAY_BINS if os.access(b, os.X_OK)), None) or shutil.which('xray')
        if not xbin:
            self.unchecked += 1
            self.lines.append('xray: не проверен — не найден бинарник xray')
            return
        with open(path, encoding='utf-8') as f:
            x = json.load(f)
        ok_n = tags = 0
        for ib in x.get('inbounds', []):
            st = ib.get('settings') or {}
            if not ('clients' in st or 'users' in st):
                continue
            tags += 1
            r = subprocess.run([xbin, 'api', 'inbounduser', '--server=127.0.0.1:10085', f"-tag={ib['tag']}"],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:               # xray не отвечает — пользователи этого inbound не работают
                self.bad += 1
                self.lines.append(f"  ✗ xray {ib['tag']}: API rc={r.returncode} {r.stderr.strip()[:120]}")
                continue
            emails = [str(u.get('email', '')).lower() for u in (json.loads(r.stdout or '{}').get('users') or [])]
            present = {e for e in emails if UU.fullmatch(e)}
            ok_n += self.compare('xray', ib['tag'], present, wrong=len(emails) - len(present))
        self.lines.append(f"xray: inbound с пользователями {tags}, совпали с панелью {ok_n} (живой gRPC API)")

    def core(self):
        path = f'{GEN}/hiddify-core.json'
        if not os.path.exists(path):
            self.lines.append('hiddify-core: не используется (нет generated/hiddify-core.json)')
            return
        state = subprocess.run(['systemctl', 'is-active', 'hiddify-core'], capture_output=True, text=True,
                               timeout=10).stdout.strip() or 'не найдена'
        if state != 'active':
            self.bad += 1
            self.lines.append(f"  ✗ hiddify-core: служба {state}")
        with open(path, encoding='utf-8') as f:
            h = json.load(f)
        ok_n = tags = 0
        for ib in h.get('inbounds', []):
            if not ib.get('users'):
                continue
            tags += 1
            ids = [core_id(u) for u in ib['users']]
            ok_n += self.compare('core', ib.get('tag'), {i for i in ids if i}, wrong=sum(1 for i in ids if not i))
        self.lines.append(f"hiddify-core: inbound с пользователями {tags}, совпали с панелью {ok_n} "
                          f"(сверен сгенерированный конфиг; служба {state})")

    def telemt(self):
        if not os.path.exists(f'{GEN}/telemt.toml'):
            self.lines.append('telemt: не используется (нет generated/telemt.toml)')
            return
        with urllib.request.urlopen('http://127.0.0.1:10088/v1/users', timeout=10) as resp:
            rows = json.load(resp).get('data') or []
        present = {dashed(u.get('username')) for u in rows if u.get('enabled') and u.get('in_runtime')}
        foreign = sum(1 for p in present if not UU.fullmatch(p))
        disabled = len(rows) - sum(1 for u in rows if u.get('enabled') and u.get('in_runtime'))
        ok = self.compare('telemt', 'users', {p for p in present if UU.fullmatch(p)})
        self.lines.append(f"telemt: записей {len(rows)}, включённых в runtime {len(present)} (не-uuid {foreign}), "
                          f"выключенных {disabled}, совпали с панелью: {'да' if ok else 'нет'} (живой API)")


def run_once(users, want):
    c = Check(users, want)
    for name, fn in (('xray', c.xray), ('hiddify-core', c.core), ('telemt', c.telemt)):
        try:
            fn()
        except Exception as e:
            c.unchecked += 1
            c.lines.append(f"{name}: не проверен — {type(e).__name__}: {str(e)[:120]}")
    return c


def main(argv):
    retry = 0
    if argv[:1] == ['--retry']:
        retry, argv = int(argv[1]), argv[2:]
    want = set(argv)
    for attempt in range(retry + 1):
        users = load_panel()
        if users is None:
            print('локальный API панели недоступен — сверка не выполнена')
            return 2
        c = run_once(users, want)
        if not c.bad or attempt == retry:
            break
        time.sleep(5)
    print(f"панель: {len(users)} пользователей, активных {len(c.active)}, неактивных {len(c.inactive)}"
          + (f" (попытка {attempt + 1} из {retry + 1})" if retry else ''))
    for line in c.lines:
        print(line)
    for w in sorted(want):
        places = c.seen.get(w, [])
        print(f"{w}: xray {places.count('xray')}, core {places.count('core')}, telemt {places.count('telemt')}")
    if c.bad:
        print(f"ИТОГ: расхождений: {c.bad}")
        return 1
    print("ИТОГ: движки совпадают с панелью" + (f"; не проверено движков: {c.unchecked}" if c.unchecked else ''))
    return 2 if c.unchecked else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
